"""The LLM-backed branch of the assistant, driven by a stubbed provider.

No key, no network, no tokens — but every line of parsing, validation, and
failure handling actually runs. The behaviour under test is defensive: a
provider is never trusted to return ids that exist, values in range, or even
valid JSON, and any failure must degrade to the rule engine rather than surface
an error to the learner.
"""

from __future__ import annotations

import json

import pytest

from backend import assistant, llm
from backend.models import LearnerProfile
from backend.pathgen import generate_path
from backend.profiling import derive


# ------------------------------------------------------------------ stubbing
class FakeProvider(llm.Provider):
    """Records what the assistant asked for, returns what the test dictates."""

    def __init__(self, response: str | None = None, error: Exception | None = None):
        super().__init__(name="fake", model="fake-model-1")
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def _complete(self, system, messages, max_tokens, schema):
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "max_tokens": max_tokens,
                "schema": schema,
            }
        )
        if self._error is not None:
            raise self._error
        return self._response


def install(monkeypatch, provider: llm.Provider | None) -> llm.Provider | None:
    monkeypatch.setattr(llm, "resolve_provider", lambda: provider)
    return provider


def payload(**overrides) -> str:
    body = {
        "goal_id": "goal-ai-engineer",
        "confidence": 0.9,
        "experience_level": "intermediate",
        "weekly_hours": 14,
        "interests": ["Data & AI"],
        "declared_skills": ["python", "sql"],
        "extra_target_skills": ["docker"],
        "preferred_formats": ["project"],
        "notes": "career pivot",
    }
    body.update(overrides)
    return json.dumps(body)


# --------------------------------------------------------------- happy path
def test_provider_interpretation_is_used(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(payload()))
    result = assistant.interpret_goal(catalog, "I want to build LLM apps")

    assert result.source == "fake"          # the provider name, not just "llm"
    assert result.goal_id == "goal-ai-engineer"
    assert result.goal_title == "AI / LLM Application Engineer"
    assert result.experience_level == "intermediate"
    assert result.weekly_hours == 14
    assert result.declared_skills == ["python", "sql"]
    assert result.extra_target_skills == ["docker"]
    assert result.preferred_formats == ["project"]
    assert result.notes == "career pivot"


def test_request_carries_schema_and_catalog(monkeypatch, catalog):
    provider = install(monkeypatch, FakeProvider(payload()))
    assistant.interpret_goal(catalog, "data analyst please")

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["schema"]["properties"]["goal_id"]
    assert call["max_tokens"] > 0
    assert "goal-ai-engineer" in call["system"]
    assert "python" in call["system"]
    assert "Never invent ids" in call["system"]


def test_json_wrapped_in_markdown_is_recovered(monkeypatch, catalog):
    """Providers without native structured output fence their JSON."""
    fenced = f"Here you go:\n```json\n{payload()}\n```\nHope that helps!"
    install(monkeypatch, FakeProvider(fenced))
    result = assistant.interpret_goal(catalog, "llm apps")
    assert result.source == "fake"
    assert result.goal_id == "goal-ai-engineer"


def test_json_embedded_in_prose_is_recovered(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(f"Sure. {payload()} Let me know."))
    result = assistant.interpret_goal(catalog, "llm apps")
    assert result.goal_id == "goal-ai-engineer"


# ------------------------------------------------------- untrusted output
def test_unknown_goal_id_falls_back_to_the_rule_match(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(payload(goal_id="goal-astronaut")))
    result = assistant.interpret_goal(catalog, "I want to be a data analyst")
    assert result.goal_id == "goal-data-analyst"  # from the keyword rules


def test_unknown_skill_ids_are_dropped(monkeypatch, catalog):
    install(
        monkeypatch,
        FakeProvider(
            payload(
                declared_skills=["python", "telepathy"],
                extra_target_skills=["docker", "warp-drive"],
            )
        ),
    )
    result = assistant.interpret_goal(catalog, "llm apps")
    assert result.declared_skills == ["python"]
    assert result.extra_target_skills == ["docker"]


def test_invalid_experience_level_is_rejected(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(payload(experience_level="wizard")))
    result = assistant.interpret_goal(catalog, "I am a complete beginner at LLMs")
    assert result.experience_level == "beginner"  # rule-derived


