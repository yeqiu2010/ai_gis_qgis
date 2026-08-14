"""Filesystem loader for Hermes-compatible ``SKILL.md`` documents."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SkillDocument:
    name: str
    body: str
    path: Path
    description: str = ""
    tools: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str = ""
    lifecycle: str = ""
    author: str = ""
    license: str = ""
    platforms: list[str] = field(default_factory=list)
    related_skills: list[str] = field(default_factory=list)
    requires_toolsets: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    fallback_for_toolsets: list[str] = field(default_factory=list)
    fallback_for_tools: list[str] = field(default_factory=list)
    required_environment_variables: list[dict[str, Any]] = field(default_factory=list)
    hermes_config: list[dict[str, Any]] = field(default_factory=list)
    execution_contract: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "filesystem"
    read_only: bool = False

    def availability(
        self,
        *,
        available_tools: set[str] | None = None,
        active_toolsets: set[str] | None = None,
        environment: dict[str, str] | None = None,
        current_platform: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Return whether this Skill can run in the supplied environment."""
        reasons: list[str] = []
        normalized_platform = _normalize_platform(current_platform or platform.system())
        if self.platforms and normalized_platform not in {
            _normalize_platform(value) for value in self.platforms
        }:
            reasons.append(f"当前平台 {normalized_platform} 不在支持列表中")

        if available_tools is not None:
            missing_tools = sorted(set(self.requires_tools) - available_tools)
            if missing_tools:
                reasons.append("缺少工具: " + ", ".join(missing_tools))
            if set(self.fallback_for_tools).intersection(available_tools):
                reasons.append("存在优先工具，当前 Skill 仅作为 fallback")

        if active_toolsets is not None:
            missing_toolsets = sorted(set(self.requires_toolsets) - active_toolsets)
            if missing_toolsets:
                reasons.append("缺少 toolset: " + ", ".join(missing_toolsets))
            if set(self.fallback_for_toolsets).intersection(active_toolsets):
                reasons.append("存在优先 toolset，当前 Skill 仅作为 fallback")

        env = environment if environment is not None else os.environ
        missing_environment = [
            str(item.get("name") or "").strip()
            for item in self.required_environment_variables
            if str(item.get("name") or "").strip() and not env.get(str(item["name"]))
        ]
        if missing_environment:
            reasons.append("缺少环境变量: " + ", ".join(missing_environment))
        return not reasons, reasons


class SkillLoader:
    def __init__(self, skills_dir: Path | str | list[Path | str]):
        if isinstance(skills_dir, list):
            self.skills_dirs = [Path(path).expanduser() for path in skills_dir]
        else:
            self.skills_dirs = [Path(skills_dir).expanduser()]
        self.skills_dir = self.skills_dirs[0]

    def load_all(self) -> dict[str, SkillDocument]:
        documents: dict[str, SkillDocument] = {}
        # Later directories override earlier ones, preserving the plugin's
        # existing custom-Skill-over-builtin precedence.
        for skills_dir in self.skills_dirs:
            if not skills_dir.exists():
                continue
            for path in sorted(skills_dir.glob("*/SKILL.md")):
                document = self.load(path)
                documents[document.name] = document
        return documents

    def load(self, path: Path) -> SkillDocument:
        raw = path.read_text(encoding="utf-8")
        metadata, body = self._split_frontmatter(raw)
        nested_metadata = _as_dict(metadata.get("metadata"))
        hermes = _as_dict(nested_metadata.get("hermes"))
        qgis_agent = _as_dict(nested_metadata.get("qgis_agent"))
        fallback_name = path.parent.name

        tools = _as_list(metadata.get("tools"))
        if not tools:
            tools = _as_list(metadata.get("allowed-tools"))

        required_environment_variables = [
            item
            for item in _as_dict_list(metadata.get("required_environment_variables"))
            if str(item.get("name") or "").strip()
        ]
        return SkillDocument(
            name=str(metadata.get("name") or fallback_name).strip(),
            description=str(metadata.get("description") or "").strip(),
            tools=tools,
            includes=_as_list(metadata.get("includes")),
            tags=_first_list(
                metadata.get("tags"),
                nested_metadata.get("tags"),
                hermes.get("tags"),
            ),
            version=str(metadata.get("version") or nested_metadata.get("version") or "").strip(),
            lifecycle=str(
                metadata.get("lifecycle") or nested_metadata.get("lifecycle") or ""
            ).strip(),
            author=str(metadata.get("author") or "").strip(),
            license=str(metadata.get("license") or "").strip(),
            platforms=_as_list(metadata.get("platforms")),
            related_skills=_as_list(hermes.get("related_skills")),
            requires_toolsets=_as_list(hermes.get("requires_toolsets")),
            requires_tools=_as_list(hermes.get("requires_tools")),
            fallback_for_toolsets=_as_list(hermes.get("fallback_for_toolsets")),
            fallback_for_tools=_as_list(hermes.get("fallback_for_tools")),
            required_environment_variables=required_environment_variables,
            hermes_config=_as_dict_list(hermes.get("config")),
            execution_contract=qgis_agent,
            metadata=nested_metadata,
            body=body.strip(),
            path=path,
        )

    def _split_frontmatter(self, raw: str) -> tuple[dict[str, Any], str]:
        if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
            return {}, raw
        lines = raw.splitlines(keepends=True)
        end_index = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        if end_index is None:
            return {}, raw
        frontmatter = "".join(lines[1:end_index])
        parsed = yaml.safe_load(frontmatter) or {}
        if not isinstance(parsed, dict):
            raise ValueError("SKILL.md YAML Frontmatter 必须是 object")
        return parsed, "".join(lines[end_index + 1 :]).lstrip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split() if item.strip()]
    return []


def _first_list(*values: Any) -> list[str]:
    for value in values:
        parsed = _as_list(value)
        if parsed:
            return parsed
    return []


def _normalize_platform(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if normalized.startswith("win"):
        return "windows"
    if normalized in {"linux", "linux2"}:
        return "linux"
    return normalized
