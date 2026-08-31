---
name: code-generator
description: 生成 QGIS 当前环境可执行代码
tools:
  - record_pipeline_stage
version: 2.1.0
author: AI GIS QGIS Plugin
license: MIT
metadata:
  hermes:
    tags: [gis, pipeline, code]
    requires_tools: [record_pipeline_stage]
---

# Code Generator

生成在当前已打开 QGIS Python 环境中运行的 Python 代码。代码会由 `execute_gis_code` 在当前 QGIS 进程里执行，不要启动新的 QGIS。

## 硬性规则

- 用户要求生成或导出文件时，代码必须把结果写入 `QGIS_AGENT_WORKSPACE`。
- `QGIS_AGENT_WORKSPACE` 是 `execute_gis_code` 执行器注入到运行命名空间和环境变量中的工作目录变量，生成代码中可以直接使用；为了可读性，建议写成 `workspace = Path(QGIS_AGENT_WORKSPACE)`。
- `expected_outputs` 始终使用列表：有文件结果时必须和代码实际输出路径完全一致；无文件结果时使用空数组。
- 仅需面积、数量、最值等统计结论时，可以不创建文件；代码把完整最终结论清晰打印到 stdout，并使用 `expected_outputs=[]`。
- 用户只要求调整当前栅格/矢量图层的符号系统、色带、分类或其他显示样式时，可以直接更新该图层 renderer、触发重绘并使用 `expected_outputs=[]`；不要为了通过检查虚构 QML 或栅格输出文件。
- 单波段伪彩色渲染必须使用正确的三层对象链：`QgsColorRampShader` 是着色函数，先用 `raster_shader = QgsRasterShader()` 和 `raster_shader.setRasterShaderFunction(color_ramp_shader)` 包装，再调用 `QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, raster_shader)`。构造器第三个参数及 `renderer.setShader()` 都严禁直接传 `QgsColorRampShader`。
- `QgsColorRampShader` 没有 `setColorRampItem()`；手工设置色带节点时必须先构造 `QgsColorRampShader.ColorRampItem(value, color, label)` 列表，再一次调用 `color_ramp_shader.setColorRampItemList(items)`。
- `QgsColorRampShader` 没有 `setClassificationMin()`/`setClassificationMax()`；范围应在构造器中传入，或使用继承的 `setMinimumValue()`/`setMaximumValue()`。
- QGIS 4 栅格色带优先使用 `Qgis.ShaderInterpolationMethod.Linear/Discrete/Exact` 和 `Qgis.ShaderClassificationMethod.Continuous/EqualInterval/Quantile`。相等间隔 7 类应设置 `EqualInterval` 后调用 `color_ramp_shader.classifyColorRamp(7, band, layer.extent(), layer.dataProvider())`。
- 连续拉伸可以给 `QgsColorRampShader` 设置最小值、最大值、`Linear` 插值以及深色/浅色端点；分类图则使用独立的颜色函数、`EqualInterval` 和明确类别数。每个 renderer 都创建自己的 `QgsRasterShader`，不要在多个 renderer 间复用已被接管所有权的 shader。
- 用户要求从同一数据生成多种样式的 QGIS 栅格图层、但未要求导出文件时，可以从源数据 URI 创建多个独立 `QgsRasterLayer`，分别设置 renderer 后用 `QgsProject.instance().addMapLayer()` 加入工程，并使用 `expected_outputs=[]`；不要无故复制底层栅格文件。
- 空 `expected_outputs` 只适用于 stdout 本身就是最终答案，或用户明确要求的当前 QGIS 图层/工程状态修改；不得用于字段唯一值探查或为下一次代码生成收集诊断信息。
- 用户要求生成的最终文件，例如 `500m.shp`、`result.gpkg`、`parks.geojson`，必须写入 `expected_outputs`；不要省略 `expected_outputs`，也不要只把文件名写在代码里。
- 用户只说“导出/生成结果”但没有给文件名时，不要询问保存目录；使用合理默认文件名并写入 `expected_outputs`，例如 `park_parcels.geojson`。
- 输出路径用 `Path(QGIS_AGENT_WORKSPACE) / "文件名"` 构造，不要写绝对路径到工作目录外。
- `expected_outputs` 中的 vector/raster 文件会由 `execute_gis_code` 验证并自动加载到 QGIS，生成代码不要再用 `QgsRasterLayer`/`QgsVectorLayer` 和 `QgsProject.addMapLayer()` 手动加载同一最终文件，否则会产生重复图层。只有不生成文件、且用户明确要求创建工程内存图层或样式副本时才自行添加图层。
- 如果用户明确要求导出到外部目录，例如 `E:\Desktop\test`，代码仍然只能写入 `QGIS_AGENT_WORKSPACE`；在 `execute_gis_code` 参数中增加 `delivery_outputs`，把工作目录内输出复制到用户目录。
- 外部导出示例：代码输出 `qn_500_area_8000.geojson`，`expected_outputs=[{"path":"qn_500_area_8000.geojson","name":"qn_500_area_8000","type":"vector"}]`，`delivery_outputs=[{"source_path":"qn_500_area_8000.geojson","target_path":"E:\\Desktop\\test\\qn_500_area_8000.geojson"}]`。
- 可以直接使用当前命名空间中的常用对象：`QgsProject`、`QgsVectorLayer`、`QgsFeature`、`QgsFeatureRequest`、`QgsGeometry`、`QgsVectorFileWriter`、`QgsProcessing`、`QgsProcessingContext`、`QgsProcessingFeedback`、`processing`、`iface`、`Path`。
- 也可以显式导入：`from qgis.core import ...`。
- 如果代码里出现 `QgsProcessing`、`QgsProcessingContext` 或 `QgsProcessingFeedback`，必须确认它们已在当前命名空间或显式导入中可用；更推荐直接把 `OUTPUT` 写成 `str(Path(QGIS_AGENT_WORKSPACE) / "文件名")`，不要用 `QgsProcessing.TEMPORARY_OUTPUT` 作为最终输出。
- 不得创建 `QgsApplication`、`QApplication`，不得调用 `initQgis`，不得启动新的 QGIS。
- 不生成网络访问、`subprocess`、`os.system`、`eval`、`exec`、`sys.exit`、删除文件或写工作目录外路径。
- 记录 `generated_code` artifact：`code`、`expected_outputs`、`dependencies`、`assumptions`、`summary`、`review`。
- 代码中的每个 `processing.run` 必须来自 `solution_plan.algorithm_evidence`，
  参数名必须依据 `get_qgis_processing_tool` 返回的真实参数，不得凭记忆猜测。
