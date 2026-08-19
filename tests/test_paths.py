"""Several roadmaps at once, and shaping one by hand."""

from __future__ import annotations

import pytest

from backend.models import MAX_ACTIVE_PATHS


def _learner(client) -> str:
    return client.post("/api/learners", json={}).json()["learner_id"]


def _with_goal(client, goal_id: str) -> str:
    learner_id = _learner(client)
    client.patch(f"/api/learners/{learner_id}/profile", json={"goal_id": goal_id})
    return learner_id


def _add(client, learner_id: str, goal_id: str):
    return client.post(f"/api/learners/{learner_id}/paths", json={"goal_id": goal_id})


# ------------------------------------------------------------- several paths
def test_a_learner_can_run_three_paths(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    assert _add(client, learner_id, "goal-frontend").status_code == 201
    assert _add(client, learner_id, "goal-devops").status_code == 201

    paths = client.get(f"/api/learners/{learner_id}/paths").json()
    assert len(paths) == MAX_ACTIVE_PATHS
    assert {p["goal_id"] for p in paths} == {
        "goal-data-analyst", "goal-frontend", "goal-devops"
    }
    assert sum(p["is_active"] for p in paths) == 1
    assert all(p["items_total"] > 0 and p["hours_total"] > 0 for p in paths)


def test_a_fourth_path_is_refused_with_a_reason(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-frontend")
    _add(client, learner_id, "goal-devops")

    refused = _add(client, learner_id, "goal-cloud-architect")
    assert refused.status_code == 400
    assert str(MAX_ACTIVE_PATHS) in refused.json()["detail"]


def test_each_path_keeps_its_own_roadmap(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-frontend")

    frontend = client.get(f"/api/learners/{learner_id}/path").json()
    assert frontend["goal_id"] == "goal-frontend"

    client.post(f"/api/learners/{learner_id}/paths/goal-data-analyst/activate")
    analyst = client.get(f"/api/learners/{learner_id}/path").json()
    assert analyst["goal_id"] == "goal-data-analyst"
    assert analyst["milestones"] != frontend["milestones"]


def test_switching_paths_does_not_regenerate_them(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-frontend")
    before = client.get(f"/api/learners/{learner_id}/path").json()

    client.post(f"/api/learners/{learner_id}/paths/goal-data-analyst/activate")
    client.post(f"/api/learners/{learner_id}/paths/goal-frontend/activate")
    after = client.get(f"/api/learners/{learner_id}/path").json()
    assert after["revision"] == before["revision"]
    assert after["generated_at"] == before["generated_at"]


def test_progress_counts_towards_every_path_that_contains_the_item(client):
    """Finishing a course you did once should not need doing twice."""
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-ml-engineer")

    analyst = client.get(f"/api/learners/{learner_id}/paths").json()
    shared = "c-prog-101"  # foundational, appears on both roadmaps
    client.post(f"/api/learners/{learner_id}/progress",
                json={"item_id": shared, "status": "completed"})

    after = client.get(f"/api/learners/{learner_id}/paths").json()
    assert sum(p["items_completed"] for p in after) >= 1
    assert after != analyst


def test_deleting_a_path_keeps_the_others_and_the_progress(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-frontend")
    path = client.get(f"/api/learners/{learner_id}/path").json()
    item = path["milestones"][0]["items"][0]["item_id"]
    client.post(f"/api/learners/{learner_id}/progress",
                json={"item_id": item, "status": "completed"})

    assert client.delete(
        f"/api/learners/{learner_id}/paths/goal-frontend"
    ).status_code == 204

    remaining = client.get(f"/api/learners/{learner_id}/paths").json()
    assert [p["goal_id"] for p in remaining] == ["goal-data-analyst"]
    assert remaining[0]["is_active"] is True
    assert item in client.get(f"/api/learners/{learner_id}/progress").json()


def test_deleting_an_unknown_path_is_404(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    assert client.delete(
        f"/api/learners/{learner_id}/paths/goal-frontend"
    ).status_code == 404


def test_adding_an_unknown_goal_is_400(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    assert _add(client, learner_id, "goal-nope").status_code == 400


def test_the_dashboard_reports_every_path(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    _add(client, learner_id, "goal-frontend")

    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["paths_total"] == 2
    assert dash["paths_completed"] == 0
    assert len(dash["paths"]) == 2
    active = [p for p in dash["paths"] if p["is_active"]]
    assert len(active) == 1 and active[0]["goal_id"] == "goal-frontend"
    assert all("percent" in p and "stages" in p for p in dash["paths"])


def test_a_finished_path_is_counted_as_completed(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    path = client.get(f"/api/learners/{learner_id}/path").json()
    for milestone in path["milestones"]:
        for item in milestone["items"]:
            client.post(f"/api/learners/{learner_id}/progress",
                        json={"item_id": item["item_id"], "status": "completed"})

    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["paths_completed"] == 1
    assert dash["paths"][0]["completed"] is True
    assert dash["paths"][0]["percent"] == 100.0


# --------------------------------------------------- shaping a path by hand
def test_a_learner_can_add_a_course_to_their_path(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    before = client.get(f"/api/learners/{learner_id}/path").json()
    scheduled = {i["item_id"] for m in before["milestones"] for i in m["items"]}
    extra = "c-js"
    assert extra not in scheduled

    response = client.post(f"/api/learners/{learner_id}/path/items",
                           json={"item_id": extra})
    assert response.status_code == 200
    after = response.json()
    items = [i for m in after["milestones"] for i in m["items"]]
    added = [i for i in items if i["item_id"] == extra]
    assert added, "the pinned course is not on the path"
    assert "added this yourself" in added[0]["rationale"].lower()
    assert after["total_hours"] > before["total_hours"]


def test_an_added_course_still_respects_prerequisites(client, catalog):
    learner_id = _with_goal(client, "goal-data-analyst")
    client.post(f"/api/learners/{learner_id}/path/items", json={"item_id": "c-rag"})
    path = client.get(f"/api/learners/{learner_id}/path").json()

    held = set(path["gap"]["known_skills"])
    for milestone in path["milestones"]:
        for entry in milestone["items"]:
            item = catalog.items[entry["item_id"]]
            if entry["item_id"] == "c-rag":
                unmet = [s for s in item.requires if s not in held]
                # Either every prerequisite is met, or the item is flagged as
                # optional at the end — never silently mis-ordered.
                assert not unmet or "optional" in entry["rationale"].lower()
            held.update(item.teaches)


def test_a_learner_can_remove_an_item_from_their_path(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    before = client.get(f"/api/learners/{learner_id}/path").json()
    victim = before["milestones"][0]["items"][0]["item_id"]

    after = client.delete(
        f"/api/learners/{learner_id}/path/items/{victim}"
    ).json()
    remaining = {i["item_id"] for m in after["milestones"] for i in m["items"]}
    assert victim not in remaining
    assert any("removed" in note.lower() for note in after["adaptation_notes"])


def test_removing_then_adding_the_same_item_restores_it(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    path = client.get(f"/api/learners/{learner_id}/path").json()
    item = path["milestones"][0]["items"][0]["item_id"]

    client.delete(f"/api/learners/{learner_id}/path/items/{item}")
    restored = client.post(f"/api/learners/{learner_id}/path/items",
                           json={"item_id": item}).json()
    assert item in {i["item_id"] for m in restored["milestones"] for i in m["items"]}


def test_adding_the_same_item_twice_is_refused(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    client.post(f"/api/learners/{learner_id}/path/items", json={"item_id": "c-js"})
    again = client.post(f"/api/learners/{learner_id}/path/items",
                        json={"item_id": "c-js"})
    assert again.status_code == 400


def test_adding_an_unknown_item_is_404(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    assert client.post(f"/api/learners/{learner_id}/path/items",
                       json={"item_id": "ghost"}).status_code == 404


# ---------------------------------------------------------- engine choice
def test_the_engine_defaults_to_the_provider(client):
    learner_id = _learner(client)
    assert client.get(
        f"/api/learners/{learner_id}/profile"
    ).json()["assistant_engine"] == "auto"


def test_a_learner_can_pin_the_rule_engine(client):
    learner_id = _learner(client)
    updated = client.patch(f"/api/learners/{learner_id}/profile",
                           json={"assistant_engine": "rules"}).json()
    assert updated["assistant_engine"] == "rules"

    body = client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "I want to be a data analyst, a beginner with 8 hours a week",
    }).json()
    assert body["source"] == "rules"
    assert body["path_generated"] is True


@pytest.mark.parametrize("value", ["gpt", "", "openai", "none"])
def test_an_unknown_engine_is_rejected(client, value):
    learner_id = _learner(client)
    assert client.patch(f"/api/learners/{learner_id}/profile",
                        json={"assistant_engine": value}).status_code == 422


def test_removing_the_only_course_for_a_skill_says_so(client):
    """The cascade is correct, but the learner must be told they caused it."""
    learner_id = _with_goal(client, "goal-data-analyst")
    before = client.get(f"/api/learners/{learner_id}/path").json()
    assert not before["uncovered_skills"]

    # c-prog-101 is the only course teaching Programming Fundamentals.
    after = client.delete(
        f"/api/learners/{learner_id}/path/items/c-prog-101"
    ).json()

    assert after["uncovered_skills"], "expected the cascade to strand skills"
    warning = " ".join(after["adaptation_notes"]).lower()
    assert "only course" in warning
    assert "add it back" in warning


def test_a_harmless_removal_carries_no_warning(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    path = client.get(f"/api/learners/{learner_id}/path").json()
    # An assessment teaches nothing, so dropping it strands no skill.
    victim = next(
        i["item_id"] for m in path["milestones"] for i in m["items"]
        if i["role"] == "assessment"
    )
    after = client.delete(f"/api/learners/{learner_id}/path/items/{victim}").json()
    assert not after["uncovered_skills"]
    assert not any("only course" in n.lower() for n in after["adaptation_notes"])


def test_the_cap_holds_in_storage_not_just_on_the_profile(client):
    """A fourth goal must remove a third path, not quietly add a fourth row."""
    learner_id = _with_goal(client, "goal-data-analyst")
    for goal in ("goal-frontend", "goal-devops"):
        _add(client, learner_id, goal)

    # Naming a fourth goal in chat is the route that used to leak rows.
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "actually i want to be a cloud architect",
    })

    paths = client.get(f"/api/learners/{learner_id}/paths").json()
    assert len(paths) == MAX_ACTIVE_PATHS, [p["goal_title"] for p in paths]
    goal_ids = {p["goal_id"] for p in paths}
    assert "goal-cloud-architect" in goal_ids

    # The profile and the stored paths agree about what exists.
    profile = client.get(f"/api/learners/{learner_id}/profile").json()
    assert set(profile["goal_ids"]) == goal_ids
    assert profile["goal_id"] == "goal-cloud-architect"


def test_making_room_is_explained_rather_than_silent(client):
    learner_id = _with_goal(client, "goal-data-analyst")
    for goal in ("goal-frontend", "goal-devops"):
        _add(client, learner_id, goal)

    # Adding explicitly is refused at the cap; naming a new goal in chat swaps
    # the active one — and says which roadmap it displaced.
    assert _add(client, learner_id, "goal-ml-engineer").status_code == 400

    client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "actually i want to be a machine learning engineer",
    })
    path = client.get(f"/api/learners/{learner_id}/path").json()
    notes = " ".join(path["adaptation_notes"]).lower()
    assert "made room" in notes
    assert "progress" in notes