@pytest.mark.parametrize("bad", [0, -5, 500, "twelve", None, 3.5, True])
def test_out_of_range_weekly_hours_are_rejected(monkeypatch, catalog, bad):
    install(monkeypatch, FakeProvider(payload(weekly_hours=bad)))
    result = assistant.interpret_goal(catalog, "llm apps, 7 hours a week")
    assert result.weekly_hours == 7  # from the regex rule, not the model


def test_invalid_formats_are_filtered(monkeypatch, catalog):
    install(
        monkeypatch, FakeProvider(payload(preferred_formats=["project", "telepathy"]))
    )
    result = assistant.interpret_goal(catalog, "llm apps")
    assert result.preferred_formats == ["project"]


def test_confidence_is_clamped(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(payload(confidence=42)))
    assert assistant.interpret_goal(catalog, "llm apps").confidence == 1.0
    install(monkeypatch, FakeProvider(payload(confidence="high")))
    assert assistant.interpret_goal(catalog, "llm apps").confidence == 0.0


def test_null_collections_do_not_crash(monkeypatch, catalog):
    install(
        monkeypatch,
        FakeProvider(
            payload(
                declared_skills=None, extra_target_skills=None,
                preferred_formats=None, interests=None, notes=None,
            )
        ),
    )
    result = assistant.interpret_goal(catalog, "I want to build LLM apps")
    assert result.goal_id == "goal-ai-engineer"
    assert result.extra_target_skills == []


def test_non_object_json_falls_back(monkeypatch, catalog):
    install(monkeypatch, FakeProvider("[1, 2, 3]"))
    result = assistant.interpret_goal(catalog, "I want to be a devops engineer")
    assert result.source == "rules"


# ------------------------------------------------------------ failure modes
def test_prose_response_falls_back_to_rules(monkeypatch, catalog):
    install(monkeypatch, FakeProvider("Sure! Here's what I think you should do."))
    result = assistant.interpret_goal(catalog, "I want to be a devops engineer")
    assert result.source == "rules"
    assert result.goal_id == "goal-devops"


def test_empty_response_falls_back_to_rules(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(""))
    result = assistant.interpret_goal(catalog, "I want to be a data analyst")
    assert result.source == "rules"
    assert result.goal_id == "goal-data-analyst"


def test_provider_error_falls_back_to_rules(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(error=llm.LLMError("429 rate limited")))
    result = assistant.interpret_goal(catalog, "I want to be a cloud architect")
    assert result.source == "rules"
    assert result.goal_id == "goal-cloud-architect"


def test_unexpected_exception_falls_back_to_rules(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(error=RuntimeError("boom")))
    result = assistant.interpret_goal(catalog, "I want to be a frontend engineer")
    assert result.source == "rules"
    assert result.goal_id == "goal-frontend"


def test_no_provider_configured_falls_back_to_rules(monkeypatch, catalog):
    install(monkeypatch, None)
    result = assistant.interpret_goal(catalog, "I want to be a data analyst")
    assert result.source == "rules"


# ----------------------------------------------------------------- answering
def _path_for(catalog, goal_id="goal-frontend"):
    profile = LearnerProfile(learner_id="a", goal_id=goal_id, name="Ana")
    return profile, generate_path(catalog, derive(catalog, profile), goal_id)


def test_provider_answer_is_returned(monkeypatch, catalog):
    install(monkeypatch, FakeProvider("Start with React Fundamentals."))
    profile, path = _path_for(catalog)
    reply, source = assistant.answer(catalog, profile, path, "what next?")
    assert source == "fake"
    assert reply == "Start with React Fundamentals."


def test_answer_context_includes_persona_and_path(monkeypatch, catalog):
    provider = install(monkeypatch, FakeProvider("ok"))
    profile, path = _path_for(catalog)
    assistant.answer(catalog, profile, path, "why this order?")

    system = provider.calls[0]["system"]
    assert assistant.ASSISTANT_NAME in system
    assert path.goal_title in system
    assert path.milestones[0].items[0].title in system
    assert "Never invent a" in system
    assert provider.calls[0]["schema"] is None  # free prose, not structured


def test_answer_context_reports_rejected_items(monkeypatch, catalog):
    provider = install(monkeypatch, FakeProvider("ok"))
    profile, path = _path_for(catalog)
    profile.excluded_item_ids = ["c-react"]
    assistant.answer(catalog, profile, path, "anything")
    assert "never suggest these again" in provider.calls[0]["system"]


