"""Skill prompt composition with include support."""

from __future__ import annotations

from pathlib import Path

from .skill_loader import SkillDocument, SkillLoader


class SkillManager:
    def __init__(self, skills_dir: Path | str | list[Path | str]):
        self.loader = SkillLoader(skills_dir)
        self._documents = self.loader.load_all()

    def get(self, name: str) -> SkillDocument | None:
        return self._documents.get(name)

    def all(self) -> dict[str, SkillDocument]:
        return dict(self._documents)

    def tool_allowlist(self) -> dict[str, list[str]]:
        return {name: document.tools for name, document in self._documents.items() if document.tools}

    def compose_prompt(self, name: str) -> str:
        document = self.get(name)
        if document is None:
            return ""
        parts = [self._format_document(document, "Active Skill")]
        for include_name in document.includes:
            included = self.get(include_name)
            if included is not None:
                parts.append(self._format_document(included, "Included Skill"))
        return "\n\n".join(parts)

    def _format_document(self, document: SkillDocument, label: str) -> str:
        header = f"## {label}: {document.name}"
        if document.description:
            header = f"{header}\n描述：{document.description}"
        if document.tools:
            header = f"{header}\n允许工具：{', '.join(document.tools)}"
        return f"{header}\n\n{document.body}"