- 阶段上下文中的 `processing_algorithm_evidence` 是服务端跨上下文压缩保留的本轮权威详情，包含允许参数和 Catalog 示例。生成代码必须直接依据它；不得把搜索摘要、旧会话知识或代码注释中的说法当成参数证据。
- 多步骤任务生成一份完整脚本；中间结果在脚本内显式衔接，整个任务只调用一次
  `execute_gis_code`。
- 任一输入超过 10 万要素时，不得生成对两个图层执行 `getFeatures()` 的嵌套循环。
- 大图层空间筛选必须使用 Processing/数据源空间索引，并先缩小候选范围再做精确判断。
- 分析查询遇到空几何或无效几何时，使用 `context = QgsProcessingContext()` 和 `context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.GeometrySkipInvalid)` 排除这些要素，并在输出摘要中说明。`InvalidGeometryCheck` 枚举属于 `Qgis`，严禁写成 `QgsProcessingContext.InvalidGeometryCheck`。代码必须导入 `from qgis.core import Qgis, QgsProcessingContext`，或确认两者都在执行命名空间中。除非用户明确要求修复数据，否则禁止生成 `native:fixgeometries`。
- 大数据任务的缓冲筛选默认溶解缓冲区，最终结果优先输出 GeoPackage。
- 耗时 Processing 调用必须保留或传入 `QgsProcessingFeedback`，以支持进度、取消和界面事件刷新。
- 每个用户任务只生成一次面向最终结果的 `execute_gis_code` 调用。不得先生成“打印唯一值”的诊断脚本，不得为 stdout 虚构 `.txt` 输出；字段和值已由用户指定时直接生成最终筛选结果。
- `inspect_layer`/`inspect_layers` 返回的 `layer_id` 只是 QGIS 工程索引键，不能作为字符串直接传给 `processing.run` 的 `INPUT`、`OVERLAY`、`JOIN`、`MASK` 等图层参数。必须先执行 `layer = QgsProject.instance().mapLayer(layer_id)`，随后用 `if layer is None: raise ValueError(...)` 检查，再把真实 `QgsMapLayer` 对象传给 Processing。所有按 ID 获取的输入和范围图层都必须分别检查。
- 不得使用 `mapLayersByName(...)[0]`；先保存 `matches` 并检查非空，再取 `matches[0]`。
- 用户指定的每个最终输出名称必须逐字保留，并在代码变量、实际文件名、加载图层名和 `expected_outputs.name/path` 中一致；不得自行修改数字后缀，例如不能把 `pdst_250` 写成 `pdst_255`。
- 不得用“相似算法”“不同核函数”或自编近似计算静默替代用户明确要求的算法。若 Catalog 中没有语义等价的工具，必须停在方案阶段说明缺口并请求用户确认替代方案；只有结构化需求中记录了用户已批准替代，代码阶段才能实现。
- `processing.run` 的 `OUTPUT` 返回类型取决于输出目标：`"memory:"`、`"TEMPORARY_OUTPUT"` 或 `QgsProcessing.TEMPORARY_OUTPUT` 通常直接返回图层对象，可以调用 `featureCount()`，不得再用 `QgsVectorLayer(..., "memory")` 包装；写入 `.gpkg`、`.shp`、`.geojson` 等文件路径时通常返回路径字符串，不能直接调用 `featureCount()`。文件结果需要计数时用 `QgsVectorLayer(result["OUTPUT"], "result", "ogr")` 验证有效后计数，或省略非必要计数。
- `generated_code` artifact 必须一次性提交完整 JSON。为避免工具参数超过输出预算，代码只保留必要的校验、处理和结果摘要，省略逐步骤 banner、字段列表调试打印及重复注释；不要续写被截断的代码片段。
- `QgsProcessingFeedback` 从 `qgis.core` 导入，不得写成 `processing.QgsProcessingFeedback()`。
- 矢量筛选、裁剪、叠加和导出优先使用已检索的 Processing 算法及 `OUTPUT`。不得凭记忆调用 `QgsVectorFileWriter.create/writeAsVectorFormat*` 的重载签名。
- 不得调用 `QgsProject.addVectorLayer`；最终输出由 `execute_gis_code` 自动加载。不得直接导入 `PyQt5` 或 `PyQt6`，统一使用 `qgis.PyQt`。
- 不猜测 Processing 结果键（例如 `OUTPUT_COUNT`）或算法 ID/参数；只能使用 `algorithm_evidence` 中读取到的真实 outputs 和参数。
- Processing 参数类型也必须与 `algorithm_evidence` 一致：`PREDICATE` 传整数枚举列表，不得传 `"intersects"`/`"within"` 等名称；`JOIN_FIELDS` 传字段名字符串列表，不得传字段索引。
- `native:aggregate` 的 `AGGREGATES` 必须是聚合定义 object 列表，不得传单个 object 或 JSON 字符串。`GROUP_BY` 只控制分组，不会自动成为输出字段；如果下游需要按分组键连接、排序或写表，必须在 `AGGREGATES` 中对分组字段增加 `first_value` 输出并使用明确别名，优先使用 ASCII 内部名，例如 `land_type`。下游 `FIELD`/`FIELD_2` 必须引用该真实输出别名，不能继续引用源字段名。`native:joinattributesbylocation` 的连接图层参数是 `JOIN`，不得混用其他算法的 `OVERLAY`。
- `QgsGeometry` 空几何判断使用 `isEmpty()`，不得调用不存在的 `isGeosEmpty()`。分析流程的无效几何仍交给 `GeometrySkipInvalid` 排除。
- 空间连接、聚合等中间结果可能改名或丢弃字段。后续引用前必须检查实际 `result_layer.fields()`；不得假定 `SHAPE_Area` 等源字段一定存在。如果统计目标来自土地图层，应优先在原土地图层上聚合，不要反向依赖连接后的建筑物字段。
- 用户要求按平方米筛选面/地块的“面积”而没有明确指定属性字段时，面积指投影后几何面积：先统一到合适的米制 CRS，再用 `$area` 计算新的 Double 字段。不得因为存在名为 `land_area`、`area` 等 String 字段就直接 `to_real(...)`；只有 `inspect_layer` 样例已证明该字段非空、可转数值且单位符合需求，并且用户明确要求使用该属性时才可使用。对筛选、相交等预期应命中要素的步骤，必须输出并检查 `featureCount()`；中间或最终结果为 0 时应抛出包含步骤名的明确错误，不能把空文件报告为成功。
- 用户明确指定“面积”等字段作为覆盖率分母时，必须检查该字段存在、值可转为数值且处理空值，并按用户定义汇总；不得悄悄改用 `geometry().area()`。若重叠面积来自投影后几何，必须确认它和面积字段单位一致；单位不明时应在 `structured_query` 阶段澄清，或在方案中明确改为对分子、分母使用同一投影几何口径。
- 不得使用 `??_1` 等乱码或占位字段名。中间输出需自建字段时优先使用 ASCII 内部名，最终 CSV 表头再映射为中文。
- 新增或覆盖派生字段时，必须按业务语义显式定义字段类型。密度、覆盖率、比例、均值、面积、长度、高度和金额等带小数的结果必须是数值型 Double；不得定义成 `QVariant.String`、`native:aggregate`/`native:refactorfields` 字段映射中的文本类型 `type: 10`，也不得选择 Processing 算法详情标记为 Text/String 的字段枚举或先 `str(value)` 再写入。字段的 `length`/`precision` 是数值存储元数据，不能用字符串长度代替数值类型；一般比值可使用长度 20、精度 10，最终仍以输出格式和本次算法证据为准。
- 写入用户指定的已有字段（例如 `dense`）前，必须检查同名字段的实际类型。若已有字段不是数值型，不得把浮点数直接写入该字段，也不得仅扩大文本长度；应在中间结果中删除/重构该字段后创建 Double 字段，或使用已检索的字段重构算法显式转换。最终写出前再次确认该字段为数值型。
- 比值表达式必须处理 NULL、非数值和分母为 0：无有效分母时写入 NULL，不得写入字符串 `"NULL"`、`"nan"` 或 `"inf"`。除非用户明确要求显示格式，不要为了控制小数位把数值转成文本。
- `native:fieldcalculator` 的枚举按该算法自身的证据解释：在当前 QGIS 算法中 `FIELD_TYPE=0` 是 Decimal/Double，`FIELD_TYPE=2` 是 Text/String。不得因注释写了 `# Double` 就把 2 当作 Double；密度或除法表达式使用 2 必须判定为错误。
- 自动修复执行失败时仍必须重新生成包含原始全部步骤的完整脚本。每次 `execute_gis_code` 都使用全新的空 `QGIS_AGENT_WORKSPACE`，上一次失败执行的中间文件不会继承；严禁生成“中间结果已存在，直接执行步骤 N”的局部脚本，严禁读取上一次 `workspace_dir`。重试脚本必须从当前 QGIS 工程原始图层开始重新创建全部中间结果。

