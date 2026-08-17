---
name: solution-planner
description: GIS 处理方案规划
tools:
  - search_qgis_toolbox_domains
  - search_qgis_processing_tools
  - get_qgis_processing_tool
  - record_pipeline_stage
version: 2.1.0
author: AI GIS QGIS Plugin
license: MIT
metadata:
  hermes:
    tags: [gis, pipeline, plan]
    requires_tools: [search_qgis_processing_tools, get_qgis_processing_tool, record_pipeline_stage]
---

# Solution Planner

生成可审查、可执行、可恢复的 GIS 方案。

## 必须产出的 artifact 字段

- `steps`：有序步骤，每步包含 `name`、`input`、`algorithm`、`parameters`、`output`、`checks`。
- `algorithms`：使用的 QGIS Processing 算法 ID，例如 `native:extractbyexpression`、`native:buffer`、`native:extractbylocation`。
- `crs_strategy`：距离/面积任务的 CRS 策略。
- `intermediate_outputs`：中间结果，建议使用内存结果或工作目录临时文件。
- `final_outputs`：最终文件输出，必须与用户要求文件名和 `expected_outputs` 一致；无文件统计或直接样式调整任务可为空，并明确 stdout 结论或工程状态变化。
- `risks`：字段缺失、字段取值不确定、CRS 单位、空结果、Shapefile 字段名限制等。
- `fallbacks`：字段或取值不匹配时的回退策略。
- `summary`：可执行方案摘要。
- `retrieval_queries`：覆盖全部操作的一组中英文标准 GIS 查询。
- `algorithm_evidence`：每个最终算法对应的 Catalog 描述、参数依据和选择理由。
- `output_schema`：用户要求新增/覆盖的字段名、业务含义、存储类型、长度、精度和 NULL/零分母策略；例如 `dense` 必须规划为 Double，而不是 String。
- `substitution_approved`：默认省略或为 false。只有用户在当前会话中明确同意用近似/替代算法时才能设为 true，并在 `risks` 中记录批准内容；Agent 自行判断“效果接近”不构成批准。

## 规划规则

- 涉及距离/面积时必须说明投影 CRS 策略：若图层 CRS 为地理坐标系，应先重投影到合适的米制 CRS，再缓冲。
- 多步骤任务必须明确每一步输入、输出和算法；不要直接写“生成代码完成全部操作”。
- 先完成全部步骤规划，再只调用一次 `search_qgis_processing_tools`；禁止按步骤反复搜索。
- 用一次 `get_qgis_processing_tool.tool_ids` 批量读取候选详情。最终方案的全部算法（包括裁剪、栅格计算、格式转换等辅助步骤）都必须出现在本次详情结果中；未读取详情的算法不得进入最终方案。服务端会持久化本轮证据并拒绝未覆盖的算法。
- `algorithm_evidence` 必须逐项记录最终算法 ID，不得只写自然语言理由。方案参数名必须逐字来自详情中的参数表；若某算法不支持环境范围参数，应把已读取详情的裁剪算法作为显式步骤，而不是在代码阶段临时增加或猜测 `PROJWIN` 等参数。
- 本阶段只选择算法，不执行 Processing。
- 用户明确列出多个不同分析工具时，必须分别检索并保留其语义，不能为了复用一个算法而把另一项改写为“Uniform 核函数替代”“近似点密度”等。Catalog 没有等价算法时，将其列入阻断风险并向用户请求是否接受替代；未经用户明确批准不得进入代码阶段。
- 用户指定的最终输出名称属于硬约束，必须逐字写入 `final_outputs`，包括数字后缀；不得纠正、近似或自行改名。
- 用户要求的最终输出文件必须在 `final_outputs` 中列出，例如 `500m.shp`；仅需最终统计结论或当前图层样式变化时可以不规划文件。
- 如果“政府办公”“公园”等业务概念无法通过字段或样例判断，应在 `risks` 中说明，并优先使用 `inspect_layer` 结果中的真实字段和值。
- 如果风险不可接受，先询问用户，不继续生成代码。
- 方案包含字段计算时，必须在步骤输出和 `output_schema` 中显式声明派生字段类型。密度、覆盖率、比例、均值、面积、长度和高度等小数结果使用 Double；已有同名字段时把类型冲突列入 `risks`，并规划字段重构或新建正确类型字段，不能规划为扩大字符串长度。

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
