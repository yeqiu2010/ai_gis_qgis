---
name: code-reviewer
description: 代码安全和 GIS 正确性审查
tools:
  - record_pipeline_stage
version: 2.1.0
author: AI GIS QGIS Plugin
license: MIT
metadata:
  hermes:
    tags: [gis, pipeline, review]
    requires_tools: [record_pipeline_stage]
---

# Code Reviewer

执行前审查 `generated_code`。审查必须具体，不要只写“代码看起来没问题”。

## 阻断项

发现以下任意问题，不要调用 `execute_gis_code`，必须要求重新生成代码：

- 创建 `QgsApplication`、`QApplication` 或调用 `initQgis`、`setPrefixPath`。
- 使用 `subprocess`、`os.system`、`eval`、`exec`、`sys.exit`。
- 删除或覆盖工作目录外文件。
- 用户要求导出到外部目录时，代码直接写外部目录；应改为工作目录输出 + `delivery_outputs`。
- 用户要求生成文件（例如 `500m.shp`），但 `expected_outputs` 为空或没有包含该文件。仅需 stdout 最终统计结论或直接调整当前图层样式时允许空数组。
- `expected_outputs` 的文件名、类型或数量与用户需求及 `structured_query` 输出契约不一致。
- 使用未定义变量，例如 `QgsProject` 未导入且不在当前命名空间说明中。
- 使用 `QgsProcessing`、`QgsProcessingContext`、`QgsProcessingFeedback` 但既没有显式导入，也不在当前命名空间说明中；最终输出不能只使用 `QgsProcessing.TEMPORARY_OUTPUT`。
- 对字段做筛选前没有检查字段是否存在。
- 任务依赖距离/面积但未说明 CRS 或单位。
- 使用了未出现在 `solution_plan.algorithm_evidence` 中的算法。
- `processing.run` 参数名与已读取的算法详情不一致。
- 把完整任务拆成多个待确认执行调用，而不是一份完整脚本。
- 用户未明确要求修复数据，却调用 `native:fixgeometries`；分析查询应通过 Processing context 的 `GeometrySkipInvalid` 排除空几何或无效几何要素。
- 把枚举写成不存在的 `QgsProcessingContext.InvalidGeometryCheck`；正确写法必须是 `Qgis.InvalidGeometryCheck.GeometrySkipInvalid`。
- 使用 `mapLayersByName(...)[0]` 而未先检查返回列表。
- 把 `inspect_layer` 返回的 QGIS 图层 ID 字符串直接传给 Processing 图层参数；必须先用 `QgsProject.instance().mapLayer(layer_id)` 取得对象并检查返回值不是 `None`。调用 `mapLayer()` 后直接访问 `extent()`、`fields()` 或传给 Processing 而没有空值检查也必须阻断。
- 用户指定的输出图层名、文件名或数字后缀被改写，例如要求 `pdst_250` 却生成 `pdst_255`。
- 使用不同核函数、相似算法或自编近似计算替代用户明确要求的分析工具，但结构化需求中没有用户批准替代的证据。
- 对 `processing.run` 的文件 `OUTPUT` 路径字符串直接调用 `featureCount()`；但不得把 `"memory:"` 或 `TEMPORARY_OUTPUT` 返回的图层对象误判成路径，也不得用 `QgsVectorLayer(..., "memory")` 重新包装该对象。
- 使用 `processing.QgsProcessingFeedback()`；正确类位于 `qgis.core`。
- Processing `PREDICATE` 传入 `"intersects"`/`"within"` 等字符串，而不是算法详情定义的整数枚举列表。
- `JOIN_FIELDS` 传入字段索引而不是字段名；`native:aggregate` 的 `AGGREGATES` 不是 object 列表。
- `native:aggregate` 使用了 `GROUP_BY`，但没有在 `AGGREGATES` 中用 `first_value`（或等价的稳定聚合）显式输出分组键；或者下游属性连接仍引用聚合前的源字段名，而不是聚合结果中的真实别名。
- `native:joinattributesbylocation` 用 `OVERLAY` 代替 `JOIN`，或者其他参数名/类型与算法详情不一致。
- 调用不存在的 `QgsGeometry.isGeosEmpty()`；空几何用 `isEmpty()`，无效几何由 `GeometrySkipInvalid` 排除。
- 未检查空间连接/聚合输出的实际字段，就假定 `SHAPE_Area` 等源字段仍然存在。
- 使用 `??_1` 等乱码/占位字段名，而不是来自 `inspect_layer` 或当前结果 `fields()` 的真实字段名。
- 未读取精确 API 证据却直接调用 `QgsVectorFileWriter.create/writeAsVectorFormat*` 重载；常规矢量输出应使用 Processing。
- 调用不存在的 `QgsProject.addVectorLayer`，或猜测未在算法详情中出现的结果键（如 `OUTPUT_COUNT`）。
- 对已经列入 `expected_outputs`、将由执行器自动加载的最终 vector/raster 文件，又手工创建 `QgsRasterLayer`/`QgsVectorLayer` 并调用 `addMapLayer()`，导致结果图层重复加载。