## 常用 PyQGIS 函数和对象

- `QgsProject.instance().mapLayers().values()`：遍历当前工程图层。
- `QgsProject.instance().mapLayersByName("图层名")`：按名称查找图层。
- `layer.fields()`：字段列表。
- `[field.name() for field in layer.fields()]`：字段名列表。
- `layer.getFeatures()`：遍历要素。
- `QgsFeatureRequest().setFilterExpression("表达式")`：属性表达式筛选。
- `QgsVectorLayer("Polygon?crs=EPSG:4326", "name", "memory")`：创建内存矢量图层。
- `provider = layer.dataProvider()`：获取数据提供器。
- `provider.addAttributes(source.fields())`：复制字段。
- `provider.addFeatures(features)`：写入要素。
- 矢量文件输出优先通过已验证的 `processing.run(..., {"OUTPUT": output_path})` 完成。
- `QgsProcessing.TEMPORARY_OUTPUT`：适合中间结果；用户要求生成/导出的最终文件仍必须写到 `QGIS_AGENT_WORKSPACE`。纯统计结论可从内存结果计算后打印到 stdout。
- `QgsProcessingFeedback()`：Processing 反馈对象，只有在算法参数确实需要时再使用。
- `processing.run("native:extractbyexpression", {...})`：按表达式提取。
- `processing.run("native:buffer", {...})`：缓冲区。
- `processing.run("native:clip", {...})`：裁剪。
- `processing.run("native:intersection", {...})`：相交。
- `processing.run("native:joinattributesbylocation", {...})`：空间连接。

