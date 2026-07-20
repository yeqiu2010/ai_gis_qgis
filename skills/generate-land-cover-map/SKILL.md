---
name: generate-land-cover-map
description: 使用 QGIS 从分类遥感影像、土地覆盖栅格或带分类字段的矢量数据生成土地覆盖专题图，并可先按行政区、研究区、规划范围等面图层裁剪再制图。适用于土地利用/土地覆盖制图、分类结果配色、LULC 地图、地类图例、打印布局，以及“用武汉/wuhan边界裁剪CLCD后制作专题图”“按某范围裁剪土地覆盖影像并制图”等请求；即使请求同时包含裁剪和制图，也应优先使用本 Skill 而不是 gis-pipeline。支持全图默认范围、可选裁剪范围、内置九类配色或用户自定义类别—名称—颜色映射，并创建含标题、图例、指南针、比例尺的 QGIS 布局及 PNG/PDF 成图。
allowed-tools: list_layers inspect_layers inspect_land_cover_map_inputs get_task_context generate_land_cover_map
metadata:
  version: 1.3.0
  lifecycle: one-shot
  tags: [land-cover, lulc, thematic-map, raster, vector, layout, qgis]
---

# 土地覆盖专题图

使用内置固定脚本完成“输入检查—可选范围裁剪—分类映射确认—图层渲染—打印布局—PNG/PDF 导出”。只绑定参数，不得生成或传入 Python 代码，不得切换到 `gis-pipeline`。

## 固定工作流

1. 调用 `list_layers` 获取当前工程图层。
2. 调用 `inspect_layers` 检查候选分类影像或矢量图层；用户指定范围时同时检查范围面图层。
3. 矢量数据必须确定 `classification_field`；栅格数据必须确定 `raster_band`，默认第 1 波段。
4. 必须调用 `inspect_land_cover_map_inputs`。将可选范围面图层 ID 绑定到 `boundary_layer_id`；矢量模式使用其完整真实值域绑定类别，栅格模式使用其波段检查结果。
5. 确认类别映射与版式参数后，只调用一次 `generate_land_cover_map`。

## 默认分类映射

用户没有自定义映射时使用：

| ID | Class | RGB |
|---:|---|---|
| 1 | Cropland | 250,227,156 |
| 2 | Forest | 68,111,51 |
| 3 | Shrub | 51,160,44 |
| 4 | Grassland | 171,211,123 |
| 5 | Water | 30,105,180 |
| 6 | Snow/Ice | 166,206,227 |
| 7 | Barren | 207,189,163 |
| 8 | Impervious | 226,66,144 |
| 9 | Wetland | 40,155,232 |

将用户给出的 `Sonw/Ice` 视为 `Snow/Ice` 的拼写误差，仅修正图例标签，不改变 ID 6 和颜色。

`class_mapping` 中每项必须包含真实类别值、图例名称和 RGB：

```json
{
  "value": 1,
  "label": "Cropland",
  "color": [250, 227, 156]
}
```

矢量分类值必须逐字、按真实数据类型来自 `classification_domain.available_values`。若字段值是 `Cropland`、`Forest` 等文本，则把 `value` 改为真实文本，但沿用对应默认颜色。默认必须覆盖所有非空真实值；用户明确只显示部分类型时才设置 `allow_unmapped_values=true`。编码含义不明或自定义映射不完整时必须询问。

栅格默认按数值 1–9 分类。用户说明其他像元编码、名称或颜色时，完整替换 `class_mapping`；不得根据自然语言自行发明编码。

## 制图范围

- 默认不传 `boundary_layer_id`，对土地覆盖图层全范围制图。
- 用户提出“按某行政区/研究区/边界裁剪”“与某图层对齐裁剪后制图”时，必须把该面图层真实 ID 传入 `boundary_layer_id`，并由本 Skill 一次完成裁剪与制图。
- 栅格输入使用面掩膜裁剪，保持原始像元分辨率和分类值；矢量输入使用面叠加裁剪。
- 裁剪范围必须是具有有效 CRS 的非空面图层，并与土地覆盖数据空间相交；不满足时暂停并说明检查错误。
- 不得因为请求包含“裁剪 + 制图”两个步骤切换到 `gis-pipeline` 或 `qgis-toolbox`。

## 版式参数

- `title`：默认“土地覆盖专题图”，优先使用用户标题。
- `subtitle`：可选副标题。
- `legend_title`：用户未指定时，根据分类标签语言自动使用“图例”、`Legend`、`凡例` 或 `범례`；其他语言由 AI 传入对应译名。
- `orientation`：`landscape` 或 `portrait`，默认横版。
- `legend_position`：`right` 或 `bottom`，默认右侧。
- `show_legend`、`show_north_arrow`、`show_scale_bar`：默认均为 `true`。
- `title_font_size`：8–48，默认 20。
- `map_margin_percent`：地图范围外扩百分比，默认 5。

图例固定只显示类别色块和 `class_mapping.label`，不显示图层名、栅格波段名、字段名等层级信息。底部图例自动使用三列，减少拥挤。

比例尺固定依据最终制图图层范围计算：较小范围使用米，达到 10 千米以上的可视宽度时使用千米；在可用地图宽度内自动调整分段宽度，避免标签或比例尺图形被挤压。

用户要求的调整超出以上参数时，说明固定模板当前支持范围并询问是否使用最接近配置，不得改写脚本。

## 调用示例

分类栅格：

```json
{
  "input_layer_id": "分类影像真实 ID",
  "boundary_layer_id": "可选范围面图层真实 ID",
  "source_type": "raster",
  "raster_band": 1,
  "title": "2025 年土地覆盖专题图",
  "orientation": "landscape",
  "legend_position": "right"
}
```

分类矢量：

```json
{
  "input_layer_id": "土地覆盖矢量真实 ID",
  "source_type": "vector",
  "classification_field": "Class_ID",
  "class_mapping": [
    {"value": 1, "label": "Cropland", "color": [250, 227, 156]},
    {"value": 2, "label": "Forest", "color": [68, 111, 51]}
  ],
  "title": "土地覆盖专题图"
}
```

## 固定结果

固定脚本将：

- 未指定范围时为原输入图层应用分类渲染；指定范围时生成并渲染 `land_cover_clipped.tif` 或 `land_cover_clipped.gpkg`，隐藏原始大图层并刷新 QGIS 地图画布。
- 在当前 QGIS 工程中创建名为“土地覆盖专题图 - 图层名”的打印布局；同名旧布局会被替换。
- 添加标题、可选副标题、只含类别色块与解释的同语言图例、`arrows/NorthArrow_04.svg` 指南针、自适应比例尺和地图框。
- 在 QGIS 中打开布局设计器。
- 生成 `land_cover_map.png`（300 DPI）和 `land_cover_map.pdf`。

执行成功后根据真实 stdout 和 outputs 说明输入类型、分类字段或波段、类别数量、版式配置、QGIS 布局名称及导出文件。

## 暂停条件

出现以下任一情况时停止并询问：

- 无法唯一识别输入图层。
- 用户要求范围裁剪但无法唯一识别范围图层，或范围图层不是有效的非空面图层。
- 矢量分类字段不存在、有多个合理候选或真实值域检查失败。
- 栅格波段不存在，或像元类别编码与映射关系不明。
- 用户自定义颜色缺少类别、名称或有效 RGB。
- 默认映射不能覆盖实际类别且用户没有说明处理方式。

严禁调用 `execute_gis_code`、Processing 搜索工具或修改固定脚本。不要询问保存目录；输出由固定工具管理。
