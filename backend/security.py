"""Request gating: optional API-key auth, rate limiting, and response headers.

The application is a zero-config local demo by default — `python run.py` needs
no key and no configuration. Everything here is therefore driven by the
environment and defaults to the local-demo setting:

| Variable                     | Default | Effect                              |
|------------------------------|---------|-------------------------------------|
| `API_KEY`                    | unset   | when set, every `/api` route except  |
|                              |         | `/api/health` requires it            |
| `RATE_LIMIT_ENABLED`         | `1`     | master switch for the limiter        |
| `RATE_LIMIT_PER_MINUTE`      | `240`   | per caller, across all `/api` routes |
| `RATE_LIMIT_CHAT_PER_MINUTE` | `20`    | `POST /api/chat` only                |
| `ALLOWED_ORIGINS`            | `*`     | comma-separated CORS origins         |
| `TRUST_PROXY_HEADER`         | `0`     | read the caller IP from              |
|                              |         | `X-Forwarded-For` (set behind a      |
|                              |         | reverse proxy, never when exposed)   |

`/api/chat` is limited separately and far more tightly than everything else:
it is the one route that can spend provider tokens, and it implicitly creates a
learner row for an unknown id.

None of this is an identity system. It is the floor an internet-facing
deployment needs so that a single caller cannot exhaust the process, and so a
demo can be put behind a shared secret without code changes.
"""

from __future__ import annotations

import hmac
import math
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config  # noqa: F401  — importing it loads .env before we read it

#: Routes reachable without `API_KEY`. `/api/health` stays open so a load
#: balancer or uptime check never needs the secret.
PUBLIC_API_PATHS = frozenset({"/api/health"})
CHAT_PATH = "/api/chat"
#: Account recovery. Rare for a real user, constant for anyone guessing
#: at a security answer, so it gets its own tight budget.
RECOVERY_PATHS = frozenset({
    # Mailing a link is cheap for us and cheaper still for someone using it to
    # pepper an address with mail, so recovery keeps its own tight budget.
    "/api/auth/forgot", "/api/auth/reset",
})

DEFAULT_PER_MINUTE = 240
DEFAULT_CHAT_PER_MINUTE = 20
DEFAULT_RECOVERY_PER_MINUTE = 5

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class RateLimiter:
    """Sliding-window request counter, keyed by caller.

    A window is exact rather than bucketed: each caller's hit timestamps are
    kept and expired, so a burst at the end of one minute cannot be followed
    immediately by a full second burst.
    """

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> Optional[int]:
        """Record one hit for `key`.

        Returns `None` when the caller is within budget, or the number of
        seconds to wait (for `Retry-After`) when they are over it. A limit of
        zero or less disables the check entirely.
        """
        if self.limit <= 0:
            return None
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, math.ceil(hits[0] + self.window - now))
            hits.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class Gate:
    """The configured gates. Rebuilt by :func:`configure`."""

    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str,
        per_minute: int,
        chat_per_minute: int,
        recovery_per_minute: int,
        trust_proxy: bool,
    ) -> None:
        self.enabled = enabled
        self.api_key = api_key
        self.trust_proxy = trust_proxy
        self.general = RateLimiter(per_minute)
        self.chat = RateLimiter(chat_per_minute)
        self.recovery = RateLimiter(recovery_per_minute)

    def reset(self) -> None:
        self.general.reset()
        self.chat.reset()
        self.recovery.reset()


def _build(**overrides) -> Gate:
    return Gate(
        enabled=overrides.get(
            "enabled", _env_flag("RATE_LIMIT_ENABLED", True)
        ),
        api_key=overrides.get("api_key", os.environ.get("API_KEY", "").strip()),
        per_minute=overrides.get(
            "per_minute", _env_int("RATE_LIMIT_PER_MINUTE", DEFAULT_PER_MINUTE)
        ),
        chat_per_minute=overrides.get(
            "chat_per_minute",
            _env_int("RATE_LIMIT_CHAT_PER_MINUTE", DEFAULT_CHAT_PER_MINUTE),
        ),
        recovery_per_minute=overrides.get(
            "recovery_per_minute",
            _env_int("RATE_LIMIT_RECOVERY_PER_MINUTE", DEFAULT_RECOVERY_PER_MINUTE),
        ),
        trust_proxy=overrides.get(
            "trust_proxy", _env_flag("TRUST_PROXY_HEADER", False)
        ),
    )


_gate = _build()


def configure(**overrides) -> Gate:
    """Rebuild the gates from the environment, applying any overrides.

    Called at startup, and by tests that need a specific limit.
    """
    global _gate
    _gate = _build(**overrides)
    return _gate


def current() -> Gate:
    return _gate


def allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def status() -> dict:
    """What `/api/health` reports about the gates (never the key itself)."""
    gate = current()
    return {
        "auth_required": bool(gate.api_key),
        "rate_limit_enabled": gate.enabled,
        "rate_limit_per_minute": gate.general.limit,
        "chat_rate_limit_per_minute": gate.chat.limit,
        "recovery_rate_limit_per_minute": gate.recovery.limit,
    }


def _caller(request: Request) -> str:
    gate = current()
    if gate.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "anonymous"


def _presented_key(request: Request) -> str:
    header = request.headers.get("x-api-key", "").strip()
    if header:
        return header
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _authorize(request: Request, path: str) -> Optional[JSONResponse]:
    gate = current()
    if not gate.api_key or path in PUBLIC_API_PATHS:
        return None
    # compare_digest so a wrong key cannot be found one character at a time.
    if hmac.compare_digest(_presented_key(request), gate.api_key):
        return None
    return JSONResponse(
        status_code=401,
        content={"detail": "missing or invalid API key"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _rate_limit(request: Request, path: str) -> Optional[JSONResponse]:
    gate = current()
    if not gate.enabled:
        return None
    caller = _caller(request)
    if request.method == "POST" and path in RECOVERY_PATHS:
        retry_after = gate.recovery.check(f"recovery:{caller}")
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "too many recovery attempts — try again shortly"},
                headers={"Retry-After": str(retry_after)},
            )
    if request.method == "POST" and path == CHAT_PATH:
        retry_after = gate.chat.check(f"chat:{caller}")
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "too many chat requests — slow down and try again"
                },
                headers={"Retry-After": str(retry_after)},
            )
    retry_after = gate.general.check(f"api:{caller}")
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )
    return None


async def gate_request(request: Request, call_next):
    """HTTP middleware: authenticate, rate limit, then harden the response."""
    path = request.url.path
    if path.startswith("/api/"):
        denied = _authorize(request, path) or _rate_limit(request, path)
        if denied is not None:
            for header, value in SECURITY_HEADERS.items():
                denied.headers.setdefault(header, value)
            return denied

    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response
