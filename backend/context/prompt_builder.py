"""Prompt builder for the minimal AgentCore loop."""

from __future__ import annotations

from pathlib import Path

from ..skills.skill_manager import SkillManager
from .qgis_context import QGISContext


class PromptBuilder:
    def __init__(self, skill_manager: SkillManager | None = None, skills_dir: Path | str | None = None):
        if skill_manager is not None:
            self.skill_manager = skill_manager
        else:
            default_dir = skills_dir or [
                Path(__file__).resolve().parents[2] / "skills",
                Path.home() / ".qgis_hermes_agent" / "custom_skills",
            ]
            self.skill_manager = SkillManager(default_dir)

    def build(self, active_skill: str, qgis_context: QGISContext) -> str:
        layer_names = ", ".join(layer["name"] for layer in qgis_context.layers) or "无"
        skill_guidance = self._skill_guidance(active_skill)
        skill_prompt = self.skill_manager.compose_prompt(active_skill)
        routing_catalog = self._routing_catalog_prompt() if active_skill == "main-orchestrator" else ""
        return (
            "你是 Agent，一个运行在 QGIS 桌面插件中的 GIS 助手。"
            "当前已启用基础对话、会话存储、轻量 QGIS 上下文感知和图层管理工具。"
            "你可以通过工具列出、检查、加载、移除、缩放、导出图层，并可加载 QML 样式。"
            "当前也已启用 execute_gis_code：适用于缓冲区、裁剪、叠加、字段计算、统计汇总等需要生成结果文件的 GIS 分析任务。"
            "生成代码时优先遵循 code-generator Skill 中的 PyQGIS/Processing 模板，"
            "优先使用 processing.run 和工作目录输出路径，少手写复杂 provider 逻辑。"
            "涉及多个输入图层时，优先一次调用 inspect_layers 获取多个图层信息，避免重复调用 inspect_layer。"
            "需要修改 QGIS 工程时必须调用工具，不要假装已经完成。"
            "如果工具结果 success=false，必须如实说明操作失败和 error 原因，不能把失败解释为空结果。"
            "如果用户没有提供必要路径、图层名或图层 ID，先询问补充信息，不要猜测。"
            "但 GIS 分析生成的新结果默认保存到插件工作目录 QGIS_AGENT_WORKSPACE 并自动加载到 QGIS，"
            "不要要求用户提供保存文件夹或完整输出路径。\n\n"
            "对于分析、筛选、提取、缓冲、裁剪、叠加等会产生结果的任务，"
            "如果信息已足够，不能把“我将执行/我会使用某方法”作为最终答复；"
            "必须继续调用工具完成任务，或明确说明缺少哪些信息。\n\n"
            "能力选择遵循 Skill-first：主调度必须由当前 AI 将用户原始请求与可路由 Skill 目录中的"
            "description 做语义比较，不得依赖程序分词、关键词计数或相关性分数。"
            "如果某个专用业务 Skill 完整覆盖任务，必须优先切换到该 Skill；"
            "自定义工具只能由已匹配或已激活的 Skill 声明和调用，不能作为主调度独立路由目标。"
            "只有没有专用业务 Skill 匹配时，单个明确的标准 GIS 操作才切换到 qgis-toolbox；"
            "没有专用业务 Skill 匹配且包含两个及以上步骤、多个输入、CRS/字段推断、"
            "统计汇总或中间依赖时，才进入 gis-pipeline。"
            "QGIS 算法目录只提供描述、参数和示例；分析统一生成完整脚本并通过 execute_gis_code 一次确认执行。\n\n"
            "会话具备记忆：用户使用“导出它”“继续”“按刚才的条件”“导出公园地块”等短句时，"
            "必须结合会话记忆、上一轮工具结果和最近对话补全图层、字段、筛选条件和待办事项；"
            "不要重复询问已经由历史工具结果确认过的信息。\n\n"
            "包含多个标准 Processing 步骤且没有专用业务 Skill 匹配的 GIS 分析，"
            "必须切换到 gis-pipeline 统一规划、检索、生成和审查脚本；"
            "不要在 main-orchestrator 中直接调用 execute_gis_code。\n\n"
            "当用户说“加载 <路径> 数据/图层”时，你负责从自然语言中提取真实路径作为 load_layer.source，"
            "source 不应包含“数据”“图层”“加载”等说明性文字。\n\n"
            f"当前 Skill：{active_skill}\n"
            f"当前工程路径：{qgis_context.project_path or '未保存'}\n"
            f"当前图层数量：{qgis_context.layer_count}\n"
            f"当前图层：{layer_names}\n\n"
            f"{routing_catalog}\n\n"
            f"{skill_guidance}\n\n"
            f"{skill_prompt}"
        )

    def _routing_catalog_prompt(self) -> str:
        catalog = self.skill_manager.routing_catalog()
        if not catalog:
            return "可路由 Skill 目录：无。"
        lines = [
            "可路由 Skill 目录（未做程序语义排序，由当前 AI 根据用户原始请求选择）："
        ]
        for item in catalog:
            tags = ", ".join(str(tag) for tag in item.get("tags") or [])
            suffix = f"；tags={tags}" if tags else ""
            lines.append(f"- {item['name']}：{item['description']}{suffix}")
        return "\n".join(lines)

    def _skill_guidance(self, active_skill: str) -> str:
        if active_skill == "data-manager":
            return (
                "Data Manager 规则：优先使用图层管理工具处理图层列表、检查、加载、移除、"
                "缩放、样式和导出。路径、图层名、图层 ID、导出目标缺失时先询问用户。"
                "加载数据时从用户原话中提取真实数据路径作为 source，不把说明性文字传给工具。"
                "删除和导出必须等待确认流程完成。"
            )
        if active_skill != "main-orchestrator":
            return (
                "Active Skill 规则：当前已经完成路由，优先遵循当前 Skill 的专用工作流并继续任务。"
                "不要因为任务包含多个步骤就改回通用 gis-pipeline；只有当前 Skill 明确要求切换，"
                "或确认当前 Skill 与用户请求不匹配时，才调用 set_active_skill。"
            )
        return (
            "Main Orchestrator 规则：先由当前 AI 对照可路由 Skill 目录进行语义匹配。"
            "专用业务 Skill 的优先级高于 qgis-toolbox、gis-pipeline 和 fast-path；"
            "不得因为任务步骤多就跳过专用 Skill。"
            "只有未匹配专用 Skill，且任务只有一个明确的缓冲、裁剪、筛选、字段计算或栅格操作时，"
            "调用 set_active_skill 切换到 qgis-toolbox。"
            "只有未匹配专用 Skill，且任务包含两个及以上操作、多个图层、CRS/字段推断、"
            "空间连接或统计汇总时，"
            "直接切换到 gis-pipeline。"
            "不要直接调用 execute_gis_code。只有简单单步且非工具箱更合适的任务才可进入 fast-path。"
            "调用代码执行前必须只写入 QGIS_AGENT_WORKSPACE，"
            "QGIS_AGENT_WORKSPACE 是 execute_gis_code 执行器注入的运行时变量。"
            "分析结果默认输出到该工作目录；用户未指定文件名时，基于任务生成合理文件名，"
            "不要询问保存文件夹。"
            "代码会在当前已打开的 QGIS Python 环境中执行，不能创建 QgsApplication/QApplication，"
            "不能初始化或启动新的 QGIS。"
            "expected_outputs 必须列出每个输出文件，vector/raster 输出会自动加载到 QGIS。"
            "不要让代码直接修改父进程 QGIS 工程。"
        )
