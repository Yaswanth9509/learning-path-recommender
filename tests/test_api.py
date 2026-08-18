"""End-to-end API tests, including the full learner journey."""

from __future__ import annotations


def _new_learner(client) -> str:
    response = client.post("/api/learners", json={})
    assert response.status_code == 201
    return response.json()["learner_id"]


# ------------------------------------------------------------------- system
def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["llm_enabled"] is False           # disabled in tests
    assert body["assistant_mode"] == "rule-engine"
    assert body["catalog"]["skills"] > 0


def test_catalog_endpoints(client):
    goals = client.get("/api/catalog/goals").json()
    skills = client.get("/api/catalog/skills").json()
    items = client.get("/api/catalog/items").json()
    assert goals and skills and items
    assert all("target_skill_names" in g for g in goals)

    courses = client.get("/api/catalog/items?type=course").json()
    assert courses and all(i["type"] == "course" for i in courses)


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Learning Path Recommender" in response.text


# ----------------------------------------------------------------- learners
def test_create_and_fetch_learner(client):
    learner_id = _new_learner(client)
    profile = client.get(f"/api/learners/{learner_id}/profile").json()
    assert profile["learner_id"] == learner_id
    assert profile["goal_id"] is None


def test_unknown_learner_is_404(client):
    assert client.get("/api/learners/nope/profile").status_code == 404


def test_profile_update_validation(client):
    learner_id = _new_learner(client)
    bad_goal = client.patch(
        f"/api/learners/{learner_id}/profile", json={"goal_id": "goal-nope"}
    )
    assert bad_goal.status_code == 400

    bad_skill = client.patch(
        f"/api/learners/{learner_id}/profile", json={"declared_skills": ["ghost"]}
    )
    assert bad_skill.status_code == 400

    bad_format = client.patch(
        f"/api/learners/{learner_id}/profile", json={"preferred_formats": ["hologram"]}
    )
    assert bad_format.status_code == 400


def test_path_requires_a_goal(client):
    learner_id = _new_learner(client)
    assert client.get(f"/api/learners/{learner_id}/path").status_code == 400


def test_delete_learner(client):
    learner_id = _new_learner(client)
    assert client.delete(f"/api/learners/{learner_id}").status_code == 204
    assert client.get(f"/api/learners/{learner_id}/profile").status_code == 404


# --------------------------------------------------------------------- chat
def test_chat_creates_goal_and_path(client):
    learner_id = _new_learner(client)
    response = client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "I want to become a data analyst. I already know SQL. "
                   "I'm a beginner with 6 hours a week.",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["path_generated"] is True
    assert body["source"] == "rules"
    assert body["profile"]["goal_id"] == "goal-data-analyst"
    assert body["profile"]["weekly_hours"] == 6
    assert body["profile"]["experience_level"] == "beginner"
    assert "sql" in body["profile"]["declared_skills"]
    assert body["interpretation"]["goal_id"] == "goal-data-analyst"

    path = client.get(f"/api/learners/{learner_id}/path").json()
    assert path["milestones"]
    assert "sql" not in path["gap"]["missing_skills"]


def test_chat_answers_follow_up_without_regenerating(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a frontend engineer",
    })
    first = client.get(f"/api/learners/{learner_id}/path").json()

    answer = client.post("/api/chat", json={
        "learner_id": learner_id, "message": "how long will this take?",
    }).json()
    assert answer["path_generated"] is False
    assert "week" in answer["reply"].lower()

    second = client.get(f"/api/learners/{learner_id}/path").json()
    assert first["revision"] == second["revision"]


def test_chat_rejects_empty_message(client):
    learner_id = _new_learner(client)
    response = client.post("/api/chat", json={"learner_id": learner_id, "message": "  "})
    assert response.status_code == 400


def test_conversation_is_persisted(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a devops engineer",
    })
    history = client.get(f"/api/learners/{learner_id}/conversation").json()
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"

    assert client.delete(f"/api/learners/{learner_id}/conversation").status_code == 204
    assert client.get(f"/api/learners/{learner_id}/conversation").json() == []


def test_chat_can_switch_goals(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a frontend engineer",
    })
    switched = client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "Actually I want to switch to cloud architecture instead",
    }).json()
    assert switched["path_generated"] is True
    assert switched["profile"]["goal_id"] == "goal-cloud-architect"


# ------------------------------------------------------- recommendations
def test_recommendations_are_explained(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a machine learning engineer",
    })
    recs = client.get(f"/api/learners/{learner_id}/recommendations?limit=8").json()
    assert len(recs) == 8
    for rec in recs:
        assert rec["reasons"]
        assert 0 <= rec["score"] <= 1
        assert set(rec["breakdown"]) == {
            "goal_relevance", "skill_readiness", "level_fit",
            "interest_match", "format_fit", "quality",
        }
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_recommendations_reject_bad_type(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a data analyst",
    })
    response = client.get(f"/api/learners/{learner_id}/recommendations?type=nonsense")
    assert response.status_code == 400


