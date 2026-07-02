"""Build a QGIS installable plugin zip from the repository root."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = ROOT.name
DIST_DIR = ROOT / "dist"
OUTPUT = DIST_DIR / f"{PLUGIN_DIR_NAME}.zip"

INCLUDE_FILES = [
    "__init__.py",
    "metadata.txt",
    "plugin.py",
]

INCLUDE_DIRS = [
    "backend",
    "config",
    "database",
    "qwebengine",
    "resources",
    "skills",
]

EXCLUDED_PARTS = {
    "__pycache__",
    "node_modules",
}


def should_include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDED_PARTS for part in rel.parts):
        return False
    if path.suffix == ".pyc":
        return False
    return True


def iter_files():
    for relative in INCLUDE_FILES:
        path = ROOT / relative
        if path.exists():
            yield path

    for relative in INCLUDE_DIRS:
        directory = ROOT / relative
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and should_include(path):
                yield path


def main() -> None:
    DIST_DIR.mkdir(exist_ok=True)
    with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as archive:
        for path in iter_files():
            archive_path = Path(PLUGIN_DIR_NAME) / path.relative_to(ROOT)
            archive.write(path, archive_path.as_posix())
    print(OUTPUT)


if __name__ == "__main__":
    main()
