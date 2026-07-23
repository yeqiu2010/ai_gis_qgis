---
name: qgis-toolbox
description: QGIS Processing 算法知识检索与简单单步 GIS 任务
tools:
  - load_skill
  - list_layers
  - inspect_layer
  - inspect_layers
  - search_qgis_toolbox_domains
  - search_qgis_processing_tools
  - get_qgis_processing_tool
  - execute_gis_code
includes:
  - code-generator
  - code-reviewer
  - executor
version: 3.0.0
author: AI GIS QGIS Plugin
license: MIT
platforms: [linux, Windows, macos]
metadata:
  hermes:
    tags: [qgis, processing, toolbox]
    related_skills: [gis-pipeline]
    requires_tools: [inspect_layers, search_qgis_processing_tools, get_qgis_processing_tool, execute_gis_code]
  qgis_agent:
    side_effects:
      modifies_qgis_project: true
      writes_files: true
      requires_confirmation: true
      concurrency: qgis_main_thread_serial
---

# QGIS Toolbox

本 Skill 只处理一个主要输入、一个明确 Processing 操作的简单任务。大模型负责理解
任务，Catalog 提供算法事实，最终生成脚本执行；不直接逐个调用 Processing Runner。

## 执行流程

1. 使用 `inspect_layer` 确认真实图层、字段、几何类型和 CRS。
2. 把用户业务语言转换为标准 GIS 术语和英文算法关键词。
3. 只调用一次 `search_qgis_processing_tools`。
4. 只调用一次 `get_qgis_processing_tool` 获取选中算法的完整参数和示例。
5. 根据真实参数生成完整脚本，并按 `code-reviewer` 规则审查。
6. 脚本最终输出到 `QGIS_AGENT_WORKSPACE`，通过 `execute_gis_code` 一次确认执行。

字段和值已经由用户明确指定时，直接生成最终 Processing 筛选代码，不得先用
`execute_gis_code` 打印唯一值做诊断。确需补充探查时优先使用 `inspect_layer`；stdout
不是文件，禁止为仅打印的诊断代码声明虚假的 `expected_outputs`。

## GIS 术语映射

先按用户目标识别操作，再生成英文检索词。不要把整句业务描述原样作为唯一查询。

| 业务表达 | 标准 GIS 操作与英文关键词 |
|---|---|
| 筛选、查找、选出某类要素 | attribute filter, extract by expression |
| 周边、服务半径、影响范围 | buffer, distance, dissolve |
| 位于、落在、范围内 | within, contains, extract by location |
| 重叠、相交、交叉部分 | intersects, intersection, overlay |
| 按边界截取 | clip |
| 合并同类地块、消除内部边界 | dissolve |
| 空间挂接、归属街道或地块 | spatial join, join attributes by location |
| 分类统计、按街道汇总 | statistics by categories, aggregate, group by |
| 添加或更新计算字段 | field calculator, expression |
| 面积、长度、周长 | area/length/perimeter calculation, field calculator |
| 统一或转换坐标系 | reproject layer, projected CRS |
| 修复无效几何 | fix geometries, make valid |
| 合并多个图层 | merge vector layers / merge rasters |
| 栅格转矢量、矢量转栅格 | polygonize / rasterize |
| 坡度、坡向、阴影 | slope, aspect, hillshade |
| 插值生成表面 | interpolation, IDW, TIN |

一次查询一个操作，复杂任务通过 `queries` 一次提交全部查询，例如：

```json
{
  "queries": [
    "extract by expression school type attribute filter",
    "reproject layer projected CRS meter",
    "buffer 500 meters dissolve",
    "intersection residential polygons",
    "field calculator overlap area ratio",
    "statistics by categories street aggregate"
  ]
}
```

## 路由门禁

出现以下任一情况，立即调用 `load_skill({"skill_name":"gis-pipeline"})`，保留当前已经确认的图层和参数：

- 需要两个及以上 Processing 算法。
- 同时包含属性筛选与空间分析。
- 涉及多个输入图层或中间结果依赖。
- 需要统一 CRS 后再做距离或面积计算。
- 涉及空间连接、分类汇总、字段连接和字段计算的组合。
- 需要推断字段含义、属性取值或复杂统计口径。

不要尝试在本 Skill 内把复杂任务拆成多轮搜索和执行。

## 边界

- 不注册、检索或执行用户自定义 Skill 和公司自定义工具。
- Catalog 结果是候选事实，必须以算法详情中的参数为准。
- 用户未指定输出文件时使用合理默认文件名，不询问保存目录。
- 执行成功后检查输出是否包含用户要求的字段和结果。
