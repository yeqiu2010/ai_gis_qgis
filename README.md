# AI GIS Agent

AI GIS Agent 是一个嵌入 QGIS 的自然语言 GIS 助手插件。它使用 Vue 对话面板接收任务，通过 QWebChannel 调用 Python 后端，由 Skill 驱动的 Agent Loop 选择受控工具、组织 GIS 工作流，并在当前 QGIS 工程中完成图层管理、空间分析、专题制图、代码执行和遥感影像分割。

当前 QGIS 插件版本为 `0.3.0`，状态为 `experimental`。建议先在测试工程或已备份的数据上使用。

## 核心特性

- 在 QGIS 侧边栏中进行多轮对话，支持会话切换、任务停止、执行过程、计划进度和 Token/耗时统计。
- 支持 OpenAI、OpenAI-compatible、Ollama，以及无需联网的 Echo 测试 Provider。
- 使用渐进式 Skill 路由：先读取轻量能力目录，再按需加载完整 `SKILL.md`，避免一次向模型注入全部工作流知识。
- 使用结构化 Tool 调用执行真实操作；Skill 只描述工作流，不直接获得任意代码执行权。
- 复杂 GIS 分析采用五阶段 Pipeline，并在生成代码、执行代码和交付结果之间保留可审计的阶段产物。
- 写入工程、执行生成代码、导出数据和遥感分割等操作可通过统一确认流程暂停并等待用户授权。
- 在 QGIS 主线程执行受控 Processing/PyQGIS 代码，检查代码结构、输出路径和产物契约。
- 使用本地 SQLite 保存会话、工具链、计划、产物、失败样本、运行指标和学习候选。
- 内置 SAM3 能力插件，可把遥感分割结果转换为真实的 QGIS 栅格或矢量图层，并继续参与后续 GIS 分析。
- 针对 DeepSeek thinking mode 保留 `reasoning_content` 调用链，同时不在用户界面展示模型的 `<think>` 等内部推理内容。

## 系统架构

```text
┌──────────────────────────────── QGIS Desktop ────────────────────────────────┐
│                                                                              │
│  plugin.py                                                                   │
│      │ 插件生命周期、工具栏、DockWidget                                      │
│      ▼                                                                       │
│  Vue 3 / TypeScript UI  ◄──── QWebChannel JSON-RPC + Agent Events ────►     │
│  resources/frontend_dist              qwebengine/RPCController               │
│                                              │                               │
│                                              │ 后台 Agent 线程                │
│                                              ▼                               │
│                                       backend/AgentCore                      │
│                         ┌────────────────────┼────────────────────┐           │
│                         ▼                    ▼                    ▼           │
│                 Context/Prompt        Skills/Tools/Plugins       LLM          │
│                 上下文压缩与预算       路由、计划、确认、执行      Provider     │
│                         │                    │                    │           │
│                         │                    ▼                    │           │
│                         │            MainThreadExecutor           │           │
│                         │                    │                    │           │
│                         │                    ▼                    │           │
│                         │          QGIS API / Processing          │           │
│                         │                                         │           │
│                         └──────────────► SQLite ◄─────────────────┘           │
│                                      状态与审计                               │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      └──── 可选：SAM3-Geo-API
```

### 组件职责

| 层级 | 主要目录 | 职责 |
| --- | --- | --- |
| QGIS 外壳 | `plugin.py` | 注册菜单和工具栏，创建右侧 DockWidget，管理插件生命周期。 |
| 前端 | `frontend/`、`resources/frontend_dist/` | Vue 3 对话界面、设置页、确认面板、任务状态和运行指标展示。 |
| 通信层 | `qwebengine/` | WebEngine 加载、QWebChannel RPC、后台任务、取消控制、事件分发和 QGIS 主线程调度；WebEngine 不可用时提供原生 Qt 回退面板。 |
| Agent 核心 | `backend/agent_core.py` | 主循环、Skill 路由、工具调用链、确认恢复、计划续跑、重试、最终交付和指标采集。 |
| 上下文系统 | `backend/context/` | 构造 QGIS 上下文、系统提示、会话记忆，估算 Token 并压缩已完成工具交换。 |
| LLM 适配层 | `backend/llm/` | Provider 抽象、OpenAI-compatible/Ollama/Echo 实现、上下文窗口适配、DeepSeek reasoning 兼容和可见内容过滤。 |
| 能力系统 | `backend/skills/`、`backend/tools/`、`backend/plugin_system.py` | 解析 Skill、限制可用 Tool、注册内置/扩展能力、校验参数和组织产物。 |
| GIS 执行层 | `backend/executor/`、`backend/processing/` | 代码预检、工作目录约束、QGIS/Processing 执行、算法证据和输出验证。 |
| 数据层 | `database/` | SQLite schema v7、迁移、会话、计划、日志、指标、失败样本和学习数据。 |
| 能力插件 | `plugins/` | 通过 `plugin.yaml` 注册可信 Tool、Skill 和执行 Hook；SAM3 是当前内置示例。 |

