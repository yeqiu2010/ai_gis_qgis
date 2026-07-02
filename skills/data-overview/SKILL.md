---
name: data-overview
description: 数据盘点和质量检查
tools:
  - list_layers
  - inspect_layer
  - get_task_context
  - record_pipeline_stage
version: 1.0.0
tags: [gis, pipeline, data]
---

# Data Overview

目标是确认当前 QGIS 工程是否具备完成任务的数据。

- 使用 `list_layers` 获取图层清单。
- 对候选图层使用 `inspect_layer` 获取字段、CRS、范围、样例和要素数。
- 记录 artifact 字段：`available_layers`、`candidate_layers`、`fields`、`crs`、`missing_data`、`quality_notes`、`summary`。
- 缺少必要数据时，不继续生成代码，在后续回复中请用户补充。
