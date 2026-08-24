---
name: analyze-cultivated-land-loss
description: 分析国土变更调查中的耕地流失情况，依据上年度调查、年度增量包、用地管理信息和永久基本农田计算不合理流出、非农化、非粮化及其占永农面积，并生成规定格式的年度统计表。适用于耕地流失情况分析；不用于一般土地利用统计或临时 GIS 代码生成。
allowed-tools: [list_layers, inspect_layers, inspect_cultivated_land_loss_inputs, get_task_context, execute_cultivated_land_loss_analysis]
license: MIT
metadata:
  version: 1.2.0
  lifecycle: one-shot
  author: AI GIS QGIS Plugin
  platforms: [linux, Windows, macos]
  hermes:
    tags: [land-change-survey, cultivated-land, land-loss, non-agriculturalization, non-grain, permanent-farmland, excel]
    requires_tools: [list_layers, inspect_layers, inspect_cultivated_land_loss_inputs, execute_cultivated_land_loss_analysis]
  qgis_agent:
    inputs:
      required:
        previous_survey_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        increment_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        land_management_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        permanent_farmland_layer_id: {type: qgis_vector_layer, geometry: [Polygon, MultiPolygon]}
        previous_land_code_field: {type: field_name}
        increment_land_code_field: {type: field_name}
        management_area_name: {type: string}
        target_crs: {type: crs}
    outputs:
      report_table: {type: table}
      detail_layer: {type: qgis_vector_layer}
      metrics_table: {type: table}
      quality_report: {type: file}
    side_effects:
      modifies_qgis_project: true
      writes_files: true
      requires_confirmation: true
      concurrency: qgis_main_thread_serial
    completion:
      required_artifacts: [report_table, detail_layer, metrics_table, quality_report]
      checks: [output_files_exist, land_codes_complete, metric_reconciliations_pass]
---

# 国土变更调查耕地流失情况分析

使用内置固定脚本完成数据检查、空间叠加、指标汇总和 Excel 模板填报。AI 只绑定当前工程中的真实图层、字段、管理区、面积单位和目标 CRS；不得生成或改写 Python/Processing 代码，也不得切换到 `gis-pipeline`。

## 数据绑定

必须绑定四个面图层：

- 上年度国土变更调查数据；
- 本年度国土变更调查增量包；
- 用地管理信息；
- 永久基本农田。

同时绑定上年度和增量包各自的地类编码字段。图层名、字段名只能用于召回候选，必须通过 `inspect_layers` 检查几何类型、CRS、字段类型和样例值。地类编码必须按字符串处理，不能擅自补前导零或按字符串区间推断类别。

## 固定工作流

1. 调用 `list_layers`，识别四个数据角色。
2. 对候选图层一次调用 `inspect_layers`。
3. 确认四个图层均为面矢量图层，并绑定两个地类编码字段。
4. 调用 `inspect_cultivated_land_loss_inputs` 完整检查代码值域、未知编码、空值、无效几何、CRS 和要素数量。
5. `inspection_complete=true` 且 `blocking_issues=[]` 时继续执行；`data_warnings` 是非阻断提示，固定执行工具会跳过对应异常要素并写入质量报告。
6. 确认管理区名称、目标米制投影 CRS 和面积单位。用户未指定单位时使用 `mu`。
7. 只调用一次 `execute_cultivated_land_loss_analysis`。

面积单位映射：

| 用户单位 | 参数值 |
|---|---|
| 平方米 | `square_meter` |
| 公顷 | `hectare` |
| 亩 | `mu` |
| 平方千米 | `square_kilometer` |

## 统计口径

详细口径见 [指标规则](references/metric_rules_2025.md)，地类集合见 [地类分类配置](references/land_classification_2025.yaml)。关键约束：

- 增量包个数是原始要素条数，空间切割后不得重新计数。
- 分析必须基于图层底层完整要素，不能继承 QGIS 图层的活动子集筛选。固定工具会克隆图层并在分析副本中清除筛选，不修改用户当前图层；忽略的筛选表达式必须写入质量警告。
- 面积全部由投影后几何计算，先以平方米汇总，再转换单位。
- 用地管理信息实际覆盖部分为合理流出，未覆盖部分为不合理流出。
- 新增耕地直接统计增量包中 `0101`、`0102`、`0103` 的几何面积。
- 耕地变化面积固定为“不合理流出面积－新增耕地面积”。
- 本年度耕地面积固定为“上年度耕地面积－不合理流出面积＋新增耕地面积”。
- 要素级数据问题（空地类编码、未知编码、空几何、无法修复或修复后不是面几何）应跳过并记录，不得中断整批分析。增量包个数仍使用原始要素条数，被跳过要素不参与面积计算。
- 最终答复必须明确提示跳过要素总数和各图层数量，并引导用户查看 `cultivated_land_loss_quality.json` 中的完整明细；不得静默忽略数据问题。
- 关键平衡校验失败时不得生成正式成功结论。

## 模板与输出

模板来源为 `assets/xx区2025年度国土变更调查情况统计表.xlsx`，单元格映射见 [模板映射](references/report_mapping_2025.yaml)。固定输出：

- `cultivated_land_loss_analysis.xlsx`：正式统计表；
- `cultivated_land_loss_details.gpkg`：图斑级审计明细；
- `cultivated_land_loss_metrics.csv`：指标汇总；
- `cultivated_land_loss_quality.json`：规则版本、数据质量和平衡校验。

固定脚本使用插件内置的 `openpyxl` 直接填报 OOXML `.xlsx` 模板，保留模板样式、合并单元格、行列尺寸和公式，不依赖 Microsoft Excel、COM 或 QAxContainer。

## 完成答复

工具成功后不得只回复“已执行”。最终答复必须读取工具返回的 `analysis_summary` 和 `data_quality`，至少给出管理区、年度、增量包个数和面积、不合理流出及占永农、非农化及占永农、非粮化及其两个分项、各分项占永农、新增耕地、上年度耕地、耕地变化、本年度耕地、结果文件路径，以及全部警告数量和代表性警告。若关键指标为 0，必须同时说明工具返回的零值原因警告，不得把 0 表述为“无异常”。

## 暂停条件

以下任一情况出现时停止并只询问必要信息：

- 四个数据角色或两个地类字段不能唯一确定；
- 目标 CRS 无效、为地理坐标系或线性单位不是米；
- 任一图层不是面矢量图层；
- 非粮化分项、永农面积上限或年度公式校验失败；
- 模板丢失或内置 `openpyxl` 不可用。