## Skill、Tool、能力插件与 Agent 的关系

| 概念 | 作用 | 是否执行代码 | 关键约束 |
| --- | --- | --- | --- |
| Skill | `SKILL.md` 中的路由描述、步骤、输入输出和完成条件 | 否 | 只能使用 Frontmatter 声明并由注册表允许的 Tool。 |
| Tool | 具有 JSON 参数契约的 Python 执行入口 | 是 | 可声明确认、破坏性、工程写入、幂等键、执行亲和性和结果压缩器。 |
| 能力插件 | 一组 Tool、Skill 和 Hook 的可信扩展包 | 是 | 用户/项目插件默认不自动启用；启用后等同本地可信代码。 |
| Agent Loop | 根据用户请求加载 Skill、调用 Tool 并检查完成条件 | 负责调度 | 不允许用自然语言伪造工具结果或跳过确认。 |

Skill 与 Tool 是分离的。`load_skill` 只是把工作流知识加入当前 Agent 上下文；真正修改图层、写文件或访问 SAM3 服务的操作必须通过 Tool 完成。

## 一次请求的运行链路

1. 前端通过 `chat` RPC 提交用户消息；`RPCController` 创建后台运行并分配独立 `run_id`。
2. `AgentCore` 采集当前 QGIS 工程、图层和会话状态，构建系统提示与可用 Tool 定义。
3. `QGISContextEngine` 计算系统提示、消息和工具 Schema 的 Token 占用；达到软阈值时压缩历史工具结果，但保留有效的 assistant/tool 调用链。
4. 主调度根据 Skill 描述做语义匹配，选择直接工具、专用业务 Skill、单步 QGIS Toolbox 或复杂 GIS Pipeline。
5. 模型返回结构化 Tool Call；`ToolRegistry` 检查能力可用性并执行前后 Hook。
6. 需要确认的 Tool 写入待确认状态并暂停；用户批准后执行真实操作，再把结果送回同一计划继续处理。
7. 工具结果、产物、计划步骤、失败信息和运行指标持续写入 SQLite。
8. 只有任务步骤和必需产物满足完成条件后才生成最终答复；界面只显示可见结论、必要说明或待补充问题。

长任务通过事件协议向界面发送 `thinking`、`tool_start`、`tool_end`、`confirm_request`、`message_delta`、`run_metrics` 和 `complete` 等事件。当前 Provider 使用非流式 HTTP 响应，`message_delta` 用于收到完整响应后的增量渲染，不代表服务端 Token Streaming。

## 任务路由

### 直接图层操作

明确的图层查看或管理请求优先走结构化工具，包括：

- 列出和检查图层、字段、CRS、范围、样例与要素数量。
- 加载和移除图层、缩放到图层、应用 QML 样式、导出现有图层。
- 使用稳定的 QGIS `layer_id` 在后续步骤间传递真实图层。

### 专用业务 Skill

专用 Skill 优先于通用 Pipeline。当前内置主要业务能力包括：

