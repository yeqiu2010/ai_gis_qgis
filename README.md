# AI GIS Agent

AI GIS Agent 是一个面向 QGIS 的自然语言 GIS 助手插件。插件在 QGIS 中嵌入 Vue 对话面板，通过 QWebChannel 连接 Python 后端，并基于 Hermes 风格的渐进式多 Skill Agent Loop 完成图层查看、数据加载、空间分析、代码生成与执行等任务。

当前插件仍标记为 experimental，建议先在测试工程或备份数据上验证工作流。

## 功能概览

- QGIS 侧边栏聊天面板，支持会话历史、流式过程消息和停止当前任务。
- OpenAI-compatible、OpenAI、Ollama 和离线 Echo provider。
- Skill 驱动的任务路由：先检索轻量 Skill 卡片，再按需加载完整 `SKILL.md`；同一主 Agent Loop 可以组合多个 Skill 并共享参数、计划和产物。
- 通用计划与完成验证：复杂任务持久化为 `task_runs`、`plan_steps` 和 `artifacts`，只有步骤与必需产物通过验证后才完成。
- 可选 `invoke_skill` Delegation：复杂且独立的只读子任务可以进入隔离子循环；QGIS 工程修改、文件写入和确认型任务强制留在主循环。
- QGIS Toolbox 算法知识检索：先分析数据和 GIS 术语，再批量检索 Processing 算法及参数示例。
- 统一脚本执行：复杂流程生成并审查一份完整 Processing/PyQGIS 脚本，通过 `execute_gis_code` 一次确认执行。
- 用户自定义 Skills 可放在 `~/.qgis_hermes_agent/custom_skills`；自定义工具以现有 `ToolEntry` 方式注册，并由 Skill 的 `tools` 声明调用。
- QGIS 图层工具：列出图层、查看字段/CRS/范围、加载/移除图层、缩放到图层、导出图层。
- SAM3 遥感影像分割：通过独立 SAM3-Geo-API 对当前栅格执行文本、自动或矢量框提示分割，输出地理配准掩码或 GeoPackage 面图层，并可继续进入矢量 GIS 流程。
- 代码执行工具：在 QGIS 主线程中运行受控 PyQGIS 代码，并将结果加载回当前工程。
- 工具确认机制：对需要确认的写入或潜在破坏性操作先请求用户确认。
- 本地 SQLite 会话库，默认保存到 `~/.qgis_hermes_agent/state.db`。
- 结构化失败样本日志，记录工具、Pipeline、生成代码、QGIS 运行和 LLM 调用错误，支持 JSONL 导出。

## 多 Skill Agent Loop

默认执行路径：

```text
search_skills
→ load_skill（可以依次加载多个）
→ create_plan / revise_plan
→ 调用受控 QGIS 工具
→ complete_plan_step / register_artifact
→ finalize_task
```

Skill 是按需加载的执行指导文档，Tool 是真正执行代码的函数。`invoke_skill` 不是普通 Skill 加载，而是面向 Skill 的隔离子 Agent：

```text
invoke_skill ≈ skill_view + delegate_task + GIS 契约验证
```

内置与自定义 `SKILL.md` 支持 Hermes Frontmatter，包括 `metadata.hermes`；GIS 运行时输入、输出、副作用和完成条件放在 `metadata.qgis_agent`。完整设计见 [多 Skill 改造方案](md/基于Hermes-Agent-Loop的多Skill改造方案.md)。

前端状态栏会显示当前已加载 Skills 和活动计划进度。RPC 同时提供 `listLoadedSkills` 与 `getTaskState`，便于其他界面或集成读取结构化状态。

## 失败日志与改进数据

失败样本保存在会话数据库的 `failure_log` 表中。记录内容包括错误来源、工具或阶段、
错误分类、推断原因、重试次数、是否适合重试、生成代码以及相关参数、stdout/stderr。
常见分类包括 `output_contract`、`pipeline_stage`、`invalid_geometry`、`missing_output`、
`generated_code_api`、`timeout`、`rate_limit`、`layer_not_found`、`field_not_found` 和
`processing_error`；工具流程错误还会分类为 `tool_arguments`、`search_query` 等。

可通过 `SessionDB` 查询或导出 JSONL：

```python
from ai_gis_qgis.database.session_db import SessionDB

db = SessionDB("~/.qgis_hermes_agent/state.db")
records = db.get_failure_records(limit=200)
db.export_failure_records("failure_samples.jsonl")
```

导出内容可能包含图层名称、本地路径、生成代码和错误上下文，分享给外部人员或模型前应先脱敏。

## 环境要求

- QGIS 3.28 或更高版本。
- Python 3.11 及以上用于本地开发和测试。
- Node.js/npm 用于重建前端资源。
- 可选：OpenAI-compatible 服务、OpenAI API 或 Ollama 服务。

QGIS 运行时会提供 `qgis.PyQt` 模块；普通 Python 环境中不需要也通常无法直接安装 QGIS Python 模块。

## 安装插件

项目打包产物位于：

```bash
dist/ai_gis_qgis.zip
```

在 QGIS 中安装：

