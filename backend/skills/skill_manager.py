"""Skill discovery, progressive loading, contracts, and prompt composition."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .skill_loader import SkillDocument, SkillLoader


class SkillManager:
    def __init__(self, skills_dir: Path | str | list[Path | str]):
        self.loader = SkillLoader(skills_dir)
        self._documents = self.loader.load_all()
        self._aliases: dict[str, str] = {}
        self._runtime_available_tools: set[str] | None = None
        self._runtime_active_toolsets: set[str] | None = None

    def reload(self) -> None:
        self._documents = self.loader.load_all()
        self._aliases = {}

    def register_skill(
        self,
        name: str,
        path: Path | str,
        *,
        source: str,
        aliases: Iterable[str] = (),
        read_only: bool = True,
    ) -> SkillDocument:
        """Register a Plugin-provided Skill without changing AgentCore."""
        canonical = str(name).strip()
        if not canonical:
            raise ValueError("Skill name cannot be empty")
        document = replace(
            self.loader.load(Path(path)),
            name=canonical,
            source=source,
            read_only=read_only,
        )
        existing = self._documents.get(canonical)
        if existing is not None and existing.source != source:
            raise ValueError(
                f"Skill name collision: {canonical} ({existing.source} vs {source})"
            )
        self._documents[canonical] = document
        for raw_alias in aliases:
            alias = str(raw_alias).strip()
            if alias and alias != canonical:
                self._aliases[alias] = canonical
        return document

    def canonical_name(self, name: str) -> str:
        return self._aliases.get(str(name), str(name))

    def get(self, name: str) -> SkillDocument | None:
        return self._documents.get(self.canonical_name(name))

    def all(self) -> dict[str, SkillDocument]:
        return dict(self._documents)

    def set_runtime_capabilities(
        self,
        *,
        available_tools: set[str],
        active_toolsets: set[str],
    ) -> None:
        self._runtime_available_tools = set(available_tools)
        self._runtime_active_toolsets = set(active_toolsets)

    def availability(self, name: str) -> tuple[bool, list[str]]:
        document = self.get(name)
        if document is None:
            return False, [f"Skill 不存在: {name}"]
        return self._availability_for_document(
            document,
            available_tools=self._runtime_available_tools,
            active_toolsets=self._runtime_active_toolsets,
        )

    def tool_allowlist(self) -> dict[str, list[str]]:
        return {name: document.tools for name, document in self._documents.items() if document.tools}

    def routing_catalog(
        self,
        *,
        include_builtin: bool = True,
        available_tools: set[str] | None = None,
        active_toolsets: set[str] | None = None,
        include_unavailable: bool = False,
    ) -> list[dict[str, object]]:
        """Return lightweight Skill cards without loading full instructions."""
        included_names = {
            include_name
            for document in self._documents.values()
            for include_name in document.includes
        }
        catalog: list[dict[str, object]] = []
        aliased_documents = set(self._aliases)
        for document in self._documents.values():
            if document.name in aliased_documents:
                continue
            is_custom = ".qgis_hermes_agent" in str(document.path)
            if not include_builtin and not is_custom:
                continue
            if document.name == "main-orchestrator" or document.name in included_names:
                continue
            if not document.description:
                continue
            available, reasons = self._availability_for_document(
                document,
                available_tools=(
                    available_tools
                    if available_tools is not None
                    else self._runtime_available_tools
                ),
                active_toolsets=(
                    active_toolsets
                    if active_toolsets is not None
                    else self._runtime_active_toolsets
                ),
            )
            if not available and not include_unavailable:
                continue
            catalog.append(self.skill_card(document, available=available, reasons=reasons))
        return sorted(catalog, key=lambda item: str(item["name"]))

    @staticmethod
    def _availability_for_document(
        document: SkillDocument,
        *,
        available_tools: set[str] | None,
        active_toolsets: set[str] | None,
    ) -> tuple[bool, list[str]]:
        _, reasons = document.availability(
            available_tools=available_tools,
            active_toolsets=active_toolsets,
        )
        if available_tools is not None:
            missing_declared = sorted(set(document.tools) - available_tools)
            if missing_declared:
                reasons.append("Skill 声明了未注册工具: " + ", ".join(missing_declared))
        return not reasons, reasons

    def skill_card(
        self,
        document: SkillDocument,
        *,
        available: bool | None = None,
        reasons: list[str] | None = None,
    ) -> dict[str, object]:
        if available is None:
            available, reasons = self.availability(document.name)
        contract = document.execution_contract
        return {
            "name": document.name,
            "description": document.description,
            "version": document.version,
            "tags": list(document.tags),
            "related_skills": list(document.related_skills),
            "custom": ".qgis_hermes_agent" in str(document.path),
            "source": document.source,
            "read_only": document.read_only,
            "lifecycle": document.lifecycle or "persistent",
            "available": bool(available),
            "unavailable_reasons": list(reasons or []),
            "inputs": contract.get("inputs") or {},
            "outputs": contract.get("outputs") or {},
            "side_effects": contract.get("side_effects") or {},
        }

    def inspect(self, name: str, *, include_body: bool = True) -> dict[str, Any] | None:
        document = self.get(name)
        if document is None:
            return None
        available, reasons = self.availability(document.name)
        result: dict[str, Any] = {
            **self.skill_card(document, available=available, reasons=reasons),
            "path": str(document.path),
            "tools": list(document.tools),
            "includes": list(document.includes),
            "requires_tools": list(document.requires_tools),
            "requires_toolsets": list(document.requires_toolsets),
            "required_environment_variables": list(document.required_environment_variables),
            "execution_contract": dict(document.execution_contract),
        }
        if include_body:
            result["content"] = document.body
        return result

    def compose_prompt(self, name: str) -> str:
        """Backward-compatible single-Skill prompt composition."""
        document = self.get(name)
        if document is None:
            return ""
        parts = [self._format_document(document, "Active Skill")]
        self._append_includes(parts, document, seen={document.name})
        return "\n\n".join(parts)

    def compose_loaded_prompt(
        self,
        names: Iterable[str],
        *,
        active_skill: str | None = None,
        include_names: Iterable[str] | None = None,
    ) -> str:
        ordered = self.normalize_loaded_skills(names)
        parts: list[str] = []
        seen: set[str] = set()
        for name in ordered:
            document = self.get(name)
            if document is None or document.name in seen:
                continue
            if document.name == active_skill:
                label = "Active Skill"
            elif document.name == "main-orchestrator":
                label = "Coordinator Skill"
            else:
                # Inactive Skills remain discoverable as cards, but their full
                # instructions must not permanently inflate every later turn.
                parts.append(self._format_card(document, "Loaded Skill Card"))
                seen.add(document.name)
                continue
            parts.append(self._format_document(document, label))
            seen.add(document.name)
            if document.name == active_skill:
                allowed_includes = (
                    {self.canonical_name(name) for name in include_names}
                    if include_names is not None
                    else None
                )
                self._append_includes(
                    parts,
                    document,
                    seen=seen,
                    allowed=allowed_includes,
                )
        return "\n\n".join(parts)

    def normalize_loaded_skills(
        self,
        names: Iterable[str],
        *,
        max_skills: int = 5,
        include_coordinator: bool = True,
    ) -> list[str]:
        ordered: list[str] = []
        if include_coordinator and "main-orchestrator" in self._documents:
            ordered.append("main-orchestrator")
        for raw_name in names:
            name = self.canonical_name(str(raw_name).strip())
            if not name or name in ordered or name not in self._documents:
                continue
            ordered.append(name)
            if len(ordered) >= max_skills:
                break
        return ordered

    def load_reference(self, name: str, relative_path: str) -> str:
        document = self.get(name)
        if document is None:
            raise KeyError(f"Skill 不存在: {name}")
        requested = str(relative_path or "").strip().replace("\\", "/")
        if not requested or requested.startswith("/"):
            raise ValueError("path 必须是 Skill 目录内的相对路径")
        root = document.path.parent.resolve()
        target = (root / requested).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError("只能读取 Skill 目录中的现有文件")
        return target.read_text(encoding="utf-8")

    def _append_includes(
        self,
        parts: list[str],
        document: SkillDocument,
        *,
        seen: set[str],
        allowed: set[str] | None = None,
    ) -> None:
        for include_name in document.includes:
            included = self.get(include_name)
            if included is None or included.name in seen:
                continue
            if allowed is not None and included.name not in allowed:
                continue
            parts.append(self._format_document(included, "Included Skill"))
            seen.add(included.name)
            self._append_includes(parts, included, seen=seen, allowed=allowed)

    def _format_document(self, document: SkillDocument, label: str) -> str:
        header = f"## {label}: {document.name}"
        if document.description:
            header = f"{header}\n描述：{document.description}"
        if document.tools:
            header = f"{header}\n允许工具：{', '.join(document.tools)}"
        return f"{header}\n\n{document.body}"

    @staticmethod
    def _format_card(document: SkillDocument, label: str) -> str:
        header = f"## {label}: {document.name}"
        if document.description:
            header += f"\n描述：{document.description}"
        if document.lifecycle:
            header += f"\n生命周期：{document.lifecycle}"
        return header
