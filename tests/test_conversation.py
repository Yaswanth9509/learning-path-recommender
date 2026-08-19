"""How the assistant handles the messages people actually type.

Every test here runs with no provider configured, because the rule engine is
what a learner sees whenever a key is missing, a quota is spent, or a request
fails — which in practice is often. It has to be good on its own.
"""

from __future__ import annotations

import pytest

from backend import assistant


def _new_learner(client) -> str:
    return client.post("/api/learners", json={}).json()["learner_id"]


def _say(client, learner_id: str, message: str) -> dict:
    return client.post(
        "/api/chat", json={"learner_id": learner_id, "message": message}
    ).json()


def _start_path(client, learner_id: str, message: str) -> dict:
    """State a goal and answer the intake question, leaving a built path.

    The assistant asks for experience and weekly hours before it builds
    anything, so a goal statement on its own is deliberately not enough.
    """
    body = _say(client, learner_id, message)
    if not body["path_generated"]:
        body = _say(client, learner_id, "I am a beginner with 8 hours a week")
    return body


# ---------------------------------------------------------------- typos
@pytest.mark.parametrize("message,expected", [
    ("i wnat to be data analist", "goal-data-analyst"),
    ("i want to lern machin lerning", "goal-ml-engineer"),
    ("i want to be a frontend enginer", "goal-frontend"),
    ("i wanna become devops engnr", "goal-devops"),
    ("plz help me become a data analyst", "goal-data-analyst"),
])
def test_misspelled_goals_are_still_understood(catalog, message, expected):
    ranked = assistant._score_goals(catalog, message)
    assert ranked, f"no goal matched {message!r}"
    assert ranked[0][0].id == expected


def test_clean_spelling_still_outscores_a_typo(catalog):
    """A fuzzy hit counts, but never as much as getting it right."""
    exact = assistant._score_goals(catalog, "i want to be a data analyst")
    fuzzy = assistant._score_goals(catalog, "i want to be a data analsyt")
    assert exact[0][0].id == fuzzy[0][0].id == "goal-data-analyst"
    assert exact[0][1] > fuzzy[0][1]


def test_common_misspellings_are_corrected_outright(catalog):
    """Words in the shorthand map resolve exactly, not just fuzzily."""
    assert assistant._normalise("data analist") == "data analyst"
    assert assistant._normalise("machin lerning") == "machine learning"


def test_shorthand_is_expanded_before_matching():
    assert assistant._normalise("wat shud i do") == "what should i do"
    assert assistant._normalise("i wnat 2 lern") == "i want 2 learn"
    assert assistant._normalise("Hey!!! plz help me...") == "hey please help me"


def test_a_typo_still_builds_a_path_end_to_end(client):
    learner_id = _new_learner(client)
    # The hours are stated, so only the experience level is still missing.
    asked = _say(client, learner_id, "i wnat to be a data analist, 6 hrs a week")
    assert asked["path_generated"] is False
    body = _say(client, learner_id, "I am a complete beginner")
    assert body["path_generated"] is True
    assert body["profile"]["goal_id"] == "goal-data-analyst"
    assert body["profile"]["weekly_hours"] == 6


# ------------------------------------------------------------- small talk
@pytest.mark.parametrize("greeting", ["hi", "Hello!", "hey there", "hii", "namaste"])
def test_greetings_get_a_welcome_not_an_interrogation(client, greeting):
    learner_id = _new_learner(client)
    body = _say(client, learner_id, greeting)
    reply = body["reply"].lower()
    assert "hello" in reply or "hi" in reply
    assert "roadmap" in reply or "learn" in reply
    assert body["path_generated"] is False
    # It still offers somewhere to go next.
    assert body["suggested_replies"]


@pytest.mark.parametrize("message", [
    "help", "what is this", "what can you do", "how does this work",
])
def test_meta_questions_explain_the_product(client, message):
    learner_id = _new_learner(client)
    reply = _say(client, learner_id, message)["reply"].lower()
    assert any(word in reply for word in ("goal", "roadmap", "order", "skills"))
    assert "tell me" in reply or "what you want" in reply


def test_i_dont_know_offers_a_way_in_rather_than_repeating_the_question(client):
    learner_id = _new_learner(client)
    reply = _say(client, learner_id, "i dont know what i want")["reply"].lower()
    assert "data" in reply and "web" in reply.replace("web development", "web")
    assert "enjoy" in reply or "all day" in reply


def test_frustration_is_answered_with_something_actionable(client):
    learner_id = _new_learner(client)
    _start_path(client, learner_id, "I want to be a data analyst")
    reply = _say(client, learner_id, "this is useless").lower() if False else _say(
        client, learner_id, "this is useless")["reply"].lower()
    assert any(word in reply for word in ("hours", "profile", "goal", "change"))