def test_answer_replays_conversation_history(monkeypatch, catalog):
    provider = install(monkeypatch, FakeProvider("ok"))
    profile, path = _path_for(catalog)
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "bogus", "content": "ignored"},
    ]
    assistant.answer(catalog, profile, path, "and now?", history=history)

    messages = provider.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "and now?"


def test_answer_falls_back_when_provider_fails(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(error=llm.LLMError("down")))
    profile, path = _path_for(catalog)
    reply, source = assistant.answer(catalog, profile, path, "how long will this take?")
    assert source == "rules"
    assert str(path.total_weeks) in reply


def test_goal_acknowledgement_uses_the_provider(monkeypatch, catalog):
    provider = install(monkeypatch, FakeProvider("Welcome aboard, Ana."))
    profile, path = _path_for(catalog)
    interp = assistant.interpret_goal(catalog, "frontend")
    reply, source = assistant.acknowledge_goal(catalog, profile, path, interp)
    assert source == "fake"
    assert reply == "Welcome aboard, Ana."
    assert "welcome message" in provider.calls[-1]["messages"][0]["content"]


def test_goal_acknowledgement_falls_back(monkeypatch, catalog):
    install(monkeypatch, FakeProvider(error=llm.LLMError("down")))
    profile, path = _path_for(catalog)
    interp = assistant._interpret_with_rules(catalog, "frontend engineer")
    reply, source = assistant.acknowledge_goal(catalog, profile, path, interp)
    assert source == "rules"
    assert path.goal_title in reply
    assert str(path.total_weeks) in reply


# --------------------------------------------------- end-to-end through the API
def test_chat_endpoint_reports_the_provider(monkeypatch, client):
    install(monkeypatch, FakeProvider(payload()))

    learner_id = client.post("/api/learners", json={}).json()["learner_id"]
    body = client.post(
        "/api/chat",
        json={"learner_id": learner_id, "message": "I want to build LLM apps"},
    ).json()

    assert body["source"] == "fake"
    assert body["path_generated"] is True
    assert body["profile"]["goal_id"] == "goal-ai-engineer"
    assert body["profile"]["weekly_hours"] == 14
    assert "docker" in body["profile"]["extra_target_skills"]

    # The extra target skill really does reshape the generated path.
    path = client.get(f"/api/learners/{learner_id}/path").json()
    taught = {s for m in path["milestones"] for i in m["items"] for s in i["teaches"]}
    assert "docker" in taught


def test_chat_endpoint_survives_a_dead_provider(monkeypatch, client):
    install(monkeypatch, FakeProvider(error=llm.LLMError("network down")))
    learner_id = client.post("/api/learners", json={}).json()["learner_id"]
    body = client.post(
        "/api/chat",
        json={
            "learner_id": learner_id,
            "message": "I want to be a data analyst, a beginner with 8 hours a week",
        },
    ).json()

    assert body["source"] == "rules"
    assert body["path_generated"] is True
    assert body["profile"]["goal_id"] == "goal-data-analyst"


def test_token_budgets_are_reservations_not_wishes():
    """Providers rate-limit on tokens *requested*, not tokens used.

    Measured replies run 100-270 tokens. The budgets were 4000 and 3000, which
    booked roughly twenty times what they spent — on Groq's free tier that was
    the difference between one message a minute and three. Keep them sized for
    the replies this app actually produces.
    """
    from backend.config import CHAT_MAX_TOKENS, EXTRACT_MAX_TOKENS

    #: Comfortably above the longest reply observed (~270 tokens), while
    #: leaving room for a model that thinks before answering.
    assert 600 <= CHAT_MAX_TOKENS <= 2000, (
        f"CHAT_MAX_TOKENS={CHAT_MAX_TOKENS} is out of proportion to a "
        "100-270 token reply"
    )
    #: The extraction call returns one small JSON object.
    assert 400 <= EXTRACT_MAX_TOKENS <= 1500, (
        f"EXTRACT_MAX_TOKENS={EXTRACT_MAX_TOKENS} is out of proportion to a "
        "small JSON payload"
    )
    assert EXTRACT_MAX_TOKENS <= CHAT_MAX_TOKENS
