"""The four capabilities the brief names that go beyond a static roadmap.

* prior learning history that survives a rebuild
* pace measured from behaviour, and re-planning around it
* reference resources with real links
* asking which goal was meant instead of guessing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import assistant, insights, profiling
from backend.models import LearnerProfile, ProgressEntry
from backend.pathgen import generate_path


def _new_learner(client) -> str:
    return client.post("/api/learners", json={}).json()["learner_id"]


def _with_goal(client, message="I want to be a data analyst") -> str:
    learner_id = _new_learner(client)
    client.post("/api/chat", json={"learner_id": learner_id, "message": message})
    return learner_id


# ------------------------------------------------------- prior learning history
def test_declared_history_survives_a_path_rebuild(catalog):
    """A course finished before this app existed must not be forgotten."""
    profile = LearnerProfile(learner_id="l1", completed_item_ids=["c-js"])

    # A rebuild syncs against the progress table, which knows nothing of c-js.
    profiling.sync_completions(catalog, profile, [], tracked_item_ids=[])
    assert "c-js" in profile.completed_item_ids


def test_progress_still_owns_the_items_it_tracks(catalog):
    """Unticking a course in the progress table un-completes it here too."""
    profile = LearnerProfile(learner_id="l1", completed_item_ids=["c-js", "c-python-alt"])

    # c-js is tracked and no longer completed; c-python was never tracked.
    profiling.sync_completions(catalog, profile, [], tracked_item_ids=["c-js"])
    assert profile.completed_item_ids == ["c-python-alt"]


def test_declared_history_removes_the_skill_from_the_gap(client):
    learner_id = _with_goal(client)
    before = client.get(f"/api/learners/{learner_id}/gap").json()["report"]
    assert "sql" in before["missing_skills"]

    client.patch(
        f"/api/learners/{learner_id}/profile", json={"completed_item_ids": ["c-sql-101"]}
    )
    after = client.get(f"/api/learners/{learner_id}/gap").json()["report"]
    assert "sql" in after["known_skills"]
    assert "sql" not in after["missing_skills"]

    # And it is still there after the path is rebuilt again.
    client.post(f"/api/learners/{learner_id}/path")
    profile = client.get(f"/api/learners/{learner_id}/profile").json()
    assert "c-sql-101" in profile["completed_item_ids"]


def test_unknown_history_items_are_rejected(client):
    learner_id = _new_learner(client)
    response = client.patch(
        f"/api/learners/{learner_id}/profile", json={"completed_item_ids": ["ghost"]}
    )
    assert response.status_code == 400


# ------------------------------------------------------------------------ pace
def _path_for(catalog, profile):
    derived = profiling.derive(catalog, profile)
    return generate_path(catalog=catalog, derived=derived, goal_id=profile.goal_id)


def _progress(item_id: str, status: str, when: datetime) -> ProgressEntry:
    return ProgressEntry(item_id=item_id, status=status, updated_at=when.isoformat())


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_pace_is_unknown_until_there_is_history(catalog):
    profile = LearnerProfile(learner_id="l1", goal_id="goal-data-analyst")
    path = _path_for(catalog, profile)

    report = insights.build_pace(catalog, profile, path, {}, now=NOW)
    assert report.status == "unknown"
    assert report.suggested_weekly_hours is None
    assert "Not enough history" in report.message


def test_pace_ignores_a_burst_inside_one_week(catalog):
    """Four days of data cannot tell us anyone's weekly rate."""
    profile = LearnerProfile(learner_id="l1", goal_id="goal-data-analyst")
    path = _path_for(catalog, profile)
    first = path.milestones[0].items[0].item_id
    progress = {first: _progress(first, "completed", NOW - timedelta(days=4))}

    assert insights.build_pace(catalog, profile, path, progress, now=NOW).status == "unknown"


