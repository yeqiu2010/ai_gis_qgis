---
name: code-generator
description: 生成 QGIS 当前环境可执行代码
tools:
  - record_pipeline_stage
version: 1.1.3
tags: [gis, pipeline, code]
---

# Code Generator

生成在当前已打开 QGIS Python 环境中运行的 Python 代码。代码会由 `execute_gis_code` 在当前 QGIS 进程里执行，不要启动新的 QGIS。

## 硬性规则

- 代码必须把结果写入 `QGIS_AGENT_WORKSPACE`。
- `QGIS_AGENT_WORKSPACE` 是 `execute_gis_code` 执行器注入到运行命名空间和环境变量中的工作目录变量，生成代码中可以直接使用；为了可读性，建议写成 `workspace = Path(QGIS_AGENT_WORKSPACE)`。
- `expected_outputs` 必须和代码实际输出路径完全一致。
- 用户要求生成的最终文件，例如 `500m.shp`、`result.gpkg`、`parks.geojson`，必须写入 `expected_outputs`；不要省略 `expected_outputs`，也不要只把文件名写在代码里。
- 输出路径用 `Path(QGIS_AGENT_WORKSPACE) / "文件名"` 构造，不要写绝对路径到工作目录外。
- 可以直接使用当前命名空间中的常用对象：`QgsProject`、`QgsVectorLayer`、`QgsFeature`、`QgsFeatureRequest`、`QgsGeometry`、`QgsVectorFileWriter`、`QgsProcessing`、`QgsProcessingContext`、`QgsProcessingFeedback`、`processing`、`iface`、`Path`。
- 也可以显式导入：`from qgis.core import ...`。
- 如果代码里出现 `QgsProcessing`、`QgsProcessingContext` 或 `QgsProcessingFeedback`，必须确认它们已在当前命名空间或显式导入中可用；更推荐直接把 `OUTPUT` 写成 `str(Path(QGIS_AGENT_WORKSPACE) / "文件名")`，不要用 `QgsProcessing.TEMPORARY_OUTPUT` 作为最终输出。
- 不得创建 `QgsApplication`、`QApplication`，不得调用 `initQgis`，不得启动新的 QGIS。
- 不生成网络访问、`subprocess`、`os.system`、`eval`、`exec`、`sys.exit`、删除文件或写工作目录外路径。
- 记录 `generated_code` artifact：`code`、`expected_outputs`、`dependencies`、`assumptions`、`summary`、`review`。

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
- `QgsVectorFileWriter.writeAsVectorFormatV3(...)`：保存矢量文件。
- `QgsProcessing.TEMPORARY_OUTPUT`：仅适合中间结果；最终结果必须写到 `QGIS_AGENT_WORKSPACE`。
- `QgsProcessingFeedback()`：Processing 反馈对象，只有在算法参数确实需要时再使用。
- `processing.run("native:extractbyexpression", {...})`：按表达式提取。
- `processing.run("native:buffer", {...})`：缓冲区。
- `processing.run("native:clip", {...})`：裁剪。
- `processing.run("native:intersection", {...})`：相交。
- `processing.run("native:joinattributesbylocation", {...})`：空间连接。

## 推荐模板：按属性筛选并输出 GeoJSON

适用于“找出建筑物图层中的公园地块”“提取 leisure=park 的地块”等任务。

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

layer = QgsProject.instance().mapLayersByName("道路")[0]
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

input_layer = QgsProject.instance().mapLayersByName("建筑物")[0]
overlay_layer = QgsProject.instance().mapLayersByName("研究区")[0]
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
- 是否包含用户要求的最终输出文件，例如 `500m.shp`？
- 输出类型是否是 `vector`、`raster`、`table` 或 `file`？
- 是否避免了 `QgsApplication`、`subprocess`、`eval`、`exec`、删除文件？
