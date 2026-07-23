---
name: calculate-land-use-building-metrics
description: 按用地类型统计建筑单体数量、建筑高度、建筑占地面积、建筑面积总量和建筑密度。适用于“各类用地建筑量”“不同用地性质中的建筑规模”“分用地类型建筑密度”等任务；支持默认统计完整用地与建筑图层，也支持使用街道、行政区或研究区面图层限定计算范围。图层名和字段名可以不同，必须根据当前 QGIS 工程动态绑定。
tools: [list_layers, inspect_layers, inspect_land_use_building_metrics_inputs, get_task_context, execute_land_use_building_metrics]
version: 1.0.0
lifecycle: one-shot
author: AI GIS QGIS Plugin
license: MIT
platforms: [linux, Windows, macos]
metadata:
  hermes:
    tags: [land-use, building, height, floor-area, footprint, density, boundary]
    requires_tools: [list_layers, inspect_layers, inspect_land_use_building_metrics_inputs, execute_land_use_building_metrics]
  qgis_agent:
    inputs:
      required:
        land_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        building_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        land_type_field: {type: field_name}
        land_area_field: {type: field_name}
        land_area_unit: {type: enum}
        building_height_field: {type: field_name}
        building_footprint_field: {type: field_name}
        building_floor_area_field: {type: field_name}
        building_area_unit: {type: enum}
        target_crs: {type: crs}
    outputs:
      metrics_layer: {type: qgis_vector_layer}
      metrics_table: {type: table}
    side_effects:
      modifies_qgis_project: true
      writes_files: true
      requires_confirmation: true
      concurrency: qgis_main_thread_serial
    completion:
      required_artifacts: [metrics_layer, metrics_table]
      checks: [output_layer_exists, statistics_fields_complete]
---

# 各类用地建筑量与建筑密度

使用内置固定脚本完成“范围确认—建筑唯一归属—分用地类型汇总—建筑密度计算”。只检查数据并绑定参数；不得生成、改写或传入 Python/Processing 代码，不得切换到 `gis-pipeline`。

## 数据绑定

绑定以下参数：

- `land_layer_id`：各类用地面图层。
- `building_layer_id`：建筑单体面图层。
- `land_type_field`：用地类型字段。
- `land_area_field`：地块面积字段。
- `land_area_unit`：地块面积字段单位。
- `building_height_field`：建筑高度字段。
- `building_footprint_field`：建筑占地面积字段。
- `building_floor_area_field`：建筑面积字段。
- `building_area_unit`：建筑占地面积和建筑面积字段的共同单位。
- `scope_mode`：`full_layer` 或 `boundary_layer`。
- `boundary_layer_id`：可选的街道、行政区或研究区面图层。
- `target_crs`：适合研究区且线性单位为米的投影 CRS。

不得假定图层和字段固定命名为“各类用地”“建筑单体”、`用地_1`、`Shape_Area`、`HEIGHT`、`FAREA` 或 `GBAREA`。名称只用于召回候选，必须通过工具检查实际字段、字段类型、样例值和几何类型。多个候选同等合理时必须询问用户。

## 固定工作流

1. 调用 `list_layers` 获取当前工程图层。
2. 对候选用地、建筑和可选边界图层一次调用 `inspect_layers`。
3. 确认用地和建筑均为面图层；使用边界时确认边界也是面图层。
4. 确认用地类型字段，以及地块面积、建筑高度、建筑占地面积、建筑面积四个数值字段。
5. 必须调用 `inspect_land_use_building_metrics_inputs`，获取真实用地类型值域、数值字段质量和边界验证结果。只有 `inspection_complete=true` 时才能继续。
6. 确认两个面积单位和目标 CRS 后，只调用一次 `execute_land_use_building_metrics`。

面积单位映射：

| 用户单位 | 参数值 |
|---|---|
| 平方米 | `square_meter` |
| 公顷 | `hectare` |
| 亩 | `mu` |
| 平方千米 | `square_kilometer` |

