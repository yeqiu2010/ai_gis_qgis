---
name: code-generator
description: 生成 QGIS 当前环境可执行代码
tools:
  - record_pipeline_stage
version: 2.0.0
tags: [gis, pipeline, code]
---

# Code Generator

生成在当前已打开 QGIS Python 环境中运行的 Python 代码。代码会由 `execute_gis_code` 在当前 QGIS 进程里执行，不要启动新的 QGIS。

## 硬性规则

- 代码必须把结果写入 `QGIS_AGENT_WORKSPACE`。
- `QGIS_AGENT_WORKSPACE` 是 `execute_gis_code` 执行器注入到运行命名空间和环境变量中的工作目录变量，生成代码中可以直接使用；为了可读性，建议写成 `workspace = Path(QGIS_AGENT_WORKSPACE)`。
- `expected_outputs` 必须和代码实际输出路径完全一致。
- 用户要求生成的最终文件，例如 `500m.shp`、`result.gpkg`、`parks.geojson`，必须写入 `expected_outputs`；不要省略 `expected_outputs`，也不要只把文件名写在代码里。
- 用户只说“导出/生成结果”但没有给文件名时，不要询问保存目录；使用合理默认文件名并写入 `expected_outputs`，例如 `park_parcels.geojson`。
- 输出路径用 `Path(QGIS_AGENT_WORKSPACE) / "文件名"` 构造，不要写绝对路径到工作目录外。
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
- 多步骤任务生成一份完整脚本；中间结果在脚本内显式衔接，整个任务只调用一次
  `execute_gis_code`。
- 任一输入超过 10 万要素时，不得生成对两个图层执行 `getFeatures()` 的嵌套循环。
- 大图层空间筛选必须使用 Processing/数据源空间索引，并先缩小候选范围再做精确判断。
- 分析查询遇到空几何或无效几何时，使用 `context = QgsProcessingContext()` 和 `context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.GeometrySkipInvalid)` 排除这些要素，并在输出摘要中说明。`InvalidGeometryCheck` 枚举属于 `Qgis`，严禁写成 `QgsProcessingContext.InvalidGeometryCheck`。代码必须导入 `from qgis.core import Qgis, QgsProcessingContext`，或确认两者都在执行命名空间中。除非用户明确要求修复数据，否则禁止生成 `native:fixgeometries`。
- 大数据任务的缓冲筛选默认溶解缓冲区，最终结果优先输出 GeoPackage。
- 耗时 Processing 调用必须保留或传入 `QgsProcessingFeedback`，以支持进度、取消和界面事件刷新。
- 每个用户任务只生成一次面向最终结果的 `execute_gis_code` 调用。不得先生成“打印唯一值”的诊断脚本，不得为 stdout 虚构 `.txt` 输出；字段和值已由用户指定时直接生成最终筛选结果。
- 不得使用 `mapLayersByName(...)[0]`；先保存 `matches` 并检查非空，再取 `matches[0]`。
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
- 用户明确指定“面积”等字段作为覆盖率分母时，必须检查该字段存在、值可转为数值且处理空值，并按用户定义汇总；不得悄悄改用 `geometry().area()`。若重叠面积来自投影后几何，必须确认它和面积字段单位一致；单位不明时应在 `structured_query` 阶段澄清，或在方案中明确改为对分子、分母使用同一投影几何口径。
- 不得使用 `??_1` 等乱码或占位字段名。中间输出需自建字段时优先使用 ASCII 内部名，最终 CSV 表头再映射为中文。

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
- `QgsProcessing.TEMPORARY_OUTPUT`：仅适合中间结果；最终结果必须写到 `QGIS_AGENT_WORKSPACE`。
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
- 是否检查字段存在？字段不存在时不要静默输出空结果。
- 是否把输出写进 `QGIS_AGENT_WORKSPACE`？
- `expected_outputs.path` 是否和代码里的文件名一致？
- 用户要求外部目录时，是否使用 `delivery_outputs` 而不是让代码直接写外部路径？
- 是否包含用户要求的最终输出文件，例如 `500m.shp`？
- 输出类型是否是 `vector`、`raster`、`table` 或 `file`？
- 是否避免了 `QgsApplication`、`subprocess`、`eval`、`exec`、删除文件？
- `PREDICATE`、`JOIN_FIELDS`、`AGGREGATES` 的数值类型是否与算法详情完全一致？
- 后续使用中间结果字段前，是否检查了该结果的实际字段名？
