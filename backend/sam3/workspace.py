"""Persistent workspaces for SAM3 jobs."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Sam3Workspace:
    job_id: str
    directory: Path
    request_path: Path
    mask_path: Path
    vector_path: Path
    manifest_path: Path

    @classmethod
    def create(cls, root: str | Path) -> Sam3Workspace:
        job_id = str(uuid.uuid4())
        directory = Path(root).expanduser() / time.strftime("%Y%m%d") / job_id
        directory.mkdir(parents=True, exist_ok=False)
        return cls(
            job_id=job_id,
            directory=directory.resolve(),
            request_path=(directory / "request_rgb.tif").resolve(),
            mask_path=(directory / "sam3_mask.tif").resolve(),
            vector_path=(directory / "sam3_objects.gpkg").resolve(),
            manifest_path=(directory / "sam3_job.json").resolve(),
        )

    def write_manifest(self, payload: dict[str, Any]) -> None:
        safe_payload = dict(payload)
        safe_payload.pop("api_token", None)
        self.manifest_path.write_text(
            json.dumps(safe_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