| Skill | 适用任务 | 主要产物 |
| --- | --- | --- |
| `calculate-land-use-building-metrics` | 按用地类型统计建筑数量、高度、占地、建筑总量和密度 | 汇总表、结果图层或文件 |
| `calculate-school-service-coverage` | 计算学校服务半径、住宅覆盖率、盲区及分街道汇总 | 缓冲/覆盖图层和统计结果 |
| `generate-land-cover-map` | 裁剪分类栅格或矢量并生成土地覆盖专题图 | QGIS 布局、PNG/PDF 和图层 |
| `analyze-cultivated-land-loss` | 分析耕地不合理流出、非农化、非粮化及永久基本农田占比 | 规定格式的年度 Excel 统计表 |
| `sam3:remote-segmentation` | 从遥感影像提取建筑、水体、道路、植被等可见目标 | 地理配准掩码或 GeoPackage 面图层 |

### QGIS Toolbox

一个主要输入、一个明确操作的简单任务加载 `qgis-toolbox`。Agent 先检索仓库内的 Processing 算法目录，再读取候选算法参数和代码示例，避免凭模型记忆猜测算法 ID 或参数名。

### GIS Pipeline

没有专用 Skill 覆盖，且任务包含多步骤、多图层、CRS/字段推断、空间连接或统计汇总时，进入 `gis-pipeline`：

```text
data_overview
    ↓
structured_query
    ↓
solution_plan
    ↓
generated_code + review
    ↓
execute_gis_code（用户确认）
    ↓
execution_result + artifact verification
```

每个阶段必须调用 `record_pipeline_stage` 保存完整 JSON artifact。阶段交接只携带下一步所需的最小状态；生成代码阶段仍会带上结构化需求，防止上下文压缩后偏离原始目标。

### 隔离 Skill 调用

`invoke_skill` 可把边界清晰、可独立完成且不需要用户确认的子任务交给隔离 Skill Runner。该 Runner 只看到显式目标、输入和 Skill 契约，并且不能调用需要确认、具有破坏性或会写入 QGIS 工程的 Tool。这一机制用于减少主上下文压力，不会创建独立进程或远程 Agent。

## 代码执行与产物验证

默认 `executor.execution_mode` 为 `current_qgis`。生成代码会在当前已打开的 QGIS Python 环境中运行，因此可以使用当前工程、图层和 Processing Provider，但不会创建新的 `QgsApplication`。

执行前后包含以下控制：

- AST 预检禁止网络模块、`subprocess`、动态执行、退出 QGIS、删除/重命名文件等高风险调用。
- `open()` 的写入操作被限制在本次独立工作目录内；允许读取任务需要的现有输入，但输出路径必须位于 `QGIS_AGENT_WORKSPACE`。
- `expected_outputs` 明确声明文件路径、名称、类型和是否必需。
- 执行结束后验证必需文件、矢量/栅格可读性和输出契约，再将有效结果加载回 QGIS。
- 已确认的幂等 Tool 可复用真实结果，避免相同参数重复执行。
- 代码失败时最多生成有限次数的完整自包含修复脚本；相同失败代码不会无限重放。

这里的安全机制是应用层防护，不是操作系统级沙箱。生成代码仍运行在 QGIS 进程内，安装第三方能力插件或自定义 Tool 前必须审查其源码。

## 上下文与模型兼容性

### Token 预算

`Context Window` 表示模型的总上下文窗口，`Max Tokens` 表示一次请求允许的最大输出，两者不是同一个值。默认配置为：

| 配置 | 默认值 |
| --- | ---: |
| `max_context_tokens` | 32768 |
| `max_tokens` | 16384 |
| `minimum_output_tokens` | 2048 |
| `soft_threshold_ratio` | 0.55 |
| `historical_message_limit` | 8 |
| `request_timeout_seconds` | 300 |

上下文引擎会为输出和 tokenizer 误差预留空间，并优先压缩已完成工具结果、大型代码参数和中段历史。OpenAI-compatible 服务若返回精确的上下文上限与输入 Token，Provider 会记录偏差并逐级收缩输出预算；确定性 4xx 请求不会在 Agent 层无意义重试。

### DeepSeek thinking mode

OpenAI-compatible Provider 对 reasoning 模型做了额外兼容：