def test_thanks_is_brief(client):
    learner_id = _new_learner(client)
    _start_path(client, learner_id, "I want to be a data analyst")
    reply = _say(client, learner_id, "thanks")["reply"]
    assert len(reply) < 300


def test_small_talk_does_not_swallow_a_real_goal(catalog):
    """"Hi, I want to be a data analyst" is a goal, not a greeting."""
    assert assistant.small_talk(catalog, "hi i want to be a data analyst", False) is None
    assert assistant.clarification(catalog, "I want to be a data analyst") is None


# -------------------------------------------------------- stating a new goal
def test_naming_a_different_goal_switches_without_ceremony(client):
    learner_id = _new_learner(client)
    _say(client, learner_id, "I want to be a machine learning engineer")
    before = client.get(f"/api/learners/{learner_id}/path").json()

    body = _say(client, learner_id, "i wnat to be data analist")
    assert body["path_generated"] is True
    assert body["profile"]["goal_id"] == "goal-data-analyst"
    after = client.get(f"/api/learners/{learner_id}/path").json()
    assert after["goal_id"] == "goal-data-analyst"
    assert after["goal_id"] != before["goal_id"]

    # The first roadmap is kept, not overwritten — they are running two goals.
    paths = client.get(f"/api/learners/{learner_id}/paths").json()
    assert {p["goal_id"] for p in paths} == {"goal-ml-engineer", "goal-data-analyst"}
    assert [p["goal_id"] for p in paths if p["is_active"]] == ["goal-data-analyst"]


@pytest.mark.parametrize("question", [
    "should i be a data analyst?",
    "what does a data analyst do?",
    "is data analyst better than frontend?",
])
def test_asking_about_a_goal_never_switches_to_it(client, question):
    learner_id = _new_learner(client)
    _say(client, learner_id, "I want to be a machine learning engineer")

    body = _say(client, learner_id, question)
    assert body["path_generated"] is False
    assert body["profile"]["goal_id"] == "goal-ml-engineer"


def test_reads_as_goal_statement():
    assert assistant.reads_as_goal_statement("i want to be a data analyst")
    assert assistant.reads_as_goal_statement("make me a devops engineer")
    assert not assistant.reads_as_goal_statement("should i learn python?")
    assert not assistant.reads_as_goal_statement("what is a data analyst")


# ------------------------------------------------- declaring a skill mid-chat
def test_saying_you_already_know_something_shrinks_the_path(client):
    learner_id = _new_learner(client)
    _say(client, learner_id, "I want to be a data analyst")
    before = client.get(f"/api/learners/{learner_id}/path").json()

    body = _say(client, learner_id, "i already know sql")
    profile = body["profile"]
    assert "sql" in profile["declared_skills"]
    assert "rebuilt" in body["reply"].lower()

    after = client.get(f"/api/learners/{learner_id}/path").json()
    assert after["total_hours"] < before["total_hours"]
    assert "sql" not in after["gap"]["missing_skills"]


def test_asking_about_a_skill_does_not_claim_it(client):
    learner_id = _new_learner(client)
    _say(client, learner_id, "I want to be a data analyst")

    body = _say(client, learner_id, "do i need to know sql?")
    assert "sql" not in body["profile"]["declared_skills"]


def test_declares_existing_knowledge():
    assert assistant.declares_existing_knowledge("i already know sql")
    assert assistant.declares_existing_knowledge("im comfortable with python")
    assert not assistant.declares_existing_knowledge("do i need sql?")
    assert not assistant.declares_existing_knowledge("teach me sql")


# ------------------------------------------------------------- reassurance
def test_feeling_overwhelmed_gets_a_concrete_answer(client):
    learner_id = _new_learner(client)
    _start_path(client, learner_id, "I want to be a machine learning engineer")

    reply = _say(client, learner_id, "is it too much for me")["reply"].lower()
    assert "hours" in reply
    assert "weekly hours" in reply or "profile" in reply
    assert "too hard" in reply


def test_the_name_reads_as_one_sentence(client):
    """Guards against "Priya, Start with …" — a capital letter mid-sentence."""
    learner_id = _new_learner(client)
    client.patch(f"/api/learners/{learner_id}/profile?regenerate=false",
                 json={"name": "Priya"})
    _start_path(client, learner_id, "I want to be a data analyst")

    reply = _say(client, learner_id, "wat shud i do first")["reply"]
    assert "Priya, " in reply
    after_name = reply.split("Priya, ", 1)[1]
    assert not after_name[:1].isupper() or after_name.startswith("“")
