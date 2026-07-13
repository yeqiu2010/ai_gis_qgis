---
name: solution-planner
description: GIS 处理方案规划
tools:
  - search_qgis_toolbox_domains
  - search_qgis_processing_tools
  - get_qgis_processing_tool
  - record_pipeline_stage
version: 2.0.0
tags: [gis, pipeline, plan]
---

# Solution Planner

生成可审查、可执行、可恢复的 GIS 方案。

## 必须产出的 artifact 字段

- `steps`：有序步骤，每步包含 `name`、`input`、`algorithm`、`parameters`、`output`、`checks`。
- `algorithms`：使用的 QGIS Processing 算法 ID，例如 `native:extractbyexpression`、`native:buffer`、`native:extractbylocation`。
- `crs_strategy`：距离/面积任务的 CRS 策略。
- `intermediate_outputs`：中间结果，建议使用内存结果或工作目录临时文件。
- `final_outputs`：最终输出，必须与用户要求文件名和 `expected_outputs` 一致。
- `risks`：字段缺失、字段取值不确定、CRS 单位、空结果、Shapefile 字段名限制等。
- `fallbacks`：字段或取值不匹配时的回退策略。
- `summary`：可执行方案摘要。
- `retrieval_queries`：覆盖全部操作的一组中英文标准 GIS 查询。
- `algorithm_evidence`：每个最终算法对应的 Catalog 描述、参数依据和选择理由。

## 规划规则

- 涉及距离/面积时必须说明投影 CRS 策略：若图层 CRS 为地理坐标系，应先重投影到合适的米制 CRS，再缓冲。
- 多步骤任务必须明确每一步输入、输出和算法；不要直接写“生成代码完成全部操作”。
- 先完成全部步骤规划，再只调用一次 `search_qgis_processing_tools`；禁止按步骤反复搜索。
- 用一次 `get_qgis_processing_tool.tool_ids` 批量读取候选详情。未读取详情的算法不得进入最终方案。
- 本阶段只选择算法，不执行 Processing。
- 最终输出文件必须在 `final_outputs` 中列出，例如 `500m.shp`。
- 如果“政府办公”“公园”等业务概念无法通过字段或样例判断，应在 `risks` 中说明，并优先使用 `inspect_layer` 结果中的真实字段和值。
- 如果风险不可接受，先询问用户，不继续生成代码。

## 大数据量空间分析规则

- 任一矢量输入超过 10 万要素时，在 `risks` 中标记为大数据任务，并在方案中明确性能策略。
- 不得规划 Python 逐要素双层循环；优先使用原生 Processing 算法或数据源端空间查询。
- 空间筛选前检查大图层是否有空间索引；数据源支持但缺少索引时，先选择空间索引创建算法。
- 先按属性缩小目标图层，再生成缓冲；线缓冲用于筛选时默认 `DISSOLVE=True`。
- 先用缓冲区范围或空间索引缩小候选集，再执行精确空间关系判断。
- 分析查询遇到空几何或无效几何时，默认通过 Processing context 的 `GeometrySkipInvalid` 排除并报告；除非用户明确要求修复数据，否则不得规划 `fixgeometries`，也不得创建完整图层的修复副本。
- 大结果和中间结果优先写入 GeoPackage，避免 Shapefile、GeoJSON 和大型内存图层。
- PostGIS 等数据库图层优先将属性和空间条件下推到数据源执行。

## 政府办公 500m 缓冲相交方案模板

适用于“从建筑物图层中提取政府办公地块，做 500m 缓冲区，再提取建筑物图层中与缓冲区相交的地块，输出 500m.shp”。

推荐步骤：

1. 检查 `建筑物` 图层字段、CRS、几何类型和样例值。
2. 构造“政府办公”属性表达式，优先使用真实字段；常见候选包括 `amenity='townhall'`、`office='government'`、`building='government'`、`name ILIKE '%政府%'`、`name ILIKE '%政务%'`。
3. 使用 `native:extractbyexpression` 提取政府办公地块。
4. 若图层 CRS 不是米制投影，使用 `native:reprojectlayer` 重投影到合适 CRS。
5. 使用 `native:buffer` 创建 500 米缓冲区，建议 `DISSOLVE=True`。
6. 使用 `native:extractbylocation` 或 `native:intersection` 从建筑物图层提取与缓冲区相交的地块。
7. 使用最终路径 `Path(QGIS_AGENT_WORKSPACE) / "500m.shp"` 输出，并设置：

```json
[{"path": "500m.shp", "name": "500m", "type": "vector"}]
```
