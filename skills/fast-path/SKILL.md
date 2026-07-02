---
name: fast-path
description: 简单 GIS 任务快速路径
tools:
  - list_layers
  - inspect_layer
  - record_pipeline_stage
  - execute_gis_code
  - set_active_skill
version: 1.0.0
tags: [gis, fast-path]
---

# Fast Path

Fast Path 只处理简单、低风险、单步或近似单步的 GIS 任务。它的目标是减少不必要流程，但不能牺牲正确性。

## 可使用 Fast Path 的条件

必须同时满足：

- 只有一个主要输入图层。
- 只有一个明确操作，例如单次属性筛选、单次缓冲、单次导出、单次统计。
- 用户已给出必要参数，例如图层名、字段/属性含义、距离和单位、输出文件名。
- 不需要推断多个中间结果。
- 不需要组合空间关系，例如“先缓冲再相交”。

## 必须切回 GIS Pipeline 的情况

出现以下任意情况，立即调用：

```json
{"skill_name": "gis-pipeline"}
```

- 多步骤任务：提取、缓冲、相交、裁剪、空间连接、统计等组合出现。
- 属性筛选和空间筛选同时出现。
- 指定最终输出文件且需要多个处理步骤，例如 `500m.shp`。
- 需要检查字段值、CRS、距离单位或中间结果是否正确。
- 生成代码需要超过一个 Processing 算法。

## Fast Path 执行规则

- 执行前至少使用 `inspect_layer` 或已有上下文确认图层和字段。
- 代码必须写入 `QGIS_AGENT_WORKSPACE`。
- `expected_outputs` 必须非空，并与代码实际输出文件完全一致。
- 失败后不要反复猜测；若一次修复仍失败，切换到 `gis-pipeline`。
