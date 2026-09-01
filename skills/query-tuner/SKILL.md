---
name: query-tuner
description: 需求澄清和结构化查询
tools:
  - record_pipeline_stage
version: 2.0.0
author: AI GIS QGIS Plugin
license: MIT
metadata:
  hermes:
    tags: [gis, pipeline, query]
    requires_tools: [record_pipeline_stage]
---

# Query Tuner

把用户自然语言转换为可执行的结构化任务。

## 必须产出的 artifact 字段

- `task_type`：例如 `attribute_extract_buffer_intersection_export`。
- `target_layers`：涉及的图层名称、QGIS 图层 ID、角色和是否已通过工具确认。
- `attribute_filters`：属性筛选目标、候选字段、候选取值和证据来源。
- `spatial_relationship`：空间关系，例如 `intersects`、`within_distance`、`clip`。
- `parameters`：距离、单位、缓冲区策略、是否 dissolve、中间结果名称。
- `fields`：已确认字段、缺失字段、可能字段。
- `output`：最终输出文件名、格式、图层名和 `expected_outputs` 建议。
- `assumptions`：所有假设，尤其是“政府办公”对应字段/取值的假设。
- `questions`：无法安全继续时必须询问用户的问题。
- `operations`：有序 GIS 操作；每项包含业务目标、输入、输出、依赖、
  `gis_terms_zh`、`gis_terms_en` 和 `candidate_algorithm_terms`。
- `summary`：一句话任务摘要。

## 结构化规则

- 距离和面积必须包含单位；例如 `500m` 解析为 `distance=500, unit=meter`。
- 用户指定文件名时必须保留原文件名，例如 `500m.shp`，并写入 `output.expected_outputs`。
- 用户未指定文件名或目录时，不要提出“保存到哪个文件夹”的问题；为分析结果生成默认文件名，并标记 `output.location = "QGIS_AGENT_WORKSPACE"`。
- 用户明确指定外部目录时，记录 `output.delivery_directory` 和 `output.delivery_outputs`；执行代码仍输出到 `QGIS_AGENT_WORKSPACE`，外部目录只用于交付复制。
- 多步骤任务必须把每一步拆成结构化操作，不要压缩成一句“执行分析”。
- 不要在本阶段选择最终算法。先把“周边、落在、密度、统一坐标系”等业务语言
  转换为 `buffer`、`intersects`、`spatial join`、`aggregate`、`field calculator`、
  `reproject layer` 等标准 GIS 术语，供下一阶段一次检索。
- 使用以下核心映射生成每个操作的 `candidate_algorithm_terms`：
  - 筛选/选出：`attribute filter`, `extract by expression`
  - 周边/服务半径：`buffer`, `distance`, `dissolve`
  - 位于/范围内：`within`, `contains`, `extract by location`
  - 重叠/交叉部分：`intersects`, `intersection`, `overlay`
  - 空间归属：`spatial join`, `join attributes by location`
  - 分类统计：`statistics by categories`, `aggregate`, `group by`
  - 添加计算字段：`field calculator`, `expression`
  - 面积/长度/覆盖率：`area calculation`, `intersection area`, `ratio`
  - 坐标统一：`reproject layer`, `projected CRS`
- 字段名、图层名、属性取值不明确时写入 `questions`，并停止后续代码生成。
- 如果已有 `inspect_layer` 结果，必须优先使用真实字段；不要硬猜字段名。
- 如果上游 `data_overview.resolved_layers` 已提供图层 ID，必须把对应 `id` 原样写入 `target_layers`，不得在结构化阶段退化为只有图层名称。

## 复杂任务示例

用户：“从建筑物图层中提取出政府办公的地块，并以这些地块做500m缓冲区，提取建筑物图层中同缓冲区相交的地块，并生成500m.shp文件”

应结构化为：

```json
{
  "task_type": "attribute_extract_buffer_intersection_export",
  "target_layers": [{"name": "建筑物", "role": "source_and_intersection_target"}],
  "attribute_filters": [{
    "concept": "政府办公",
    "candidate_fields": ["amenity", "office", "building", "name"],
    "candidate_values": ["government", "public", "政务", "政府", "办公"]
  }],
  "spatial_relationship": "intersects_buffer",
  "parameters": {"buffer_distance": 500, "unit": "meter", "dissolve_buffer": true},
  "output": {
    "filename": "500m.shp",
    "name": "500m",
    "type": "vector",
    "expected_outputs": [{"path": "500m.shp", "name": "500m", "type": "vector"}]
  },
  "questions": []
}
```