1. 打开 `插件` -> `管理并安装插件`。
2. 选择 `从 ZIP 安装`。
3. 选择 `dist/ai_gis_qgis.zip`。
4. 安装后启用 `AI GIS Agent`。
5. 点击工具栏图标或菜单 `AI GIS Agent` 打开侧边栏。

如果需要重新生成安装包：

```bash
uv run python scripts/package_plugin.py
```

也可以使用系统 Python 运行脚本，但开发依赖推荐交给 `uv` 管理。

## 模型配置

打开插件面板右上角设置按钮，配置 LLM：

- `提供商`：`openai_compatible`、`openai`、`ollama` 或 `echo`。
- `Base URL`：OpenAI-compatible 接口地址，例如 `http://127.0.0.1:8000/v1`。
- `模型名`：服务端模型名。
- `API Key`：本地服务可留空；OpenAI 或需要鉴权的服务需填写。
- `Temperature`：采样温度。
- `Max Tokens`：期望的最大输出 token 数。
- `Context Window`：模型总上下文窗口，例如 vLLM `--max-model-len 32768` 时填写 `32768`。
- `请求超时（秒）`：等待 OpenAI-compatible/vLLM 完成一次非流式生成的最长时间，默认 300 秒。27B 等大模型或长工具调用建议设置为 300–600 秒。

后端会根据当前输入上下文自动收敛本次 `max_tokens`，避免 `输入 token + 输出 token` 超过模型上下文长度。若 vLLM 返回精确的上下文越界错误，provider 会解析错误并用合法输出上限自动重试一次。
复杂 GIS 脚本需要把完整 Python 代码放入工具参数；默认 `Max Tokens` 为 16384。若使用旧配置中的 4096/8192 并频繁看到“参数 JSON 不完整或被截断”，请在设置中提高到 16384，同时确保 Context Window 留有足够输出空间。Agent 主循环默认允许 40 次迭代和 50 次工具调用。
GIS Pipeline 会在阶段边界以完整 JSON envelope 传递最小状态：下一阶段只接收前一阶段 artifact，并在 `solution_plan` 之后继续携带 `structured_query` 中的结构化用户需求；历史对话、原始工具结果和更早阶段不会加入提示词，截断的 `_raw_arguments` 也不会再次回填。

默认配置见 [config/defaults.py](config/defaults.py)。

## SAM3 遥感影像分割

先启动 `sam3-geo-api` 服务，再在插件设置页的“SAM3 遥感分割服务”区域配置：

- `Base URL`：默认 `http://127.0.0.1:8000`。
- `API Token`：当前本地服务可留空，网关启用 Bearer 鉴权时填写。
- 连接/推理超时、最大上传大小、最大像元数和单次最大边界框数。
- HTTPS 服务默认校验证书。

点击“测试 SAM3 连接”可查看模型是否就绪。之后可在对话中使用，例如：

```text
从当前遥感影像中分割建筑物，输出面图层。
在研究区面图层范围内识别水体，同时保留掩码栅格。
使用候选框图层中选中的要素，在影像中生成真实轮廓。
识别建筑物后与地块相交，统计各地块建筑占地率。
```

插件会在确认后把指定范围的影像快照上传到配置的服务。SAM3 API 请求通过受信任工具执行，不进入禁止联网的生成代码沙箱；分割结果加载为真实 QGIS 图层后，才能继续交给 QGIS Toolbox 或 GIS Pipeline。

当前 SAM3 服务不支持点提示和超大影像自动分块。超过像元限制时，请改用当前画布范围或 AOI。完整设计与限制见 [SAM3 接入方案](md/SAM3遥感影像分割API插件接入方案.md)。

## 开发

安装 Python 开发依赖：

```bash
uv sync --dev
```

安装前端依赖：

```bash
cd frontend
npm install
```

重建前端产物：

```bash
cd frontend
npm run build
```

构建结果会写入 `resources/frontend_dist`，QGIS 插件运行时加载该目录中的静态文件。

重新打包插件：

```bash
uv run python scripts/package_plugin.py
```

## 测试与检查

运行单元测试：

```bash
uv run pytest
```

运行 Python 语法检查示例：

```bash
python3 -m py_compile backend/llm/openai_provider.py backend/llm/provider_registry.py qwebengine/web_engine_view.py
```

前端类型检查和生产构建：

```bash
cd frontend
npm run build
```

## 目录结构

```text
backend/              Agent 核心、LLM provider、工具与执行器
config/               默认配置与设置管理
database/             SQLite schema、迁移和会话数据库
frontend/             Vue 前端源码
qwebengine/           QGIS WebEngine 面板、QWebChannel 和 RPC 控制器
resources/            图标与已构建前端资源
resources/qgis_toolbox/ QGIS Processing 工具箱检索目录
skills/               Agent Skill 定义
tests/                单元测试
scripts/              打包和调试脚本
metadata.txt          QGIS 插件元数据
plugin.py             QGIS 插件生命周期入口
```

## 数据与安全说明

- 会话数据库默认位于 `~/.qgis_hermes_agent/state.db`。
- 代码执行工作目录默认位于 `~/.qgis_hermes_agent/workspaces`。
- 复杂或写入型操作会通过工具确认流程请求用户确认。
- 建议在真实生产数据上操作前先备份 QGIS 工程和关键数据文件。
