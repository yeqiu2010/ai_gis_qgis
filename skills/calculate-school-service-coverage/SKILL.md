---
name: calculate-school-service-coverage
description: 计算中小学或学校设施服务半径对住宅区、居民区、社区或居住用地的要素覆盖率，并按街道、乡镇或行政区分组汇总。适用于学校类型筛选、教育设施服务半径、500 米覆盖范围、居住区覆盖率、服务盲区和分街道统计等任务。图层名、字段名、类别值和距离可以不同，必须根据当前 QGIS 工程动态绑定。
tools:
  - list_layers
  - inspect_layers
  - inspect_school_service_coverage_inputs
  - get_task_context
  - execute_school_service_coverage
version: 2.1.0
lifecycle: one-shot
tags: [school, education, service-area, coverage, residential, street-statistics]
---

# 中小学服务半径覆盖率

使用内置固定脚本完成“学校筛选—服务区生成—居住区覆盖率—行政区汇总”。AI 只检查数据并绑定参数；不得生成、改写或传入 Python/Processing 代码，不得切换到 `gis-pipeline`。

## 数据角色

从用户说明和当前 QGIS 工程中绑定：

- `school_layer_id`：学校或教育设施图层 ID，图层可以是点或面。
- `residential_layer_id`：住宅区、居民区、社区或居住用地面图层 ID。
- `school_type_field`：学校类别字段。
- `school_type_values`：用户目标学校类型在当前数据中的真实精确字段值，只能从专用检查工具返回的值域中选择。
- `residential_area_field`：用户指定的居住区总面积数值字段。
- `area_unit`：面积字段单位。
- `group_field`：街道、乡镇或行政区字段。
- `service_distance_m`：以米为单位的服务半径。
- `target_crs`：适合研究区且线性单位为米的投影 CRS。

不得假定图层固定名为“学校”或“城镇住宅区”，也不得假定字段固定名为 `CCN`、`面积` 或 `XZQMC`。

数据绑定证据优先级：

1. 用户明确指定并经工具检查确认的图层、字段和值。
2. 字段类型、样例值、几何类型和 CRS。
3. 图层名或字段名的语义相似性只能用于召回候选。

字段名候选可以包括：

- 学校类型：`CCN`、`school_type`、`type`、`category`、`学校类别`、`办学类型`。
- 面积：`面积`、`area`、`area_m2`、`shape_area`、`res_area`。
- 分组：`XZQMC`、`street`、`street_name`、`town`、`township`、`街道`、`乡镇`。

存在多个合理候选时必须询问用户，不得自行选择。

## 工作流

### 1. 检查数据

1. 调用 `list_layers` 获取当前工程图层。
2. 对候选学校和居住区图层一次调用 `inspect_layers`。
3. 确认图层 ID、几何类型、CRS、字段名称、字段类型和样例值。
4. 确认学校图层是点或面，居住区图层是面。
5. 确认面积字段为数值型，分组字段真实存在。
6. 确认学校类型字段后，必须调用 `inspect_school_service_coverage_inputs` 获取该字段完整的实际唯一值及计数。不得仅根据 `inspect_layers` 的少量样例确定类别值。

`school_type_values` 必须遵循以下证据约束：

1. 只能逐字使用 `available_values` 中存在的值，不得自行创造、拆分、合并或改写类别。
2. 用户目标词与某个真实值完全一致时，精确匹配优先且只选择该值。例如用户说“中小学”，实际值为 `["中小学", "高等院校", "幼托机构"]`，必须选择 `["中小学"]`，不得扩展为 `["小学", "初中", "九年一贯制"]`。
3. 没有精确匹配时，AI 只能在 `available_values` 内做语义判断。只有一个明确候选时可选择；存在多个合理候选时，列出候选及计数并询问用户。
4. 没有合理候选、值域检查失败或 `domain_complete` 不为 `true` 时必须停止并询问，不得调用执行工具。

