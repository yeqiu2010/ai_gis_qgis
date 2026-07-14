---
name: code-reviewer
description: 代码安全和 GIS 正确性审查
tools:
  - record_pipeline_stage
version: 2.0.0
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
- 用户要求导出到外部目录时，代码直接写外部目录；应改为工作目录输出 + `delivery_outputs`。
- `expected_outputs` 为空，或没有包含用户要求生成的最终文件，例如 `500m.shp`。
- `expected_outputs.path` 和代码实际输出文件名不一致。
- `expected_outputs` 声明了文件，但代码只向 stdout 打印内容，没有通过 Processing `OUTPUT`、文件写入或 Writer API 实际创建该文件。
- 使用未定义变量，例如 `QgsProject` 未导入且不在当前命名空间说明中。
- 使用 `QgsProcessing`、`QgsProcessingContext`、`QgsProcessingFeedback` 但既没有显式导入，也不在当前命名空间说明中；最终输出不能只使用 `QgsProcessing.TEMPORARY_OUTPUT`。
- 对字段做筛选前没有检查字段是否存在。
- 任务依赖距离/面积但未说明 CRS 或单位。
- 使用了未出现在 `solution_plan.algorithm_evidence` 中的算法。
- `processing.run` 参数名与已读取的算法详情不一致。
- 把完整任务拆成多个待确认执行调用，而不是一份完整脚本。
- 用户未明确要求修复数据，却调用 `native:fixgeometries`；分析查询应通过 Processing context 的 `GeometrySkipInvalid` 排除空几何或无效几何要素。
- 使用 `mapLayersByName(...)[0]` 而未先检查返回列表。
- 对 `processing.run` 的文件 `OUTPUT` 路径字符串直接调用 `featureCount()`。
- 使用 `processing.QgsProcessingFeedback()`；正确类位于 `qgis.core`。
- Processing `PREDICATE` 传入 `"intersects"`/`"within"` 等字符串，而不是算法详情定义的整数枚举列表。
- `JOIN_FIELDS` 传入字段索引而不是字段名；`native:aggregate` 的 `AGGREGATES` 不是 object 列表。
- `native:joinattributesbylocation` 用 `OVERLAY` 代替 `JOIN`，或者其他参数名/类型与算法详情不一致。
- 调用不存在的 `QgsGeometry.isGeosEmpty()`；空几何用 `isEmpty()`，无效几何由 `GeometrySkipInvalid` 排除。
- 未检查空间连接/聚合输出的实际字段，就假定 `SHAPE_Area` 等源字段仍然存在。
- 使用 `??_1` 等乱码/占位字段名，而不是来自 `inspect_layer` 或当前结果 `fields()` 的真实字段名。
- 未读取精确 API 证据却直接调用 `QgsVectorFileWriter.create/writeAsVectorFormat*` 重载；常规矢量输出应使用 Processing。
- 调用不存在的 `QgsProject.addVectorLayer`，或猜测未在算法详情中出现的结果键（如 `OUTPUT_COUNT`）。
- 直接从 `PyQt5` 或 `PyQt6` 导入 QGIS 运行时类型；必须使用
  `from qgis.PyQt...`，例如 `from qgis.PyQt.QtCore import QVariant`。
- 创建 Polygon/Line/Point 输出图层，却没有为输出要素调用 `setGeometry`；纯统计结果
  应创建无几何表，要求空间结果时必须保留或聚合真实几何。
- 分组统计中用赋值覆盖分母字段，例如遍历多栋建筑时反复执行
  `land_area = current_land_area`。地块面积必须按唯一地块去重汇总，不能按建筑重复累加，
  也不能只保留最后一个地块。

## GIS 正确性检查

- 图层查找应有空结果判断，例如 `if not matches: raise ValueError(...)`。
- 字段筛选应先读取 `field_names = [field.name() for field in layer.fields()]`。
- 属性表达式中的字段名用双引号，例如 `"leisure" = 'park'`。
- 中文名称模糊匹配优先用 `ILIKE '%公园%'`。
- Processing 输出参数必须是工作目录内路径字符串。
- 输出 vector/raster 后，`expected_outputs.type` 应匹配。
- 属性值探查不是最终产物时，不得虚构 `.txt` 预期输出；优先使用 `inspect_layer`，确需输出诊断文件时必须在代码中真实写入。
- 写 GeoPackage 时优先使用 Processing 算法的 `OUTPUT` 路径；只有取得当前 QGIS 版本精确 API 证据时才允许使用 Writer API。
- 密度、覆盖率等比值必须审查分子与分母的统计粒度一致；按用地类型统计时，分母应为该
  类型唯一地块面积总量，不能使用任意单个地块面积。

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
