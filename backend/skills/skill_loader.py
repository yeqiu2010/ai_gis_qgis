"""Filesystem skill loader with lightweight frontmatter support."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


class SkillLoader:
    def __init__(self, skills_dir: Path | str):
        self.skills_dir = Path(skills_dir)

    def load_all(self) -> dict[str, SkillDocument]:
        documents = {}
        if not self.skills_dir.exists():
            return documents
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            document = self.load(path)
            documents[document.name] = document
        return documents

    def load(self, path: Path) -> SkillDocument:
        raw = path.read_text(encoding="utf-8")
        metadata, body = self._split_frontmatter(raw)
        fallback_name = path.parent.name
        return SkillDocument(
            name=str(metadata.get("name") or fallback_name),
            description=str(metadata.get("description") or ""),
            tools=list(metadata.get("tools") or []),
            includes=list(metadata.get("includes") or []),
            tags=list(metadata.get("tags") or []),
            version=str(metadata.get("version") or ""),
            body=body.strip(),
            path=path,
        )

    def _split_frontmatter(self, raw: str) -> tuple[dict[str, Any], str]:
        if not raw.startswith("---\n"):
            return {}, raw
        end = raw.find("\n---", 4)
        if end < 0:
            return {}, raw
        frontmatter = raw[4:end]
        body = raw[end + 4 :].lstrip()
        return self._parse_frontmatter(frontmatter), body

    def _parse_frontmatter(self, frontmatter: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        current_key: str | None = None
        for raw_line in frontmatter.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if line.startswith("  - ") and current_key:
                values.setdefault(current_key, []).append(line[4:].strip())
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if not value:
                values[key] = []
            elif value.startswith("[") and value.endswith("]"):
                values[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            else:
                values[key] = value
        return values