如果学校类别使用无法解释的编码，例如 `01`、`02`，不得猜测哪些编码代表中小学。专用检查工具只负责提供真实值域，编码含义仍不明确时必须询问用户。不得调用代码执行工具打印唯一值做诊断。

### 2. 确认计算参数

面积单位必须映射为以下枚举之一：

| 用户单位 | `area_unit` |
|---|---|
| 平方米 | `square_meter` |
| 公顷 | `hectare` |
| 亩 | `mu` |
| 平方千米 | `square_kilometer` |

面积字段单位不明时必须询问用户，不得默认使用平方米。固定脚本会把分母统一转换为平方米。

`target_crs` 必须是适合研究区、线性单位为米的投影 CRS。输入图层使用地理坐标系或不同 CRS 时，由固定脚本统一重投影。

默认服务半径为 500 米；用户明确给出其他距离时使用用户值。

### 3. 调用固定工具

完成真实值域检查且信息完整后，只调用一次 `execute_school_service_coverage`。例如用户要求“中小学”、专用检查工具返回的实际值域为 `["中小学", "高等院校", "幼托机构"]`，且用户指定 1000 米服务半径时：

```json
{
  "school_layer_id": "学校图层真实 ID",
  "residential_layer_id": "城镇住宅区图层真实 ID",
  "school_type_field": "CCN",
  "school_type_values": ["中小学"],
  "residential_area_field": "面积",
  "area_unit": "square_meter",
  "group_field": "XZQMC",
  "service_distance_m": 1000,
  "target_crs": "EPSG:4547"
}
```

严禁：

- 调用 `search_qgis_processing_tools` 或 `get_qgis_processing_tool`。
- 调用 `execute_gis_code`。
- 在工具参数中生成或传递代码、表达式、算法 ID、输出路径或 `expected_outputs`。
- 修改原始图层。
- 在参数不完整时先执行再根据错误猜测。

工具会固定生成：

- `school_service_coverage.gpkg`：居住区空间结果，增加 `svc_cov_m2`、`svc_rate`、`svc_pct`。
- `school_service_coverage_by_group.csv`：按街道、乡镇或行政区统计结果。

## 固定计算口径

单个居住区覆盖率：

```text
feature_coverage = covered_area_m2 / residential_total_area_m2
```

分组覆盖率：

```text
group_coverage =
sum(covered_area_m2 for valid unique residential features)
/
sum(residential_total_area_m2 for valid unique residential features)
```

固定脚本保证：

- 目标学校使用确认后的精确字段值筛选。
- 学校缓冲区先溶解，重叠服务区不会重复计数。
- 每个居住区直接与溶解服务区求交，不产生分母重复累计。
- 面积、覆盖率和百分比使用 Double。
- 分母为 NULL、非数值、零或负数时写入 NULL 并统计异常。
- 空几何和无效几何不参与有效覆盖率统计。
- 分组字段为空时归入“未分组”。
- 覆盖率大于 1 时不截断，并在摘要中报告异常数量。
- 分组统计使用分子总和除以分母总和，不计算覆盖率简单平均值。

## 暂停条件

出现以下任一情况时停止并询问必要问题：

- 无法唯一识别学校或居住区图层。
- 存在多个同等可能的学校类型、面积或分组字段。
- 无法确认代表目标学校类型的精确字段值。
- 居住区不是面图层。
- 面积字段不是数值型或单位不明。
- 用户指定的图层或字段不存在。
- 无法确定适合研究区的米制投影 CRS。

不要询问保存目录；输出文件名和工作目录由固定工具管理。

## 结果说明

工具执行成功后根据真实 stdout 和 outputs 说明：

- 使用的图层、字段、学校类型值、面积单位、服务半径和目标 CRS。
- 参与计算的学校数。
- 居住区总数、有效数、无效面积数和无效几何数。
- 总居住区面积、总覆盖面积和总体覆盖率。
- 分组数量和异常覆盖率数量。
- 生成并加载的空间结果及分组统计表。
