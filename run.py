"""Development entry point.

    python run.py            # http://127.0.0.1:8000
    python run.py --port 9000 --reload
"""

from __future__ import annotations

import argparse
import sys

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description="Learning Path Recommender server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="auto-reload on edits")
    args = parser.parse_args()

    # Fail fast and legibly if the catalog data is inconsistent.
    from backend.catalog import CatalogError, get_catalog

    try:
        catalog = get_catalog()
    except CatalogError as exc:
        print(f"Catalog failed validation: {exc}", file=sys.stderr)
        return 1

    print(
        f"Catalog OK — {len(catalog.skills)} skills, {len(catalog.items)} items, "
        f"{len(catalog.goals)} goals"
    )
    print(f"Open http://{args.host}:{args.port}/ in your browser")
    uvicorn.run(
        "backend.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
