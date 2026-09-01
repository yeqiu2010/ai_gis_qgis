---
name: data-overview
description: 数据盘点和质量检查
tools:
  - list_layers
  - inspect_layer
  - get_task_context
  - record_pipeline_stage
version: 1.0.0
author: AI GIS QGIS Plugin
license: MIT
metadata:
  hermes:
    tags: [gis, pipeline, data]
    requires_tools: [list_layers, inspect_layer, record_pipeline_stage]
---

# Data Overview

目标是确认当前 QGIS 工程是否具备完成任务的数据。

- 使用 `list_layers` 获取图层清单。
- 对候选图层使用 `inspect_layer` 获取字段、CRS、范围、样例和要素数。
- 记录 artifact 字段：`available_layers`、`candidate_layers`、`resolved_layers`、`fields`、`crs`、`missing_data`、`quality_notes`、`summary`。
- `resolved_layers` 必须逐个保留 `inspect_layer` 返回的 `id`、`name`、`type`、`source` 和 `crs`；图层 ID 是后续代码解析当前 QGIS 图层的权威标识，不得只保留名称。
- 缺少必要数据时，不继续生成代码，在后续回复中请用户补充。
