"""The request gates: API key, rate limiting, payload bounds, headers.

Every test here configures the gates explicitly and the autouse fixture in
`conftest.py` restores the environment defaults afterwards, so nothing leaks
into the rest of the suite.
"""

from __future__ import annotations

import pytest

from backend import security
from backend.models import MAX_MESSAGE_CHARS


def _new_learner(client) -> str:
    return client.post("/api/learners", json={}).json()["learner_id"]


# ------------------------------------------------------------- the limiter
def test_limiter_allows_up_to_the_limit_then_blocks():
    now = [1000.0]
    limiter = security.RateLimiter(limit=3, window=60.0, clock=lambda: now[0])

    assert [limiter.check("caller") for _ in range(3)] == [None, None, None]
    retry_after = limiter.check("caller")
    assert retry_after is not None and 1 <= retry_after <= 60


def test_limiter_window_slides_rather_than_resetting():
    now = [0.0]
    limiter = security.RateLimiter(limit=2, window=60.0, clock=lambda: now[0])

    limiter.check("caller")          # t=0
    now[0] = 30.0
    limiter.check("caller")          # t=30
    assert limiter.check("caller") is not None

    now[0] = 61.0                    # the t=0 hit has expired, the t=30 has not
    assert limiter.check("caller") is None
    assert limiter.check("caller") is not None


def test_limiter_keys_callers_separately():
    limiter = security.RateLimiter(limit=1)
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None


def test_limit_of_zero_disables_the_check():
    limiter = security.RateLimiter(limit=0)
    assert all(limiter.check("caller") is None for _ in range(50))


# ------------------------------------------------------------- rate limiting
def test_chat_is_rate_limited_before_the_general_budget(client):
    learner_id = _new_learner(client)
    security.configure(enabled=True, per_minute=1000, chat_per_minute=2)

    body = {"learner_id": learner_id, "message": "I want to be a data analyst"}
    assert client.post("/api/chat", json=body).status_code == 200
    assert client.post("/api/chat", json=body).status_code == 200

    limited = client.post("/api/chat", json=body)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert "chat" in limited.json()["detail"]

    # Reads are on the separate, larger budget and still work.
    assert client.get(f"/api/learners/{learner_id}/profile").status_code == 200


def test_general_rate_limit_applies_to_every_api_route(client):
    security.configure(enabled=True, per_minute=2, chat_per_minute=100)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/catalog/goals").status_code == 200
    limited = client.get("/api/catalog/skills")
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate limit exceeded"


def test_static_pages_are_not_rate_limited(client):
    security.configure(enabled=True, per_minute=1, chat_per_minute=1)
    client.get("/api/health")
    for _ in range(5):
        assert client.get("/").status_code == 200


def test_disabled_limiter_never_blocks(client):
    security.configure(enabled=False, per_minute=1, chat_per_minute=1)
    for _ in range(10):
        assert client.get("/api/health").status_code == 200


# ------------------------------------------------------------------- api key
def test_api_key_is_required_when_configured(client):
    security.configure(api_key="s3cret")

    denied = client.get("/api/catalog/goals")
    assert denied.status_code == 401
    assert "api key" in denied.json()["detail"].lower()

    assert client.get(
        "/api/catalog/goals", headers={"X-API-Key": "s3cret"}
    ).status_code == 200
    assert client.get(
        "/api/catalog/goals", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200
    assert client.get(
        "/api/catalog/goals", headers={"X-API-Key": "wrong"}
    ).status_code == 401


def test_health_stays_public_so_probes_need_no_secret(client):
    security.configure(api_key="s3cret")
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["security"]["auth_required"] is True


def test_no_api_key_means_no_authentication(client):
    security.configure(api_key="")
    assert client.get("/api/catalog/goals").status_code == 200
    assert client.get("/api/health").json()["security"]["auth_required"] is False


def test_health_reports_the_configured_limits(client):
    security.configure(enabled=True, per_minute=99, chat_per_minute=7)
    reported = client.get("/api/health").json()["security"]
    assert reported["rate_limit_enabled"] is True
    assert reported["rate_limit_per_minute"] == 99
    assert reported["chat_rate_limit_per_minute"] == 7


# ------------------------------------------------------------------- headers
def test_responses_carry_hardening_headers(client):
    for response in (client.get("/api/health"), client.get("/")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"


def test_rejected_requests_carry_them_too(client):
    security.configure(api_key="s3cret")
    denied = client.get("/api/catalog/goals")
    assert denied.status_code == 401
    assert denied.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------ payload bounds
def test_oversized_chat_message_is_rejected(client):
    learner_id = _new_learner(client)
    response = client.post("/api/chat", json={
        "learner_id": learner_id, "message": "x" * (MAX_MESSAGE_CHARS + 1),
    })
    assert response.status_code == 422


def test_message_at_the_limit_is_accepted(client):
    learner_id = _new_learner(client)
    response = client.post("/api/chat", json={
        "learner_id": learner_id, "message": "x" * MAX_MESSAGE_CHARS,
    })
    assert response.status_code == 200


@pytest.mark.parametrize("learner_id", [
    "",                      # empty
    "a" * 65,                # too long
    "../../etc/passwd",      # path-ish
    "drop table learners;",  # spaces and punctuation
])
def test_malformed_learner_ids_are_rejected(client, learner_id):
    response = client.post(
        "/api/chat", json={"learner_id": learner_id, "message": "hello"}
    )
    assert response.status_code == 422


def test_chat_still_creates_a_learner_for_a_well_formed_unknown_id(client):
    response = client.post(
        "/api/chat", json={"learner_id": "brand-new-01", "message": "hello"}
    )
    assert response.status_code == 200
    assert client.get("/api/learners/brand-new-01/profile").status_code == 200


def test_oversized_profile_fields_are_rejected(client):
    learner_id = _new_learner(client)
    too_long = client.patch(
        f"/api/learners/{learner_id}/profile", json={"name": "n" * 200}
    )
    assert too_long.status_code == 422

    out_of_range = client.patch(
        f"/api/learners/{learner_id}/profile", json={"weekly_hours": 500}
    )
    assert out_of_range.status_code == 422


def test_oversized_feedback_note_is_rejected(client):
    learner_id = _new_learner(client)
    response = client.post(f"/api/learners/{learner_id}/feedback", json={
        "item_id": "c-viz", "signal": "liked", "note": "z" * 5000,
    })
    assert response.status_code == 422


# -------------------------------------------------------------------- config
def test_allowed_origins_default_to_wildcard(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    assert security.allowed_origins() == ["*"]


def test_allowed_origins_are_parsed_from_the_environment(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_ORIGINS", "https://a.example.com, https://b.example.com"
    )
    assert security.allowed_origins() == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_gate_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "12")
    monkeypatch.setenv("RATE_LIMIT_CHAT_PER_MINUTE", "3")
    monkeypatch.setenv("API_KEY", "  from-env  ")

    gate = security.configure()
    assert gate.enabled is True
    assert gate.general.limit == 12
    assert gate.chat.limit == 3
    assert gate.api_key == "from-env"


def test_unparsable_limits_fall_back_to_the_default(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "not-a-number")
    assert security.configure().general.limit == security.DEFAULT_PER_MINUTE


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False), ("", True),
    ("1", True), ("true", True), ("yes", True),
])
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", raw)
    assert security.configure().enabled is expected