def test_a_slow_learner_is_reported_as_behind_with_a_suggestion(catalog):
    profile = LearnerProfile(
        learner_id="l1", goal_id="goal-data-analyst", weekly_hours=20
    )
    path = _path_for(catalog, profile)
    first = path.milestones[0].items[0]
    # One item finished over ten weeks: far under the 20h/week that was planned.
    progress = {
        first.item_id: _progress(first.item_id, "completed", NOW - timedelta(days=70))
    }

    report = insights.build_pace(catalog, profile, path, progress, now=NOW)
    assert report.status == "behind"
    assert report.observed_weekly_hours < report.planned_weekly_hours
    assert report.suggested_weekly_hours == round(report.observed_weekly_hours)
    assert report.projected_weeks_remaining > report.planned_weeks_remaining
    assert str(report.suggested_weekly_hours) in report.message


def test_a_fast_learner_is_reported_as_ahead(catalog):
    profile = LearnerProfile(
        learner_id="l1", goal_id="goal-data-analyst", weekly_hours=2
    )
    path = _path_for(catalog, profile)
    items = [i for m in path.milestones for i in m.items][:3]
    progress = {
        item.item_id: _progress(item.item_id, "completed", NOW - timedelta(days=14))
        for item in items
    }

    report = insights.build_pace(catalog, profile, path, progress, now=NOW)
    assert report.status == "ahead"
    assert report.observed_weekly_hours > report.planned_weekly_hours
    assert report.projected_weeks_remaining < report.planned_weeks_remaining


def test_a_matching_pace_is_on_track_and_suggests_nothing(catalog):
    profile = LearnerProfile(
        learner_id="l1", goal_id="goal-data-analyst", weekly_hours=8
    )
    path = _path_for(catalog, profile)
    first = path.milestones[0].items[0]
    weeks = first.hours / 8
    progress = {
        first.item_id: _progress(
            first.item_id, "completed", NOW - timedelta(days=round(weeks * 7))
        )
    }

    report = insights.build_pace(catalog, profile, path, progress, now=NOW)
    assert report.status == "on_track"
    assert report.suggested_weekly_hours is None


def test_pace_appears_on_the_dashboard_and_its_own_endpoint(client):
    learner_id = _with_goal(client)
    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["pace"]["status"] == "unknown"

    direct = client.get(f"/api/learners/{learner_id}/pace").json()
    assert direct["planned_weekly_hours"] == dash["pace"]["planned_weekly_hours"]


def test_replan_does_nothing_without_evidence(client):
    learner_id = _with_goal(client)
    before = client.get(f"/api/learners/{learner_id}/path").json()

    result = client.post(f"/api/learners/{learner_id}/replan").json()
    assert result["changed"] is False
    assert result["path_revision"] == before["revision"]
    assert client.get(f"/api/learners/{learner_id}/profile").json()["weekly_hours"] == (
        before["weekly_hours"]
    )


def test_replan_refits_the_schedule_to_the_observed_rate(client, db, catalog):
    learner_id = _with_goal(client)
    path = client.get(f"/api/learners/{learner_id}/path").json()
    first = path["milestones"][0]["items"][0]

    # Backdate a completion so the learner has a measurable, slow history.
    client.post(
        f"/api/learners/{learner_id}/progress",
        json={"item_id": first["item_id"], "status": "completed"},
    )
    long_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    db._write(  # noqa: SLF001 - the test is about behaviour over time
        "UPDATE progress SET updated_at = ? WHERE learner_id = ?",
        (long_ago, learner_id),
    )

    pace = client.get(f"/api/learners/{learner_id}/pace").json()
    assert pace["status"] == "behind"

    result = client.post(f"/api/learners/{learner_id}/replan").json()
    assert result["changed"] is True
    assert result["weekly_hours"] == pace["suggested_weekly_hours"]
    assert result["path_revision"] > path["revision"]

    rebuilt = client.get(f"/api/learners/{learner_id}/path").json()
    assert rebuilt["weekly_hours"] == result["weekly_hours"]
    assert rebuilt["total_weeks"] > path["total_weeks"]
    assert any("re-planned" in note.lower() for note in rebuilt["adaptation_notes"])