- 保存模型返回的 `reasoning_content`，并在后续 assistant/tool 调用链中原样传回。
- 把所有控制指令合并到首条 `system` 消息，兼容要求 “System message must be at the beginning” 的服务。
- 用户可见答复会过滤 `<think>`、`<thinking>`、`<reasoning>` 和 `<analysis>` 块；结构化 reasoning 仍保留给模型，不进入界面。

## 环境要求

- QGIS `3.28` 或更高版本；插件元数据声明的最大兼容版本为 `4.99`。
- QGIS 运行环境需要可用的 Qt WebEngine 和 QWebChannel；缺失时插件会尝试使用原生 Qt 面板。
- 本地开发和测试使用 Python `3.11+`。
- 重建前端需要 Node.js 和 npm。
- 真实模型调用需要 OpenAI/OpenAI-compatible 服务或 Ollama；Echo Provider 可用于离线检查界面和会话链路。
- SAM3 功能需要单独部署兼容的 SAM3-Geo-API。

QGIS 自带 `qgis` 和 `qgis.PyQt`，普通 Python 虚拟环境通常无法安装或导入这些模块。单元测试通过替身隔离 QGIS 运行时。安装包中已包含 Excel 工作流所需的 `openpyxl` 和 `et_xmlfile`。

## 安装

### 从 ZIP 安装

可安装包位于：

```text
dist/ai_gis_qgis.zip
```

1. 在 QGIS 中打开“插件 → 管理并安装插件”。
2. 选择“从 ZIP 安装”。
3. 选择 `dist/ai_gis_qgis.zip`。
4. 启用 `AI GIS Agent`。
5. 点击工具栏按钮或菜单项打开右侧面板。

升级时建议覆盖安装后完全重启 QGIS，避免旧 Python 模块和旧前端资源仍驻留在进程中。

### 从源码打包

```bash
uv sync --dev
cd frontend
npm ci
npm run build
cd ..
uv run python scripts/package_plugin.py
```

打包脚本会把插件运行所需的 Python 模块、配置、数据库 Schema、Skills、能力插件、前端产物和 vendored 依赖写入 `dist/ai_gis_qgis.zip`，不会打包测试、前端源码、虚拟环境或 `node_modules`。

## 配置

面板设置页当前提供 LLM 和 SAM3 常用配置。完整默认值见 [config/defaults.py](config/defaults.py)。

### LLM

| 字段 | 说明 |
| --- | --- |
| `provider` | `openai_compatible`、`openai`、`ollama` 或 `echo`。`openai` 与兼容接口共用同一 Provider 实现。 |
| `base_url` | OpenAI-compatible 通常以 `/v1` 结尾；Ollama 填服务根地址。 |
| `model` | 服务端暴露的准确模型名。 |
| `api_key` | 本地无鉴权服务可留空。 |
| `temperature` | 默认 `0.1`，GIS 工具调用建议保持较低。 |
| `max_tokens` | 单次最大输出；复杂脚本建议保留默认 `16384` 或按模型能力调整。 |
| `max_context_tokens` | 模型总上下文窗口，必须与服务端实际限制一致。 |
| `request_timeout_seconds` | 非流式请求的客户端等待时间，默认 `300` 秒。 |

### 本地路径

| 数据 | 默认位置 |
| --- | --- |
| 设置回退文件 | `~/.qgis_hermes_agent/settings.json` |
| 会话数据库 | `~/.qgis_hermes_agent/state.db` |
| 代码执行工作区 | `~/.qgis_hermes_agent/workspaces/<日期>/<UUID>/` |
| 自定义 Skills | `~/.qgis_hermes_agent/custom_skills/` |
| 自定义 Tools | `~/.qgis_hermes_agent/custom_tools/` |
| 用户能力插件 | `~/.qgis_hermes_agent/plugins/` |

设置同时写入 QGIS `QSettings` 和本地回退文件，并以 revision 选择最新值。API Key 和 SAM3 Token 属于本机敏感配置；RPC 回读会脱敏，但本地持久化文件仍应通过操作系统权限保护。

### 高级配置

`config/defaults.py` 还定义了：

