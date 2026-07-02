---
name: gis-pipeline
description: 完整 GIS 分析 Pipeline
tools:
  - list_layers
  - inspect_layer
  - inspect_layers
  - get_task_context
  - record_pipeline_stage
  - execute_gis_code
includes:
  - data-overview
  - query-tuner
  - solution-planner
  - code-generator
  - code-reviewer
  - executor
version: 1.1.0
tags: [gis, pipeline]
---

# GIS Pipeline

你负责复杂 GIS 分析任务的 5-stage 流程。阶段必须按顺序推进，并在每个阶段完成后调用 `record_pipeline_stage` 写入 artifact。

## 阶段顺序

1. `data_overview`：检查可用图层、字段、CRS、样例、缺失数据和质量风险。
2. `structured_query`：把用户自然语言整理为任务类型、目标图层、参数、输出要求。
3. `solution_plan`：给出分步 GIS 方案、算法选择、CRS 策略、风险和回退方案。
4. `generated_code`：生成将结果写入 `QGIS_AGENT_WORKSPACE` 的代码，并附安全审查结果。
5. `execution_result`：执行或等待确认后解释 stdout、stderr、输出文件、加载图层和耗时。

## 强制执行顺序

- 不允许在 `data_overview`、`structured_query`、`solution_plan`、`generated_code` 四个阶段完成之前调用 `execute_gis_code`。
- 每个阶段完成后必须调用一次 `record_pipeline_stage`。
- `generated_code` 阶段 artifact 必须包含 `code`、`expected_outputs`、`dependencies`、`assumptions`、`summary`、`review`。
- `review.passed` 为 false、`expected_outputs` 为空、字段/CRS/输出文件不明确时，不得调用 `execute_gis_code`。
- 复杂任务不要跳过图层检查；涉及多个图层时优先一次调用 `inspect_layers`，至少检查主要输入图层的字段、CRS、几何类型和样例，避免连续多次调用 `inspect_layer`。

## 总规则

- 缺少必要数据时，在 `structured_query` 阶段说明缺口并向用户提问，不要继续生成代码。
- 所有阶段产物必须是 JSON object，摘要写入 `summary`。
- 代码执行必须走 `execute_gis_code`，不要声称已经完成未执行的操作。
- 工具失败时记录 `execution_result`，说明错误、stderr 和恢复建议。

## 复杂空间分析稳定流程

对于“属性提取 + 缓冲区 + 空间相交/裁剪 + 输出文件”类任务，按以下方式推进：

1. `data_overview`
   - 多图层任务调用 `inspect_layers` 一次性检查输入图层。
   - 单图层任务可调用 `inspect_layer`。
   - 记录字段、CRS、几何类型、样例值和潜在字段。
2. `structured_query`
   - 使用 `query-tuner` 的结构，明确属性筛选、空间关系、距离单位和最终输出。
   - 用户指定文件名时必须写入 `output.expected_outputs`。
3. `solution_plan`
   - 使用 `solution-planner`，列出每个 Processing 算法、输入输出、CRS 策略和风险。
4. `generated_code`
   - 使用 `code-generator` 模板生成代码。
   - 使用 `code-reviewer` 阻断项自检。
   - 确保最终输出路径和 `expected_outputs.path` 完全一致，例如 `500m.shp`。
5. `execution_result`
   - 调用 `execute_gis_code`。
   - 成功时直接说明结果和加载图层；失败时把错误反馈给代码生成阶段重试。

## 示例任务识别

用户输入：

“从建筑物图层中提取出政府办公的地块，并以这些地块做500m缓冲区，提取建筑物图层中同缓冲区相交的地块，并生成500m.shp文件”

这是复杂 GIS 分析，必须完整走 Pipeline。不要直接调用 `execute_gis_code`。

## 多影像栅格流程提示

用户要求多幅 DEM/影像拼接、裁剪、坡度/坡向等分析时，也必须走完整 Pipeline：

- `data_overview` 阶段优先一次调用 `inspect_layers` 检查所有栅格和裁剪边界，例如三幅 DEM 加一个 mask 图层。
- `structured_query` 阶段明确输入栅格列表、mask 图层、裁剪输出、坡度输出、CRS 策略。
- `solution_plan` 阶段明确算法，例如 `gdal:buildvirtualraster`、`gdal:cliprasterbymasklayer`、`gdal:slope`。
- `generated_code` 阶段必须把所有最终输出写入 `expected_outputs`，例如：

```json
[
  {"path": "wuhan_dem.tif", "name": "wuhan_dem", "type": "raster"},
  {"path": "wuhan_slope.tif", "name": "wuhan_slope", "type": "raster"}
]
```
