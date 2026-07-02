---
name: code-reviewer
description: 代码安全和 GIS 正确性审查
tools:
  - record_pipeline_stage
version: 1.1.2
tags: [gis, pipeline, review]
---

# Code Reviewer

执行前审查 `generated_code`。审查必须具体，不要只写“代码看起来没问题”。

## 阻断项

发现以下任意问题，不要调用 `execute_gis_code`，必须要求重新生成代码：

- 创建 `QgsApplication`、`QApplication` 或调用 `initQgis`、`setPrefixPath`。
- 使用 `subprocess`、`os.system`、`eval`、`exec`、`sys.exit`。
- 删除或覆盖工作目录外文件。
- 输出没有写入 `QGIS_AGENT_WORKSPACE`。
- `expected_outputs` 为空，或没有包含用户要求生成的最终文件，例如 `500m.shp`。
- `expected_outputs.path` 和代码实际输出文件名不一致。
- 使用未定义变量，例如 `QgsProject` 未导入且不在当前命名空间说明中。
- 使用 `QgsProcessing`、`QgsProcessingContext`、`QgsProcessingFeedback` 但既没有显式导入，也不在当前命名空间说明中；最终输出不能只使用 `QgsProcessing.TEMPORARY_OUTPUT`。
- 对字段做筛选前没有检查字段是否存在。
- 任务依赖距离/面积但未说明 CRS 或单位。

## GIS 正确性检查

- 图层查找应有空结果判断，例如 `if not matches: raise ValueError(...)`。
- 字段筛选应先读取 `field_names = [field.name() for field in layer.fields()]`。
- 属性表达式中的字段名用双引号，例如 `"leisure" = 'park'`。
- 中文名称模糊匹配优先用 `ILIKE '%公园%'`。
- Processing 输出参数必须是工作目录内路径字符串。
- 输出 vector/raster 后，`expected_outputs.type` 应匹配。

## 审查产物

审查结果写入 `generated_code.artifact.review` 或 `safety_review`：

```json
{
  "passed": true,
  "blocking_issues": [],
  "warnings": [],
  "checked_items": [
    "workspace_output",
    "expected_outputs_match",
    "field_existence",
    "no_new_qgis_app",
    "no_forbidden_calls"
  ]
}
```

如果 `passed=false`，必须说明如何修复，并重新进入 `code-generator`。
