"""Path generation invariants.

The headline guarantee — an item is never scheduled before the things it depends
on — is asserted across every goal and several learner shapes, not just one
happy path.
"""

from __future__ import annotations

import itertools

import pytest

from backend.pathgen import generate_path


def _walk(path):
    """Yield (milestone, item) in the exact order a learner would work."""
    for milestone in path.milestones:
        for item in milestone.items:
            yield milestone, item


def assert_prerequisites_respected(catalog, derived, path):
    """The core correctness property of the whole system."""
    held = set(derived.known_skills)
    for milestone, entry in _walk(path):
        item = catalog.items[entry.item_id]
        unmet = [s for s in item.requires if s not in held]
        assert not unmet, (
            f"{item.id} in {milestone.title} needs "
            f"{[catalog.skill_name(s) for s in unmet]} which is not yet covered"
        )
        held.update(item.teaches)


ALL_GOALS = [
    "goal-data-analyst", "goal-data-scientist", "goal-ml-engineer",
    "goal-ai-engineer", "goal-frontend", "goal-fullstack",
    "goal-devops", "goal-cloud-architect", "goal-sre",
]

LEVELS = ["beginner", "intermediate", "advanced"]


@pytest.mark.parametrize("goal_id", ALL_GOALS)
def test_path_respects_prerequisites_for_every_goal(catalog, make_profile, goal_id):
    _, derived = make_profile(goal_id=goal_id, experience_level="beginner")
    path = generate_path(catalog, derived, goal_id)
    assert_prerequisites_respected(catalog, derived, path)


@pytest.mark.parametrize(
    "goal_id,level", list(itertools.product(ALL_GOALS[:4], LEVELS))
)
def test_path_respects_prerequisites_across_levels(
    catalog, make_profile, goal_id, level
):
    _, derived = make_profile(goal_id=goal_id, experience_level=level, weekly_hours=12)
    path = generate_path(catalog, derived, goal_id)
    assert_prerequisites_respected(catalog, derived, path)


@pytest.mark.parametrize("goal_id", ALL_GOALS)
def test_path_covers_the_entire_gap(catalog, make_profile, goal_id):
    """Every missing skill is taught by some item on the path."""
    _, derived = make_profile(goal_id=goal_id)
    path = generate_path(catalog, derived, goal_id)
    taught = {s for _, item in _walk(path) for s in item.teaches}
    uncovered = set(path.gap.missing_skills) - taught
    assert not uncovered, f"gap skills never taught: {sorted(uncovered)}"
    assert not path.uncovered_skills


@pytest.mark.parametrize("goal_id", ALL_GOALS)
def test_path_has_projects_and_assessments(catalog, make_profile, goal_id):
    _, derived = make_profile(goal_id=goal_id)
    path = generate_path(catalog, derived, goal_id)
    roles = {item.role for _, item in _walk(path)}
    assert "core" in roles
    assert "project" in roles or "assessment" in roles, (
        "a path should include applied or verifying work"
    )


def test_no_duplicate_items_in_a_path(catalog, make_profile):
    for goal_id in ALL_GOALS:
        _, derived = make_profile(goal_id=goal_id)
        path = generate_path(catalog, derived, goal_id)
        ids = [item.item_id for _, item in _walk(path)]
        assert len(ids) == len(set(ids)), f"duplicate items in {goal_id}: {ids}"


def test_known_skills_are_not_retaught(catalog, make_profile):
    """A learner is never sent to a course for something they already hold."""
    known = ["python", "prog-fundamentals", "math-stats", "data-wrangling"]
    _, derived = make_profile(goal_id="goal-data-scientist", declared_skills=known)
    path = generate_path(catalog, derived, "goal-data-scientist")
    for _, item in _walk(path):
        catalog_item = catalog.items[item.item_id]
        if catalog_item.type != "course":
            continue
        new = set(catalog_item.teaches) - set(derived.known_skills)
        assert new, f"{item.item_id} teaches nothing the learner lacks"


def test_implied_prerequisites_are_not_retaught(catalog, make_profile):
    """Declaring an advanced skill implies its prerequisites are held."""
    _, derived = make_profile(
        goal_id="goal-ml-engineer", declared_skills=["deep-learning"]
    )
    assert "supervised-ml" in derived.implied_skills
    assert "python" in derived.implied_skills
    path = generate_path(catalog, derived, "goal-ml-engineer")
    assert "supervised-ml" not in path.gap.missing_skills
    assert "python" not in path.gap.missing_skills


def test_weekly_hours_drive_the_schedule(catalog, make_profile):
    _, slow = make_profile(goal_id="goal-frontend", weekly_hours=4)
    _, fast = make_profile(goal_id="goal-frontend", weekly_hours=20)
    slow_path = generate_path(catalog, slow, "goal-frontend")
    fast_path = generate_path(catalog, fast, "goal-frontend")
    assert slow_path.total_hours == fast_path.total_hours
    assert slow_path.total_weeks > fast_path.total_weeks


def test_excluded_items_never_appear(catalog, make_profile):
    _, base = make_profile(goal_id="goal-frontend")
    first = generate_path(catalog, base, "goal-frontend")
    victim = first.milestones[0].items[0].item_id

    _, derived = make_profile(goal_id="goal-frontend", excluded_item_ids=[victim])
    second = generate_path(catalog, derived, "goal-frontend")
    assert victim not in [item.item_id for _, item in _walk(second)]
    # The skill it taught is still covered by an alternative, or reported.
    assert_prerequisites_respected(catalog, derived, second)


def test_no_gap_produces_a_proof_stage(catalog, make_profile):
    goal = catalog.goal("goal-data-analyst")
    everything = list(catalog.prerequisite_closure(goal.target_skills))
    _, derived = make_profile(goal_id="goal-data-analyst", declared_skills=everything)
    path = generate_path(catalog, derived, "goal-data-analyst")
    assert path.gap.missing_skills == []
    assert path.milestones, "a learner with no gap still needs a way to prove it"
    roles = {item.role for _, item in _walk(path)}
    assert roles <= {"project", "assessment"}


def test_generation_is_deterministic(catalog, make_profile):
    _, derived = make_profile(goal_id="goal-ai-engineer", weekly_hours=9)
    first = generate_path(catalog, derived, "goal-ai-engineer")
    second = generate_path(catalog, derived, "goal-ai-engineer")
    assert [i.item_id for _, i in _walk(first)] == [i.item_id for _, i in _walk(second)]
    assert first.total_hours == second.total_hours


def test_milestone_hours_sum_to_total(catalog, make_profile):
    _, derived = make_profile(goal_id="goal-cloud-architect")
    path = generate_path(catalog, derived, "goal-cloud-architect")
    assert sum(m.hours for m in path.milestones) == path.total_hours
    for milestone in path.milestones:
        assert milestone.hours == sum(i.hours for i in milestone.items)


def test_every_item_has_a_rationale(catalog, make_profile):
    for goal_id in ALL_GOALS:
        _, derived = make_profile(goal_id=goal_id)
        path = generate_path(catalog, derived, goal_id)
        for _, item in _walk(path):
            assert item.rationale.strip(), f"{item.item_id} has no explanation"