def test_explain_endpoint_returns_weights_and_placement(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a data analyst",
    })
    path = client.get(f"/api/learners/{learner_id}/path").json()
    item_id = path["milestones"][0]["items"][0]["item_id"]

    body = client.get(f"/api/learners/{learner_id}/explain/{item_id}").json()
    assert body["recommendation"]["item_id"] == item_id
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-9
    assert body["placement"]["order"] == 1

    missing = client.get(f"/api/learners/{learner_id}/explain/nope")
    assert missing.status_code == 404


def test_gap_endpoint_reports_and_explains_the_gap(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "I want to be a data analyst. I already know SQL.",
    })
    body = client.get(f"/api/learners/{learner_id}/gap").json()

    report = body["report"]
    assert "sql" in report["known_skills"]
    assert "sql" not in report["missing_skills"]
    assert set(report["target_skills"]) <= set(report["known_skills"]) | set(
        report["missing_skills"]
    )
    assert 0 <= report["coverage_pct"] <= 100
    assert str(len(report["missing_skills"])) in body["explanation"]


def test_gap_requires_a_goal(client):
    learner_id = _new_learner(client)
    assert client.get(f"/api/learners/{learner_id}/gap").status_code == 400


# ----------------------------------------------------------------- progress
def test_progress_updates_do_not_reshuffle_the_path(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a frontend engineer",
    })
    before = client.get(f"/api/learners/{learner_id}/path").json()
    item_id = before["milestones"][0]["items"][0]["item_id"]

    response = client.post(f"/api/learners/{learner_id}/progress",
                           json={"item_id": item_id, "status": "completed"})
    assert response.status_code == 200
    assert item_id in response.json()["completed_items"]

    after = client.get(f"/api/learners/{learner_id}/path").json()
    assert after["revision"] == before["revision"]
    order_before = [i["item_id"] for m in before["milestones"] for i in m["items"]]
    order_after = [i["item_id"] for m in after["milestones"] for i in m["items"]]
    assert order_before == order_after
    assert after["milestones"][0]["items"][0]["status"] == "completed"


def test_progress_rejects_unknown_item(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a data analyst",
    })
    response = client.post(f"/api/learners/{learner_id}/progress",
                           json={"item_id": "ghost", "status": "completed"})
    assert response.status_code == 404


# ----------------------------------------------------------------- feedback
def test_not_relevant_feedback_removes_the_item_and_rebuilds(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a machine learning engineer",
    })
    before = client.get(f"/api/learners/{learner_id}/path").json()
    victim = before["milestones"][0]["items"][0]["item_id"]

    result = client.post(f"/api/learners/{learner_id}/feedback",
                         json={"item_id": victim, "signal": "not_relevant"}).json()
    assert victim in result["excluded_items"]
    assert result["path_revision"] == before["revision"] + 1

    after = client.get(f"/api/learners/{learner_id}/path").json()
    assert victim not in [i["item_id"] for m in after["milestones"] for i in m["items"]]


def test_too_easy_feedback_shrinks_the_path(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a machine learning engineer",
    })
    before = client.get(f"/api/learners/{learner_id}/path").json()
    target = before["milestones"][0]["items"][0]["item_id"]

    client.post(f"/api/learners/{learner_id}/feedback",
                json={"item_id": target, "signal": "too_easy"})
    after = client.get(f"/api/learners/{learner_id}/path").json()

    # Skills marked as mastered leave the gap, so there is less work left.
    assert len(after["gap"]["missing_skills"]) < len(before["gap"]["missing_skills"])
    profile = client.get(f"/api/learners/{learner_id}/profile").json()
    assert profile["difficulty_offset"] == 1


def test_feedback_is_listed(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a data analyst",
    })
    client.post(f"/api/learners/{learner_id}/feedback",
                json={"item_id": "c-viz", "signal": "liked", "note": "great charts"})
    entries = client.get(f"/api/learners/{learner_id}/feedback").json()
    assert len(entries) == 1
    assert entries[0]["signal"] == "liked"
    assert entries[0]["note"] == "great charts"


def test_feedback_rejects_unknown_item(client):
    learner_id = _new_learner(client)
    response = client.post(f"/api/learners/{learner_id}/feedback",
                           json={"item_id": "ghost", "signal": "liked"})
    assert response.status_code == 404


