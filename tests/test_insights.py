"""Skill graph, weekly plan, achievements, and Markdown export."""

from __future__ import annotations

import pytest

from backend import insights
from backend.models import ProgressEntry
from backend.pathgen import generate_path

GOALS = ["goal-ai-engineer", "goal-frontend", "goal-devops", "goal-data-analyst"]


def _setup(catalog, make_profile, goal_id="goal-frontend", **kwargs):
    profile, derived = make_profile(goal_id=goal_id, **kwargs)
    path = generate_path(catalog, derived, goal_id)
    return profile, derived, path


# ---------------------------------------------------------------- skill graph
@pytest.mark.parametrize("goal_id", GOALS)
def test_graph_edges_only_connect_known_nodes(catalog, make_profile, goal_id):
    _, derived, path = _setup(catalog, make_profile, goal_id)
    graph = insights.build_graph(catalog, derived, path, {})
    ids = {n.id for n in graph.nodes}
    for edge in graph.edges:
        assert edge.source in ids
        assert edge.target in ids


@pytest.mark.parametrize("goal_id", GOALS)
def test_graph_edges_always_point_forward(catalog, make_profile, goal_id):
    """A prerequisite must sit in a strictly earlier layer than its dependent."""
    _, derived, path = _setup(catalog, make_profile, goal_id)
    graph = insights.build_graph(catalog, derived, path, {})
    layer_of = {n.id: n.layer for n in graph.nodes}
    for edge in graph.edges:
        assert layer_of[edge.source] < layer_of[edge.target], (
            f"{edge.source} (layer {layer_of[edge.source]}) must precede "
            f"{edge.target} (layer {layer_of[edge.target]})"
        )