不要尝试通过穷举 Processing、GDAL、Writer 或 Python 文件 API 来静态证明输出一定会生成，也不要根据不完整的静态数据流推断工作目录中的中间文件尚未创建。代码审查只检查声明式输出契约、安全边界和确定性的 API 错误；文件是否存在、非空且可被对应 GIS 驱动打开，以执行后的 `verified` 结果为唯一依据。
- 直接从 `PyQt5` 或 `PyQt6` 导入 QGIS 运行时类型；必须使用
  `from qgis.PyQt...`，例如 `from qgis.PyQt.QtCore import QVariant`。
- 把 `QgsColorRampShader` 直接传给 `QgsSingleBandPseudoColorRenderer` 构造器或 `renderer.setShader()`；二者要求 `QgsRasterShader`，必须先用 `setRasterShaderFunction()` 包装颜色函数。
- 调用不存在的 `QgsColorRampShader.setColorRampItem()`；必须构造 `QgsColorRampShader.ColorRampItem` 列表并调用 `setColorRampItemList(items)`。
- 调用不存在的 `QgsColorRampShader.setClassificationMin()`/`setClassificationMax()`；应使用构造器或 `setMinimumValue()`/`setMaximumValue()`。
- 创建 Polygon/Line/Point 输出图层，却没有为输出要素调用 `setGeometry`；纯统计结果
  应创建无几何表，要求空间结果时必须保留或聚合真实几何。
- 分组统计中用赋值覆盖分母字段，例如遍历多栋建筑时反复执行
  `land_area = current_land_area`。地块面积必须按唯一地块去重汇总，不能按建筑重复累加，
  也不能只保留最后一个地块。
- 用户指定面积字段作为覆盖率分母，但代码忽略该字段改用几何面积；或者分子使用投影后几何面积、分母使用单位不明的属性面积，却没有验证单位一致。
- 用户只要求按平方米筛选地块面积、并未明确指定面积属性字段，代码却把可能为空或单位不明的 String 字段（例如 `land_area`）直接 `to_real(...)`，而不是在米制投影中用 `$area` 计算几何面积；预期应命中要素的筛选流程也必须检查中间及最终 `featureCount()`，不得把 0 要素文件直接当作成功结果。
- 密度、覆盖率、比例、均值、面积、长度或高度等小数派生字段被定义为文本类型（例如 `QVariant.String`、`native:aggregate`/`native:refactorfields` 映射中的文本类型 `type: 10`，或算法详情标记为 Text/String 的字段枚举），或者向有长度限制的 String 字段写入浮点数。此类字段必须使用 Double，并设置合理的数值长度和精度。
- 代码准备写入已有 `dense` 等派生字段，却没有检查同名字段的实际类型；若原字段是文本型，必须先重构为唯一的 Double 字段，不能仅增加字符串长度或直接写入浮点数。
- 比值计算没有处理 NULL、非数值、分母为 0，或把结果转换成 `str`、`nan`、`inf` 后写入属性表。
- `native:fieldcalculator` 对密度/除法结果使用 `FIELD_TYPE=2`，即使旁边注释声称它是 Double 也必须阻断；该值实际表示 Text/String，Decimal/Double 应使用本次算法证据对应的枚举（当前算法通常为 0）。
- 自动重试脚本只包含失败步骤，出现“中间结果已存在”，或把上一次 `QGIS_AGENT_WORKSPACE` 中的文件作为输入。每次执行使用新的空工作目录，重试必须从原始 QGIS 图层重新执行完整流程。

## GIS 正确性检查

- 图层查找应有空结果判断，例如 `if not matches: raise ValueError(...)`。
- 字段筛选应先读取 `field_names = [field.name() for field in layer.fields()]`。
- 属性表达式中的字段名用双引号，例如 `"leisure" = 'park'`。
- 中文名称模糊匹配优先用 `ILIKE '%公园%'`。
- Processing 输出参数必须是工作目录内路径字符串。
- 输出 vector/raster 后，`expected_outputs.type` 应匹配。
- 属性值探查不是最终产物时，不得虚构 `.txt` 预期输出；优先使用 `inspect_layer`，确需输出诊断文件时必须在代码中真实写入。
- stdout 无文件结果必须是用户所需的最终统计答案，并包含统计口径、单位和数值，不能只是供下一步使用的诊断数据。直接样式调整必须作用于用户指定的当前图层并触发重绘。
- 写 GeoPackage 时优先使用 Processing 算法的 `OUTPUT` 路径；只有取得当前 QGIS 版本精确 API 证据时才允许使用 Writer API。
- 密度、覆盖率等比值必须审查分子与分母的统计粒度一致；按用地类型统计时，分母应为该
  类型唯一地块面积总量，不能使用任意单个地块面积。
- 派生字段审查必须同时核对“表达式返回类型”和“目标字段存储类型”；表达式得到浮点数并不代表文本型目标字段会自动安全转换。

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
