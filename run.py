"""Development entry point.

    python run.py            # http://127.0.0.1:8000
    python run.py --sqlite   # ignore DATABASE_URL, use the local file
    python run.py --port 9000 --reload
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description="Rungs server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="auto-reload on edits")
    parser.add_argument(
        "--sqlite",
        action="store_true",
        help="ignore DATABASE_URL and use the local SQLite file",
    )
    args = parser.parse_args()

    # A remote database costs a round trip per query — about 300ms from here to
    # a Neon instance in another region, and a page load makes several. That is
    # the right trade in production, where the app and the database sit in the
    # same region, and the wrong one for local work.
    if args.sqlite:
        os.environ["DATABASE_URL"] = ""
        print("Storage: local SQLite file (DATABASE_URL ignored)")

    # Fail fast and legibly if the catalog data is inconsistent.
    from backend.catalog import CatalogError, get_catalog

    try:
        catalog = get_catalog()
    except CatalogError as exc:
        print(f"Catalog failed validation: {exc}", file=sys.stderr)
        return 1

    from backend.sqlstore import database_url, redact

    print(
        f"Catalog OK — {len(catalog.skills)} skills, {len(catalog.items)} items, "
        f"{len(catalog.goals)} goals"
    )
    url = database_url()
    print(f"Storage: {'postgres at ' + redact(url) if url else 'sqlite'}")
    print(f"Open http://{args.host}:{args.port}/ in your browser")
    uvicorn.run(
        "backend.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