def test_graph_marks_goal_targets(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    graph = insights.build_graph(catalog, derived, path, {})
    targets = {n.id for n in graph.nodes if n.is_target}
    assert targets == set(path.gap.target_skills)


def test_graph_reflects_known_and_completed_skills(catalog, make_profile):
    _, derived, path = _setup(
        catalog, make_profile, "goal-frontend", declared_skills=["html-css"]
    )
    graph = insights.build_graph(catalog, derived, path, {})
    by_id = {n.id: n for n in graph.nodes}
    assert by_id["html-css"].state == "mastered"

    first = path.milestones[0].items[0]
    progress = {first.item_id: ProgressEntry(item_id=first.item_id, status="completed")}
    graph2 = insights.build_graph(catalog, derived, path, progress)
    by_id2 = {n.id: n.state for n in graph2.nodes}

    # A course may teach skills outside this goal's scope (a Python course on a
    # frontend path teaches Python too); the graph deliberately shows only
    # goal-relevant skills, so assert over the intersection.
    in_scope = [s for s in first.teaches if s in by_id2]
    assert in_scope, "the first item should teach at least one in-scope skill"
    for skill_id in in_scope:
        assert by_id2[skill_id] == "mastered"


def test_graph_records_which_item_teaches_each_skill(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-devops")
    graph = insights.build_graph(catalog, derived, path, {})
    planned = [n for n in graph.nodes if n.state == "planned"]
    assert planned
    assert any(n.taught_by and n.milestone for n in planned)


def test_graph_slots_are_unique_within_a_layer(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-cloud-architect")
    graph = insights.build_graph(catalog, derived, path, {})
    seen: set[tuple[int, int]] = set()
    for node in graph.nodes:
        key = (node.layer, node.slot)
        assert key not in seen
        seen.add(key)
    assert graph.widest_layer >= 1
    assert graph.layer_count >= 1


# ---------------------------------------------------------------- weekly plan
def test_plan_never_exceeds_weekly_capacity(catalog, make_profile):
    for hours in (3, 8, 20):
        _, derived, path = _setup(
            catalog, make_profile, "goal-ml-engineer", weekly_hours=hours
        )
        plan = insights.build_weekly_plan(path, {})
        for week in plan.weeks:
            assert week.hours <= week.capacity + 0.01, week


def test_plan_schedules_every_remaining_hour(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend", weekly_hours=10)
    plan = insights.build_weekly_plan(path, {})
    scheduled = sum(i.hours_this_week for week in plan.weeks for i in week.items)
    assert scheduled == pytest.approx(path.total_hours, abs=0.5)


def test_plan_preserves_path_order(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-devops", weekly_hours=6)
    plan = insights.build_weekly_plan(path, {})

    expected = [i.item_id for m in path.milestones for i in m.items]
    seen: list[str] = []
    for week in plan.weeks:
        for item in week.items:
            if item.item_id not in seen:
                seen.append(item.item_id)
    assert seen == expected


def test_plan_splits_long_courses_across_weeks(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-ml-engineer", weekly_hours=5)
    plan = insights.build_weekly_plan(path, {})
    assert any(item.continues for week in plan.weeks for item in week.items), (
        "a 40-hour course at 5h/week must span weeks"
    )


def test_plan_skips_completed_work(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend", weekly_hours=10)
    first = path.milestones[0].items[0]
    progress = {first.item_id: ProgressEntry(item_id=first.item_id, status="completed")}

    full = insights.build_weekly_plan(path, {})
    partial = insights.build_weekly_plan(path, progress)
    ids = {i.item_id for w in partial.weeks for i in w.items}
    assert first.item_id not in ids
    assert partial.total_weeks <= full.total_weeks


def test_plan_weeks_are_numbered_consecutively(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-fullstack", weekly_hours=7)
    plan = insights.build_weekly_plan(path, {})
    assert [w.week for w in plan.weeks] == list(range(1, len(plan.weeks) + 1))
    assert all(w.focus for w in plan.weeks)


def test_plan_handles_a_fully_completed_path(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    progress = {
        i.item_id: ProgressEntry(item_id=i.item_id, status="completed")
        for m in path.milestones
        for i in m.items
    }
    plan = insights.build_weekly_plan(path, progress)
    assert plan.weeks == []
    assert plan.total_weeks == 0


# --------------------------------------------------------------- achievements
def test_no_progress_earns_nothing(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    ach = insights.build_achievements(catalog, path, {})
    assert ach.xp == 0
    assert ach.level == 1
    assert ach.earned_count == 0
    assert all(b.hint for b in ach.badges if not b.earned)


def test_first_completion_earns_the_first_badge(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    first = path.milestones[0].items[0]
    progress = {first.item_id: ProgressEntry(item_id=first.item_id, status="completed")}
    ach = insights.build_achievements(catalog, path, progress)

    assert ach.xp == first.hours * insights.XP_PER_HOUR
    earned = {b.id for b in ach.badges if b.earned}
    assert "first-step" in earned
    assert next(b for b in ach.badges if b.id == "first-step").hint == ""


def test_finishing_a_stage_earns_stage_clear(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    progress = {
        i.item_id: ProgressEntry(item_id=i.item_id, status="completed")
        for i in path.milestones[0].items
    }
    ach = insights.build_achievements(catalog, path, progress)
    assert "stage-clear" in {b.id for b in ach.badges if b.earned}


def test_completing_everything_earns_the_trophy(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    progress = {
        i.item_id: ProgressEntry(item_id=i.item_id, status="completed")
        for m in path.milestones
        for i in m.items
    }
    ach = insights.build_achievements(catalog, path, progress)
    earned = {b.id for b in ach.badges if b.earned}
    assert {"goal-reached", "halfway", "builder", "stage-clear"} <= earned
    assert ach.level > 1


def test_xp_levels_are_monotonic(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-ml-engineer")
    items = [i for m in path.milestones for i in m.items]
    progress: dict[str, ProgressEntry] = {}
    last_xp, last_level = -1, 0
    for item in items:
        progress[item.item_id] = ProgressEntry(
            item_id=item.item_id, status="completed"
        )
        ach = insights.build_achievements(catalog, path, progress)
        assert ach.xp > last_xp
        assert ach.level >= last_level
        assert 0 <= ach.xp_into_level
        last_xp, last_level = ach.xp, ach.level


# --------------------------------------------------------------------- export
def test_markdown_export_contains_the_whole_path(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-ai-engineer")
    ach = insights.build_achievements(catalog, path, {})
    markdown = insights.export_markdown(path, ach)

    assert markdown.startswith("# Learning Path — ")
    assert path.goal_title in markdown
    for milestone in path.milestones:
        assert milestone.title in markdown
        for item in milestone.items:
            assert item.title in markdown
    assert "| [ ] |" in markdown          # unchecked task boxes


def test_markdown_export_escapes_table_pipes(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    path.milestones[0].items[0].rationale = "a | b | c"
    ach = insights.build_achievements(catalog, path, {})
    markdown = insights.export_markdown(path, ach)
    assert "a \\| b \\| c" in markdown


def test_markdown_export_marks_completed_items(catalog, make_profile):
    _, derived, path = _setup(catalog, make_profile, "goal-frontend")
    path.milestones[0].items[0].status = "completed"
    ach = insights.build_achievements(catalog, path, {})
    assert "| [x] |" in insights.export_markdown(path, ach)


# ------------------------------------------------------------------- via API
def test_feature_endpoints(client):
    learner_id = client.post("/api/learners", json={}).json()["learner_id"]
    client.post("/api/chat", json={
        "learner_id": learner_id,
        "message": "I want to be an AI engineer, 10 hours a week",
    })

    graph = client.get(f"/api/learners/{learner_id}/graph").json()
    assert graph["nodes"] and graph["edges"]
    assert graph["layer_count"] >= 1

    plan = client.get(f"/api/learners/{learner_id}/plan").json()
    assert plan["weeks"]
    assert plan["weekly_hours"] == 10
    assert all(w["hours"] <= 10.01 for w in plan["weeks"])

    ach = client.get(f"/api/learners/{learner_id}/achievements").json()
    assert ach["xp"] == 0
    assert ach["total_count"] == len(ach["badges"])

    export = client.get(f"/api/learners/{learner_id}/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/markdown")
    assert "attachment" in export.headers["content-disposition"]
    assert export.text.startswith("# Learning Path")


def test_feature_endpoints_reject_unknown_learners(client):
    for suffix in ("graph", "plan", "achievements", "export"):
        assert client.get(f"/api/learners/ghost/{suffix}").status_code == 404