单位不明时必须询问，不得默认平方米。建筑高度保留源字段数值单位，结果说明中不得擅自声称为米。

## 计算范围

- 用户未指定范围时使用 `scope_mode=full_layer`，统计完整用地图层和建筑图层。
- 用户明确说“在某街道图层中”“以某行政区图层为范围”时，绑定该面图层并使用 `scope_mode=boundary_layer`。
- 边界图层的全部有效面会先合并为一个计算范围。
- 如果用户只给出街道名称，而候选边界图层包含多个街道且没有单独的目标街道图层，必须询问用户或要求先得到目标街道面图层；不得把整个多街道图层当作目标范围。
- 边界切过地块时，地块面积字段按“边界内几何面积 ÷ 原地块几何面积”同比例折算。

全图层示例：

```json
{
  "land_layer_id": "各类用地真实 ID",
  "building_layer_id": "建筑单体真实 ID",
  "land_type_field": "用地_1",
  "land_area_field": "Shape_Area",
  "land_area_unit": "square_meter",
  "building_height_field": "HEIGHT",
  "building_footprint_field": "FAREA",
  "building_floor_area_field": "GBAREA",
  "building_area_unit": "square_meter",
  "scope_mode": "full_layer",
  "target_crs": "EPSG:4547"
}
```

指定“汉阳区 xx 街道”边界图层时，只改为：

```json
{
  "scope_mode": "boundary_layer",
  "boundary_layer_id": "汉阳区 xx 街道图层真实 ID"
}
```

## 固定统计口径

使用建筑面的面内点将每栋建筑唯一归属到一个用地地块和一种用地类型。不得使用普通空间相交把同一栋跨界建筑重复计入多个类型。面内点同时落入多个重叠用地面时，选择与建筑相交面积最大的地块；仍相同时按要素 ID 确定唯一结果。

对每种用地类型输出：

- 用地地块数、有效地块面积总量。
- 建筑单体数。
- 建筑高度有效数、总和、平均值、最小值和最大值。
- 建筑占地面积总量。
- 建筑面积总量。
- 建筑密度及百分比。

建筑密度固定为：

```text
building_density =
sum(building_footprint_area_m2 assigned to the land type)
/
sum(valid_land_parcel_area_m2 of the land type)
```

建筑高度、占地面积和建筑面积分别独立验证。某个字段无效时只排除该项指标，不丢弃整栋建筑的其他有效指标。地块面积为 NULL、非数值、零或负数时不进入密度分母。无效几何不参与空间归属并单独计数。空用地类型归入“未分类”。没有建筑但分母有效的用地类型密度为 0。密度大于 1 时不截断，并报告异常类型数量。

## 工具限制与输出

严禁调用 `search_qgis_processing_tools`、`get_qgis_processing_tool` 或 `execute_gis_code`，不得传递代码、表达式、算法 ID、输出路径或 `expected_outputs`，不得修改原始图层。

固定输出：

- `land_use_building_metrics.gpkg`：每种用地类型一个汇总面要素及核心指标。
- `land_use_building_metrics.csv`：完整统计、有效数和异常数。

执行成功后根据真实 stdout 和 outputs 说明计算范围、绑定字段、单位、各类型数量、未归属建筑数、无效几何数、重叠归属数和生成结果。

## 暂停条件

出现以下任一情况时停止并询问必要问题：

- 无法唯一识别用地、建筑或用户指定的边界图层。
- 必要字段存在多个同等合理候选或不存在。
- 用地、建筑或边界不是面图层。
- 面积字段单位不明，或 `FAREA` 与 `GBAREA` 单位不同。
- 数值质量检查失败或用地类型值域明显不合理。
- 无法确定适合研究区的米制投影 CRS。

不要询问保存目录；输出文件名和工作目录由固定工具管理。