## Processing 参数示例

以当前 QGIS 的算法详情为准，空间连接的关键参数形状应类似：

```python
processing.run(
    "native:joinattributesbylocation",
    {
        "INPUT": input_layer,
        "PREDICATE": [0],
        "JOIN": join_layer,
        "JOIN_FIELDS": ["SHAPE_Area"],
        "METHOD": 0,
        "DISCARD_NONMATCHING": True,
        "PREFIX": "land_",
        "OUTPUT": output_path,
    },
)
```

其中 `[0]` 只是形状示例；具体枚举数值必须从本次 `inspect_processing_algorithm`/`get_qgis_processing_tool` 的结果取得。

## 推荐模板：分组聚合后按分组键连接

`native:aggregate` 不会因为设置了 `GROUP_BY` 就自动输出分组字段。下面显式把源字段 `用地_1` 物化为内部连接键 `land_type`，再汇总地块面积：

```python
land_agg_result = processing.run(
    "native:aggregate",
    {
        "INPUT": land_layer,
        "GROUP_BY": '"用地_1"',
        "AGGREGATES": [
            {
                "aggregate": "first_value",
                "input": '"用地_1"',
                "name": "land_type",
                "type": 10,
                "length": 100,
                "precision": 0,
            },
            {
                "aggregate": "sum",
                "input": '"Shape_Area"',
                "name": "sum_land_area",
                "type": 6,
                "length": 20,
                "precision": 2,
            },
        ],
        "OUTPUT": "memory:",
    },
    context=context,
    feedback=feedback,
)
land_stats = land_agg_result["OUTPUT"]

land_stats_fields = [field.name() for field in land_stats.fields()]
required_land_stats_fields = {"land_type", "sum_land_area"}
missing = required_land_stats_fields.difference(land_stats_fields)
if missing:
    raise ValueError(f"地块聚合结果缺少字段：{sorted(missing)}")
```

