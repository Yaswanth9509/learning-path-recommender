"""Catalog integrity. These tests are the guard against inconsistent data."""

from __future__ import annotations

import pytest

from backend.catalog import (
    Catalog,
    CatalogError,
    Goal,
    LearningItem,
    Skill,
    validate,
)


def test_catalog_loads_and_validates(catalog):
    assert len(catalog.skills) > 0
    assert len(catalog.items) > 0
    assert len(catalog.goals) > 0


def test_every_skill_prerequisite_resolves(catalog):
    for skill in catalog.skills.values():
        for prereq in skill.prerequisites:
            assert prereq in catalog.skills, f"{skill.id} -> {prereq}"


def test_every_item_skill_reference_resolves(catalog):
    for item in catalog.items.values():
        for skill_id in (*item.teaches, *item.requires):
            assert skill_id in catalog.skills, f"{item.id} -> {skill_id}"


def test_every_goal_targets_known_skills(catalog):
    for goal in catalog.goals.values():
        assert goal.target_skills
        for skill_id in goal.target_skills:
            assert skill_id in catalog.skills, f"{goal.id} -> {skill_id}"


def test_every_skill_is_teachable(catalog):
    """A path can only be built if every skill has a course behind it."""
    teachable = {s for item in catalog.items.values() for s in item.teaches}
    missing = set(catalog.skills) - teachable
    assert not missing, f"no course teaches: {sorted(missing)}"


def test_prerequisite_graph_is_acyclic(catalog):
    # topological_layers raises if a cycle exists.
    layers = catalog.topological_layers(list(catalog.skills))
    assert sum(len(layer) for layer in layers) == len(catalog.skills)


def test_topological_layers_respect_dependencies(catalog):
    layers = catalog.topological_layers(list(catalog.skills))
    placed: set[str] = set()
    for layer in layers:
        for skill_id in layer:
            for prereq in catalog.skill(skill_id).prerequisites:
                assert prereq in placed, f"{skill_id} placed before {prereq}"
        placed.update(layer)


def test_prerequisite_closure_includes_transitive_deps(catalog):
    closure = catalog.prerequisite_closure(["mlops"])
    assert "mlops" in closure
    assert "docker" in closure       # direct
    assert "linux" in closure        # via docker
    assert "cli-git" in closure      # via linux
    assert "supervised-ml" in closure  # via model-evaluation


def test_layering_is_deterministic(catalog):
    first = catalog.topological_layers(list(catalog.skills))
    second = catalog.topological_layers(list(catalog.skills))
    assert first == second


# --------------------------------------------------------- negative cases
def _skill(sid, prereqs=()):
    return Skill(id=sid, name=sid, domain="D", level=1,
                 prerequisites=tuple(prereqs), description="d")


def _course(cid, teaches=(), requires=()):
    return LearningItem(
        id=cid, title=cid, provider="p", type="course", level=1, hours=1,
        rating=4.0, learners=10, format="video", cost="free", domain="D",
        teaches=tuple(teaches), requires=tuple(requires), description="d",
    )


def _goal(gid, targets):
    return Goal(id=gid, title=gid, domain="D", description="d",
                typical_roles=(), keywords=(), target_skills=tuple(targets))


def _catalog(skills, items, goals):
    return Catalog(
        skills={s.id: s for s in skills},
        items={i.id: i for i in items},
        goals={g.id: g for g in goals},
    )


def test_validate_rejects_cycle():
    bad = _catalog(
        [_skill("a", ["b"]), _skill("b", ["a"])],
        [_course("c1", ["a"]), _course("c2", ["b"])],
        [_goal("g", ["a"])],
    )
    with pytest.raises(CatalogError, match="cycle"):
        validate(bad)


def test_validate_rejects_dangling_skill_prerequisite():
    bad = _catalog(
        [_skill("a", ["ghost"])], [_course("c1", ["a"])], [_goal("g", ["a"])]
    )
    with pytest.raises(CatalogError, match="unknown skill 'ghost'"):
        validate(bad)


def test_validate_rejects_dangling_item_reference():
    bad = _catalog(
        [_skill("a")], [_course("c1", ["a"], ["ghost"])], [_goal("g", ["a"])]
    )
    with pytest.raises(CatalogError, match="unknown skill 'ghost'"):
        validate(bad)


def test_validate_rejects_unteachable_skill():
    bad = _catalog(
        [_skill("a"), _skill("b")], [_course("c1", ["a"])], [_goal("g", ["a"])]
    )
    with pytest.raises(CatalogError, match="no course teaches"):
        validate(bad)


def test_validate_rejects_goal_with_unknown_skill():
    bad = _catalog([_skill("a")], [_course("c1", ["a"])], [_goal("g", ["ghost"])])
    with pytest.raises(CatalogError, match="targets unknown skill"):
        validate(bad)


def test_validate_rejects_item_that_teaches_and_requires_same_skill():
    bad = _catalog(
        [_skill("a")], [_course("c1", ["a"], ["a"])], [_goal("g", ["a"])]
    )
    with pytest.raises(CatalogError, match="both teaches and requires"):
        validate(bad)


def test_validate_rejects_self_referential_prerequisite():
    bad = _catalog(
        [_skill("a", ["a"])], [_course("c1", ["a"])], [_goal("g", ["a"])]
    )
    with pytest.raises(CatalogError, match="itself as a prerequisite"):
        validate(bad)
