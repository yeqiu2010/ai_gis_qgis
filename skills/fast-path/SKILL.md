---
name: fast-path
lifecycle: task
description: 简单 GIS 任务快速路径
tools:
  - list_layers
  - inspect_layer
  - record_pipeline_stage
  - execute_gis_code
  - load_skill
version: 2.0.0
author: AI GIS QGIS Plugin
license: MIT
platforms: [linux, Windows, macos]
metadata:
  hermes:
    tags: [gis, fast-path]
    related_skills: [qgis-toolbox, gis-pipeline]
  qgis_agent:
    side_effects:
      writes_files: true
      requires_confirmation: true
---

# Fast Path

Fast Path 只处理简单、低风险、单步或近似单步的 GIS 任务。它的目标是减少不必要流程，但不能牺牲正确性。

## 可使用 Fast Path 的条件

必须同时满足：

- 只有一个主要输入图层。
- 只有一个明确操作，例如单次属性筛选、单次缓冲、单次导出、单次统计。
- 用户已给出必要参数，例如图层名、字段/属性含义、距离和单位；输出文件名可缺省，缺省时使用任务默认文件名。
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
- 用户要求生成/导出文件时，代码必须写入 `QGIS_AGENT_WORKSPACE`；未指定文件名时自动使用合理默认文件名，不要询问保存文件夹。
- 有文件结果时 `expected_outputs` 必须非空并与实际输出完全一致；单次统计且 stdout 就是最终答案，或用户明确要求直接调整当前图层样式时，使用空数组。
- 无文件统计必须打印口径、单位和最终数值；无文件样式调整必须更新 renderer、触发重绘并打印完成摘要。
- 不得把仅打印到 stdout 的字段值探查声明成预期文件；用户已明确字段和筛选值时直接生成最终筛选结果。
- 失败后不要反复猜测；若一次修复仍失败，切换到 `gis-pipeline`。
