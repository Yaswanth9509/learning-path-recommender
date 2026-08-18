"""Runtime configuration.

The application runs fully without any LLM: every AI-backed feature has a
deterministic implementation behind it. A provider upgrades quality; it is never
required for correctness. See `llm.py` for the providers themselves.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Token budgets. On models with thinking enabled these cap thinking plus
#: response text together, so both leave deliberate headroom.
CHAT_MAX_TOKENS = 4000
EXTRACT_MAX_TOKENS = 3000

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader so users need no extra dependency."""
    env_file = path or ENV_FILE
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables always win over the file.
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def get_settings() -> dict[str, Any]:
    """Current assistant configuration, as reported by /api/health."""
    from . import llm

    return llm.status()