建筑统计结果也应显式输出同名内部键 `land_type`。随后连接必须使用：

```python
processing.run(
    "native:joinattributestable",
    {
        "INPUT": building_stats,
        "FIELD": "land_type",
        "INPUT_2": land_stats,
        "FIELD_2": "land_type",
        "FIELDS_TO_COPY": ["sum_land_area"],
        "METHOD": 1,
        "DISCARD_NONMATCHING": False,
        "PREFIX": "land_",
        "OUTPUT": "memory:",
    },
    context=context,
    feedback=feedback,
)
```

`type`、`length`、`precision` 和其他参数仍必须以本次算法证据及输入字段类型为准，不得只照抄模板。地块面积必须从原始用地图层按唯一地块汇总，不能从“一栋建筑一条记录”的空间连接结果重复累加。

## 推荐模板：计算密度等 Double 派生字段

优先使用已在 `solution_plan.algorithm_evidence` 中读取详情的字段计算算法。下面的 `FIELD_TYPE=0` 仅在本次算法证据确认其含义为 Decimal/Double 时使用，不得脱离算法详情照抄：

```python
dense_result = processing.run(
    "native:fieldcalculator",
    {
        "INPUT": joined_stats,
        "FIELD_NAME": "dense",
        "FIELD_TYPE": 0,  # 本次算法证据必须确认 0 = Decimal/Double
        "FIELD_LENGTH": 20,
        "FIELD_PRECISION": 10,
        "NEW_FIELD": True,
        "FORMULA": (
            'CASE WHEN to_real("sum_land_area") IS NULL OR '
            'to_real("sum_land_area") = 0 OR '
            'to_real("sum_footprint") IS NULL '
            'THEN NULL ELSE to_real("sum_footprint") / '
            'to_real("sum_land_area") END'
        ),
        "OUTPUT": output_path,
    },
    context=context,
    feedback=feedback,
)
```

如果依据精确 API 证据自行创建字段，必须使用数值类型，而不是字符串：

```python
from qgis.PyQt.QtCore import QMetaType, QVariant
from qgis.core import QgsField

double_type = QMetaType.Type.Double if hasattr(QMetaType, "Type") else QVariant.Double
provider.addAttributes([QgsField("dense", double_type, len=20, prec=10)])
layer.updateFields()
dense_index = layer.fields().indexFromName("dense")
if dense_index < 0 or not layer.fields()[dense_index].isNumeric():
    raise ValueError("dense 字段必须是数值型 Double")
```

若输入已经存在文本型 `dense`，不能直接运行上面的新增字段代码造成重名，也不能向原字段写浮点数；先通过方案中已检索的字段重构步骤生成唯一的 Double `dense` 字段。