- `context`：压缩开关、软阈值、最小输出预算和工具结果内联上限。
- `executor`：执行模式、超时、内存提示值和工作目录。
- `skills`：自定义 Skill 与 Tool 目录。
- `plugins`：用户/项目插件目录、启用和禁用列表；项目插件需要显式设置 `enable_project_plugins=true`。
- `ui`：面板宽度、主题和语言的预留配置。

## SAM3 遥感分割

SAM3 通过内置能力插件 `plugins/sam3` 注册以下 Tool：

- `check_sam3_service`
- `inspect_sam3_segmentation_inputs`
- `segment_remote_sensing_image`

设置页可配置服务地址、Bearer Token、连接/推理超时、最大上传大小、最大像元数、边界框数量、TLS 校验和默认 RGB 波段。典型请求：

```text
从当前遥感影像中分割建筑物，输出面图层。
在研究区边界内识别水体，同时保留掩码栅格。
使用候选框图层中选中的要素分割建筑物，再统计各地块建筑占地率。
```

SAM3 网络请求由受信任 Tool 发起，不由生成代码直接联网。分割结果必须先写入工作区并加载为真实 QGIS 图层，之后才能作为裁剪、相交、空间连接、统计或制图的输入。

当前实现不支持点提示，也不负责超大影像自动切片；超过服务像元限制时应改用当前画布、AOI 或预先裁剪的数据。

## 持久化与可观测性

SQLite schema 当前版本为 `7`，主要数据分为：

- 会话：`sessions`、`messages`、`state_meta`。
- 运行指标：`run_metrics`，记录耗时、输入/输出 Token、LLM 调用次数和状态。
- GIS 审计：`tool_call_log`、`layer_snapshots`、`code_execution_log`、`failure_log`。
- 任务状态：`task_runs`、`plan_steps`、`skill_invocations`、`artifacts`、`task_outcomes`。
- 本地学习：`solution_recipes`、`recipe_runs`、`knowledge_candidates`、`knowledge_versions`、`skill_usage`。

失败样本会按 `generated_code_api`、`output_contract`、`pipeline_stage`、`timeout`、`rate_limit`、`layer_not_found`、`field_not_found`、`processing_error` 等类型分类。可用 Python 导出 JSONL：

```python
from ai_gis_qgis.database.session_db import SessionDB

db = SessionDB("~/.qgis_hermes_agent/state.db")
records = db.get_failure_records(limit=200)
db.export_failure_records("failure_samples.jsonl")
```

导出内容可能包含本地路径、图层名称、生成代码、参数和 stderr，分享前必须脱敏。

## 扩展开发

### 自定义 Skill

在 `~/.qgis_hermes_agent/custom_skills/<skill-name>/SKILL.md` 创建 Hermes 风格文档。后加载的自定义 Skill 可覆盖同名内置 Skill。

```yaml
---
name: my-analysis
description: 描述何时应选择此能力
tools: [list_layers, inspect_layers]
lifecycle: task
metadata:
  hermes:
    tags: [gis, custom]
    requires_tools: [list_layers]
  qgis_agent:
    inputs: {}
    outputs: {}
    side_effects:
      modifies_qgis_project: false
---
```

`metadata.qgis_agent` 可声明输入、输出、副作用和完成检查；`metadata.hermes` 可声明标签及 Tool/Toolset 依赖，顶层 Frontmatter 可声明平台和必需环境变量。

### 自定义 Tool

在 `~/.qgis_hermes_agent/custom_tools/` 放置 Python 文件，并导出 `build_tool()` 或 `build_tools()`；返回值必须是 `ToolEntry` 或其列表。自定义 Tool 是可执行 Python 代码，不受 Skill 文档的只读属性保护，只应加载受信任实现。

### 能力插件

能力插件使用 `plugin.yaml` 和注册函数组合 Tool、Skill 与 Hook：

```yaml
id: example
version: 1.0.0
entrypoint_file: plugin.py
entrypoint: register
```

插件注册函数接收受限的 `PluginContext`，可调用 `register_tool()`、`register_skill()` 和 `register_hook()`。发现来源包括内置 `plugins/`、用户插件目录、显式启用的项目插件目录，以及 Python entry point 组 `qgis_hermes_agent.plugins`。非内置插件默认禁用，必须加入 `plugins.enabled`。

