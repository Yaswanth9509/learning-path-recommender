"""Package the project into a clean, shareable zip.

Excludes build artefacts, local database state, and secrets — the same things
.gitignore excludes. Run from anywhere:

    python make_zip.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / f"{ROOT.name.replace(' ', '-')}.zip"

EXCLUDE_DIRS = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", "venv",
    ".idea", "htmlcov", "build", "dist",
}
EXCLUDE_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".zip"}
EXCLUDE_NAMES = {".env", ".coverage", "coverage.xml"}


def included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return path.name not in EXCLUDE_NAMES


def main() -> int:
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p))
    if not files:
        print("Nothing to package.", file=sys.stderr)
        return 1

    if OUT.exists():
        OUT.unlink()

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, arcname=Path(ROOT.name) / path.relative_to(ROOT))

    size_kb = OUT.stat().st_size / 1024
    print(f"Created {OUT}")
    print(f"{len(files)} files, {size_kb:.1f} KB")
    for path in files:
        print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
