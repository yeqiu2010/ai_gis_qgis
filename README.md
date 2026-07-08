# AI GIS Agent

AI GIS Agent 是一个面向 QGIS 的自然语言 GIS 助手插件。插件在 QGIS 中嵌入 Vue 对话面板，通过 QWebChannel 连接 Python 后端，并基于可切换的 Skill 工作流完成图层查看、数据加载、空间分析、代码生成与执行等任务。

当前插件仍标记为 experimental，建议先在测试工程或备份数据上验证工作流。

## 功能概览

- QGIS 侧边栏聊天面板，支持会话历史、流式过程消息和停止当前任务。
- OpenAI-compatible、OpenAI、Ollama 和离线 Echo provider。
- Skill 驱动的任务路由，复杂 GIS 分析会进入 `gis-pipeline` 流程。
- QGIS 图层工具：列出图层、查看字段/CRS/范围、加载/移除图层、缩放到图层、导出图层。
- 代码执行工具：在 QGIS 主线程中运行受控 PyQGIS 代码，并将结果加载回当前工程。
- 工具确认机制：对需要确认的写入或潜在破坏性操作先请求用户确认。
- 本地 SQLite 会话库，默认保存到 `~/.qgis_hermes_agent/state.db`。

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

后端会根据当前输入上下文自动收敛本次 `max_tokens`，避免 `输入 token + 输出 token` 超过模型上下文长度。若 vLLM 返回精确的上下文越界错误，provider 会解析错误并用合法输出上限自动重试一次。

默认配置见 [config/defaults.py](config/defaults.py)。

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