## 开发与测试

### Python

```bash
uv sync --dev
uv run pytest tests/unit -q
uv run ruff check .
uv run pyright
```

QGIS API 相关功能需要在真实 QGIS 中做集成验证；普通单元测试覆盖路由、Provider、上下文、数据库、工具契约、SAM3 客户端/适配器和主要业务流程。

### 前端

```bash
cd frontend
npm ci
npm run dev
npm run build
```

`npm run build` 会先运行 `vue-tsc --noEmit`，再由 Vite 输出到 `resources/frontend_dist/`。QGIS 加载的是构建产物，不直接加载 `frontend/src/`。

### 打包

```bash
uv run python scripts/package_plugin.py
```

安装包根目录必须保留为 `ai_gis_qgis/`，否则 QGIS 无法按 Python 包方式加载相对导入。

## 目录结构

```text
ai_gis_qgis/
├─ plugin.py                    QGIS 插件生命周期入口
├─ metadata.txt                QGIS 插件元数据
├─ backend/
│  ├─ agent_core.py            主 Agent Loop
│  ├─ context/                 上下文构建、估算和压缩
│  ├─ llm/                     Provider 与 reasoning 兼容
│  ├─ skills/                  Skill 解析、管理和隔离 Runner
│  ├─ tools/                   核心、业务和规划 Tool
│  ├─ executor/                代码策略、执行和产物验证
│  ├─ processing/              Processing 算法证据
│  └─ sam3/                    SAM3 客户端与 QGIS 适配
├─ config/                     默认配置和持久化
├─ database/                   SQLite schema v7、迁移和数据访问
├─ frontend/                   Vue/TypeScript 源码
├─ qwebengine/                 WebEngine、QWebChannel、RPC 和线程桥
├─ skills/                     内置 SKILL.md 与业务脚本/参考资料
├─ plugins/                    能力插件；当前包含 SAM3
├─ resources/
│  ├─ frontend_dist/           已构建前端
│  └─ qgis_toolbox/            Processing 检索目录
├─ vendor/                     随插件分发的 Python 依赖
├─ tests/unit/                 单元测试
├─ scripts/package_plugin.py   ZIP 打包脚本
└─ dist/ai_gis_qgis.zip        QGIS 安装包
```

## 常见问题

### 面板提示前端未构建

确认 `resources/frontend_dist/index.html` 存在；开发环境执行：

```bash
cd frontend
npm ci
npm run build
```

### 模型返回上下文超限

确保插件的 `Context Window` 与服务端实际模型上限一致，并且不要把 `Max Tokens` 设置为整个上下文大小。复杂脚本通常需要较大输出预算，但仍必须给输入、工具 Schema 和安全边界留出空间。

### DeepSeek 报 reasoning_content 或 system message 错误

当前版本会回传 `reasoning_content`，并保证只有一条位于开头的 `system` 消息。若升级后仍出现旧错误，通常是 QGIS 进程仍加载旧插件模块；覆盖安装并完全重启 QGIS。

### 界面显示大量 Think 内容

当前版本会在后端发布、历史读取和前端渲染三处过滤内联推理块。若服务使用不带标签的自定义推理格式，应优先配置服务端 reasoning parser，使推理通过独立 `reasoning_content` 字段返回。

### Tool 一直等待确认

确认请求是持久化状态。请在当前会话中点击确认或取消；重新发送相同任务不会自动代表授权。取消后计划会进入等待用户或取消状态，避免后台继续写入工程。

## 数据与安全

- 会话、设置、工作目录和失败日志默认都保存在本机用户目录。
- 真实生产数据操作前应备份 QGIS 工程和关键数据文件。
- 不要把未经脱敏的 SQLite 数据库、失败 JSONL、设置文件或工作目录上传到公共服务。
- 生成代码的 AST 与路径策略不能替代操作系统隔离。
- 用户 Tool、用户插件和项目插件都属于本地可执行代码，只启用可信来源。
- SAM3、OpenAI-compatible、OpenAI 和 Ollama 的数据边界由相应服务部署方式决定；使用远程服务前应确认组织的数据合规要求。