# ---------------------------------------------------------------- dashboard
def test_dashboard_tracks_completion(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a frontend engineer",
    })
    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["overall_percent"] == 0.0
    assert dash["items_total"] > 0
    assert dash["next_actions"]
    assert dash["milestones"][0]["status"] in ("available", "in_progress")
    assert dash["skills_total"] > 0

    path = client.get(f"/api/learners/{learner_id}/path").json()
    first_item = path["milestones"][0]["items"][0]["item_id"]
    client.post(f"/api/learners/{learner_id}/progress",
                json={"item_id": first_item, "status": "completed"})

    dash2 = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash2["items_completed"] == 1
    assert dash2["overall_percent"] > 0
    assert dash2["hours_remaining"] < dash["hours_remaining"]
    assert dash2["skills_mastered"] >= dash["skills_mastered"]


def test_completed_work_survives_a_path_rebuild(client):
    """Finishing a course drops it from the plan — but not from your progress."""
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a machine learning engineer",
    })
    path = client.get(f"/api/learners/{learner_id}/path").json()
    first = path["milestones"][0]["items"][0]["item_id"]

    client.post(f"/api/learners/{learner_id}/progress",
                json={"item_id": first, "status": "completed"})
    before = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert before["items_completed"] == 1

    # Force a rebuild; the finished course leaves the path because its skill is
    # no longer a gap.
    client.post(f"/api/learners/{learner_id}/feedback",
                json={"item_id": path["milestones"][-1]["items"][0]["item_id"],
                      "signal": "not_relevant"})
    rebuilt = client.get(f"/api/learners/{learner_id}/path").json()
    assert first not in [i["item_id"] for m in rebuilt["milestones"] for i in m["items"]]

    after = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert after["items_completed"] == 1, "completed work must still be counted"
    assert after["overall_percent"] > 0
    assert after["hours_completed"] >= before["hours_completed"]


def test_dashboard_domain_breakdown_totals_match(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a cloud architect",
    })
    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    counted = sum(
        sum(states.values()) for states in dash["domain_breakdown"].values()
    )
    assert counted == dash["skills_total"] == len(dash["skill_progress"])


def test_next_actions_prioritise_started_work(client):
    learner_id = _new_learner(client)
    client.post("/api/chat", json={
        "learner_id": learner_id, "message": "I want to be a devops engineer",
    })
    path = client.get(f"/api/learners/{learner_id}/path").json()
    items = [i["item_id"] for m in path["milestones"] for i in m["items"]]
    client.post(f"/api/learners/{learner_id}/progress",
                json={"item_id": items[2], "status": "in_progress"})

    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["next_actions"][0]["item_id"] == items[2]
    assert "in progress" in dash["next_actions"][0]["why"].lower()


# ------------------------------------------------------------- full journey
def test_complete_learner_journey(client):
    """Chat -> path -> progress -> feedback -> dashboard, all consistent."""
    learner_id = _new_learner(client)

    chat = client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "I'm a beginner and want to become an AI engineer building "
                   "LLM apps. I know Python. I have 10 hours a week.",
    }).json()
    assert chat["path_generated"]
    assert chat["profile"]["goal_id"] == "goal-ai-engineer"

    path = client.get(f"/api/learners/{learner_id}/path").json()
    assert path["total_hours"] > 0
    assert path["weekly_hours"] == 10

    # Every item's prerequisites are satisfied by the time it appears.
    from backend.catalog import get_catalog

    cat = get_catalog()
    held = set(cat.prerequisite_closure(chat["profile"]["declared_skills"]))
    for milestone in path["milestones"]:
        for item in milestone["items"]:
            catalog_item = cat.items[item["item_id"]]
            assert all(s in held for s in catalog_item.requires), item["item_id"]
            held.update(catalog_item.teaches)

    # Complete the first stage.
    for item in path["milestones"][0]["items"]:
        client.post(f"/api/learners/{learner_id}/progress",
                    json={"item_id": item["item_id"], "status": "completed"})

    dash = client.get(f"/api/learners/{learner_id}/dashboard").json()
    assert dash["milestones"][0]["status"] == "completed"
    assert dash["overall_percent"] > 0

    # Reject something and confirm the plan adapts around it.
    later = path["milestones"][-1]["items"][0]["item_id"]
    client.post(f"/api/learners/{learner_id}/feedback",
                json={"item_id": later, "signal": "not_relevant"})
    rebuilt = client.get(f"/api/learners/{learner_id}/path").json()
    assert later not in [i["item_id"] for m in rebuilt["milestones"] for i in m["items"]]

    # Completed work is still recorded after the rebuild.
    progress = client.get(f"/api/learners/{learner_id}/progress").json()
    assert len(progress) == len(path["milestones"][0]["items"])