# ------------------------------------------------------------------ resources
def test_resources_carry_real_links(catalog):
    resources = catalog.items_of_type("resource")
    assert resources, "the catalogue should ship reference material"
    for item in resources:
        assert item.url.startswith("https://")
        assert item.teaches, f"{item.id} supports no skill"


def test_resources_are_never_scheduled_into_a_path(catalog, make_profile):
    """Reference material supports a skill; it never satisfies coverage."""
    for goal_id in catalog.goals:
        _, derived = make_profile(goal_id=goal_id)
        path = generate_path(catalog=catalog, derived=derived, goal_id=goal_id)
        scheduled = {i.item_id for m in path.milestones for i in m.items}
        assert not scheduled & {r.id for r in catalog.items_of_type("resource")}


def test_resources_are_recommendable_and_filterable(client):
    learner_id = _with_goal(client)
    recs = client.get(
        f"/api/learners/{learner_id}/recommendations?type=resource&limit=5"
    ).json()
    assert recs
    for rec in recs:
        assert rec["type"] == "resource"
        assert rec["url"].startswith("https://")
        assert any("Reference material" in reason for reason in rec["reasons"])


def test_catalog_endpoint_exposes_urls(client):
    items = client.get("/api/catalog/items?type=resource").json()
    assert items and all(i["url"].startswith("https://") for i in items)


def test_every_skill_is_still_covered_by_a_course_not_a_resource(catalog):
    """Resources must not be able to satisfy the teachability invariant."""
    taught_by_courses = {
        s for item in catalog.items.values() if item.is_course for s in item.teaches
    }
    assert set(catalog.skills) <= taught_by_courses


# --------------------------------------------------------------- clarification
@pytest.mark.parametrize("message", [
    "I want to work with data",
    "help me get a tech job",
    "I want to build things",
    "hello",
])
def test_vague_goals_are_asked_about_not_guessed(client, message):
    learner_id = _new_learner(client)
    body = client.post(
        "/api/chat", json={"learner_id": learner_id, "message": message}
    ).json()

    assert body["needs_clarification"] is True
    assert body["path_generated"] is False
    assert len(body["suggested_replies"]) >= 2
    assert client.get(f"/api/learners/{learner_id}/profile").json()["goal_id"] is None


@pytest.mark.parametrize("message", [
    "I want to be a data analyst",
    "I want to become an AI engineer building LLM apps with retrieval",
    "I want to be a frontend engineer",
    "I want to be a machine learning engineer",
    "I want to be a devops engineer",
])
def test_clear_goals_are_never_interrupted_by_a_question(client, message):
    learner_id = _new_learner(client)
    body = client.post(
        "/api/chat", json={"learner_id": learner_id, "message": message}
    ).json()

    assert body["needs_clarification"] is False
    assert body["path_generated"] is True


def test_a_suggested_reply_resolves_the_ambiguity(client):
    """The options offered must be phrased so that sending one back works."""
    learner_id = _new_learner(client)
    first = client.post(
        "/api/chat", json={"learner_id": learner_id, "message": "I want to work with data"}
    ).json()

    answer = client.post("/api/chat", json={
        "learner_id": learner_id, "message": first["suggested_replies"][0],
    }).json()
    assert answer["needs_clarification"] is False
    assert answer["path_generated"] is True
    assert answer["profile"]["goal_id"] is not None


def test_follow_up_questions_are_never_treated_as_ambiguous_goals(client):
    """Once a learner has a path, questions are answered, not interrogated."""
    learner_id = _with_goal(client)
    body = client.post("/api/chat", json={
        "learner_id": learner_id, "message": "what should I do first?",
    }).json()
    assert body["needs_clarification"] is False


def test_clarification_offers_a_spread_of_domains(catalog):
    result = assistant.clarification(catalog, "hello")
    assert result is not None
    _, replies = result
    goals = [g for g in catalog.goals.values() if any(g.title in r for r in replies)]
    assert len({g.domain for g in goals}) >= 2


def test_clarification_returns_none_for_an_unambiguous_goal(catalog):
    assert assistant.clarification(catalog, "I want to be a data analyst") is None