## 推荐模板：按属性筛选并输出 GeoJSON

适用于“找出建筑物图层中的公园地块”“提取 leisure=park 的地块”等任务。
如果用户只说“导出公园地块”但未提供文件名，默认输出 `park_parcels.geojson`，图层名为“公园地块”。

```python
from pathlib import Path
from qgis.core import QgsProject
import processing

project = QgsProject.instance()
matches = project.mapLayersByName("建筑物")
if not matches:
    raise ValueError("找不到图层：建筑物")

layer = matches[0]
field_names = [field.name() for field in layer.fields()]

candidate_clauses = []
if "leisure" in field_names:
    candidate_clauses.append("\"leisure\" = 'park'")
if "landuse" in field_names:
    candidate_clauses.append("\"landuse\" = 'park'")
if "name" in field_names:
    candidate_clauses.append("\"name\" ILIKE '%公园%'")

if not candidate_clauses:
    raise ValueError("图层中没有 leisure、landuse 或 name 字段，无法识别公园地块")

expression = " OR ".join(candidate_clauses)
output_path = str(Path(QGIS_AGENT_WORKSPACE) / "park_parcels.geojson")

result = processing.run(
    "native:extractbyexpression",
    {
        "INPUT": layer,
        "EXPRESSION": expression,
        "OUTPUT": output_path,
    },
)

print(f"已按表达式筛选：{expression}")
print(f"输出文件：{result['OUTPUT']}")
```

对应 `expected_outputs`：

```json
[
  {"path": "park_parcels.geojson", "name": "公园地块", "type": "vector"}
]
```

## 推荐模板：缓冲区

```python
from pathlib import Path
from qgis.core import QgsProject
import processing

matches = QgsProject.instance().mapLayersByName("道路")
if not matches:
    raise ValueError("找不到图层：道路")
layer = matches[0]
output_path = str(Path(QGIS_AGENT_WORKSPACE) / "road_buffer.gpkg")

processing.run(
    "native:buffer",
    {
        "INPUT": layer,
        "DISTANCE": 50,
        "SEGMENTS": 8,
        "END_CAP_STYLE": 0,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2,
        "DISSOLVE": False,
        "OUTPUT": output_path,
    },
)
```

## 推荐模板：裁剪

```python
from pathlib import Path
from qgis.core import QgsProject
import processing

input_matches = QgsProject.instance().mapLayersByName("建筑物")
overlay_matches = QgsProject.instance().mapLayersByName("研究区")
if not input_matches or not overlay_matches:
    raise ValueError("找不到建筑物或研究区图层")
input_layer = input_matches[0]
overlay_layer = overlay_matches[0]
output_path = str(Path(QGIS_AGENT_WORKSPACE) / "buildings_clip.gpkg")

processing.run(
    "native:clip",
    {
        "INPUT": input_layer,
        "OVERLAY": overlay_layer,
        "OUTPUT": output_path,
    },
)
```

## 生成代码前自检

- 是否找到了图层？找不到时抛出清晰 `ValueError`。
- 是否已把每个 QGIS `layer_id` 解析为 `QgsMapLayer` 对象、检查不是 `None`，并将对象而不是 ID 字符串传入 Processing？
- 是否检查字段存在？字段不存在时不要静默输出空结果。
- 是否把输出写进 `QGIS_AGENT_WORKSPACE`？
- `expected_outputs.path` 是否和代码里的文件名一致？
- 用户要求外部目录时，是否使用 `delivery_outputs` 而不是让代码直接写外部路径？
- 是否包含用户要求的最终输出文件，例如 `500m.shp`？
- 用户指定的输出名称和数字后缀是否逐字一致，且没有用近似算法替代原操作？
- 输出类型是否是 `vector`、`raster`、`table` 或 `file`？
- 是否避免了 `QgsApplication`、`subprocess`、`eval`、`exec`、删除文件？
- `PREDICATE`、`JOIN_FIELDS`、`AGGREGATES` 的数值类型是否与算法详情完全一致？
- 后续使用中间结果字段前，是否检查了该结果的实际字段名？
- 密度、比例等派生字段是否为 Double，且没有复用同名 String 字段或把数值转成字符串？
- 比值是否处理了 NULL、非数值和分母为 0？
- 这是重试代码时，是否仍包含失败步骤之前的全部步骤，并且没有假设上次工作目录中的中间文件存在？
