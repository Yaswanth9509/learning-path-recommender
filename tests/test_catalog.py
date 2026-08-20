"""Catalog integrity. These tests are the guard against inconsistent data."""

from __future__ import annotations

import pytest

from backend.catalog import (
    Catalog,
    CatalogError,
    Goal,
    LearningItem,
    Skill,
    get_catalog,
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
        format="video", cost="free", domain="D",
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


# ------------------------------------------------------------------- breadth
#: Goals outside software, so the product is not implicitly "for developers".
NON_SOFTWARE_GOALS = [
    "goal-product-manager",
    "goal-digital-marketer",
    "goal-ux-designer",
    "goal-financial-analyst",
    "goal-mathematics",
]


def test_the_catalogue_covers_more_than_software(catalog):
    """A learner outside tech has to find themselves in the goal list."""
    software = {"Data & AI", "Web Development", "Cloud & DevOps", "Foundations"}
    other = {goal.domain for goal in catalog.goals.values()} - software
    assert len(other) >= 4, f"only {other} sit outside software"


def test_a_subject_goal_exists_not_only_careers(catalog):
    """Someone can want to learn a subject without wanting the job."""
    assert "goal-mathematics" in catalog.goals
    assert catalog.goals["goal-mathematics"].domain == "Mathematics"


@pytest.mark.parametrize("goal_id", NON_SOFTWARE_GOALS)
def test_every_non_software_goal_builds_a_real_path(goal_id, catalog):
    """Content is only real if the planner can actually schedule it."""
    from backend.models import LearnerProfile
    from backend.pathgen import generate_path
    from backend.profiling import derive

    profile = LearnerProfile(
        learner_id="breadth", goal_id=goal_id, experience_level="beginner",
        weekly_hours=8,
    )
    path = generate_path(catalog, derive(catalog, profile), goal_id)
    items = [item for milestone in path.milestones for item in milestone.items]

    assert len(path.milestones) >= 2, "a one-stage path is not a roadmap"
    assert len(items) >= 5, f"{goal_id} scheduled only {len(items)} items"
    assert path.total_hours > 0

    # Nothing may be scheduled before a skill it depends on is taught.
    taught: set[str] = set()
    for item in items:
        catalogue_item = catalog.items[item.item_id]
        missing = set(catalogue_item.requires) - taught
        unmet = {sid for sid in missing if sid in catalog.skills}
        assert not unmet, f"{item.item_id} scheduled before {sorted(unmet)}"
        taught.update(catalogue_item.teaches)


@pytest.mark.parametrize("phrase,expected", [
    ("I want to become a product manager", "goal-product-manager"),
    ("help me become a UX designer", "goal-ux-designer"),
    ("I want to be a financial analyst", "goal-financial-analyst"),
    ("I want to get better at maths and calculus", "goal-mathematics"),
    ("i want to be a data analyst", "goal-data-analyst"),
])
def test_new_domains_are_reachable_from_plain_language(phrase, expected, catalog):
    """Content nobody can ask for may as well not be in the catalogue."""
    from backend import assistant

    assert assistant._interpret_with_rules(catalog, phrase).goal_id == expected



# --------------------------------------------------------------- goal kinds
def test_every_goal_says_what_kind_of_goal_it_is(catalog):
    """A roadmap toward an exam is not a roadmap toward a job."""
    from backend.catalog import GOAL_KINDS

    for goal in catalog.goals.values():
        assert goal.kind in GOAL_KINDS, f"{goal.id} has kind {goal.kind!r}"


def test_the_catalogue_covers_all_four_kinds(catalog):
    """People learn for a job, a subject, an exam, or a certificate."""
    kinds = {goal.kind for goal in catalog.goals.values()}
    assert kinds == {"job", "subject", "exam", "certification"}, kinds


def test_exams_and_subjects_are_not_phrased_as_jobs(catalog):
    """"I want to become a GATE" is nonsense."""
    from backend.assistant import goal_phrase

    assert goal_phrase(catalog.goals["goal-gate-cs"]).startswith("I want to crack")
    assert goal_phrase(catalog.goals["goal-ug-economics"]).startswith("I want to study")
    assert goal_phrase(catalog.goals["goal-cfa-1"]).startswith("I want to get")
    assert goal_phrase(catalog.goals["goal-data-analyst"]).startswith("I want to become")


NON_JOB_GOALS = [
    "goal-jee", "goal-neet", "goal-gate-cs", "goal-upsc", "goal-cat",
    "goal-gre", "goal-gmat", "goal-ielts", "goal-toefl", "goal-sat",
    "goal-cfa-1", "goal-cloud-cert",
    "goal-ug-cs", "goal-ug-economics", "goal-ug-psychology",
    "goal-pg-datascience",
]


@pytest.mark.parametrize("goal_id", NON_JOB_GOALS)
def test_every_exam_and_subject_builds_a_real_path(goal_id, catalog):
    """Content is only real if the planner can actually schedule it."""
    from backend.models import LearnerProfile
    from backend.pathgen import generate_path
    from backend.profiling import derive

    profile = LearnerProfile(
        learner_id="kinds", goal_id=goal_id, experience_level="beginner",
        weekly_hours=10,
    )
    path = generate_path(catalog, derive(catalog, profile), goal_id)
    items = [item for milestone in path.milestones for item in milestone.items]
    assert len(path.milestones) >= 2, f"{goal_id} is a one-stage path"
    assert len(items) >= 4, f"{goal_id} scheduled only {len(items)} items"

    taught: set[str] = set()
    for item in items:
        entry = catalog.items[item.item_id]
        unmet = {s for s in entry.requires if s in catalog.skills} - taught
        assert not unmet, f"{item.item_id} scheduled before {sorted(unmet)}"
        taught.update(entry.teaches)


@pytest.mark.parametrize("phrase,expected", [
    ("i want to crack gate", "goal-gate-cs"),
    ("preparing for neet", "goal-neet"),
    ("i want to clear upsc", "goal-upsc"),
    ("JEE preparation", "goal-jee"),
    ("i want to do masters in data science", "goal-pg-datascience"),
    ("undergraduate computer science syllabus", "goal-ug-cs"),
    ("i want to become a data analyst", "goal-data-analyst"),
])
def test_exams_and_degrees_are_reachable_from_plain_language(phrase, expected, catalog):
    from backend import assistant

    assert assistant._interpret_with_rules(catalog, phrase).goal_id == expected


def test_every_course_links_somewhere_real():
    """125 of 125 courses once had no URL at all.

    A course the learner cannot open is a label on an empty bottle. Projects
    and self-assessments are exempt: those are work the learner does, not a
    page somebody else hosts.
    """
    catalog = get_catalog()
    linkless = [
        item.id for item in catalog.items.values()
        if item.type not in ("project", "assessment") and not item.url
    ]
    assert not linkless, f"courses with no url: {linkless}"


def test_no_item_claims_an_invented_provider():
    """The catalogue used to credit 22 providers that do not exist.

    Once the links became real the names had to as well, or the page would
    cite "Lyceum Open Courseware" while pointing at MIT.
    """
    invented = {
        "Lyceum Open Courseware", "DevPath", "QuantAcademy", "CloudBridge",
        "Studio Method", "MLWorks", "Beacon Marketing Institute", "Prep Forge",
        "WebCraft", "Ledger Institute", "DataForge", "Beacon Language Lab",
        "OpenLearn", "FrontierUI", "Northgate Business School", "ProductCraft",
        "Civic Institute", "NeuralSchool", "CampusX", "MetricLab",
        "SecureStack", "SignalOps",
    }
    offenders = [
        f"{item.id} -> {item.provider}"
        for item in get_catalog().items.values() if item.provider in invented
    ]
    assert not offenders, f"items crediting a provider that does not exist: {offenders}"


def test_the_catalogue_carries_no_invented_ratings():
    """Every item shipped a rating between 4.2 and 4.9 and a learner count.

    Both were made up. Harmless as flavour on a fictional provider; not
    harmless once the provider became MIT OpenCourseWare, where a fabricated
    4.7 borrows real authority.
    """
    import json
    from pathlib import Path
    raw = json.loads(
        (Path(__file__).resolve().parent.parent / "backend" / "data" / "courses.json")
        .read_text(encoding="utf-8")
    )
    carrying = [i["id"] for i in raw if "rating" in i or "learners" in i]
    assert not carrying, f"items still carrying invented numbers: {carrying}"


def test_every_url_is_a_plausible_https_link():
    """Offline shape check. The network verification happened once, at
    authoring time; the suite must never reach for a socket to pass.
    """
    import re
    bad = []
    for item in get_catalog().items.values():
        if not item.url:
            continue
        if not re.match(r"^https://[a-z0-9.-]+\.[a-z]{2,}(/|$)", item.url, re.I):
            bad.append(f"{item.id} -> {item.url}")
    assert not bad, f"urls that are not plain https links: {bad}"


def test_leetcode_practice_sets_point_at_study_plans():
    """A study plan is a curated multi-hour set, which is what an assessment
    item represents. A single problem is twenty minutes and does not match.
    """
    leet = {i.id: i.url for i in get_catalog().items.values()
            if i.url and "leetcode.com" in i.url}
    assert leet, "the LeetCode practice sets have gone"
    for item_id, url in leet.items():
        assert "/studyplan/" in url, (
            f"{item_id} links to {url}, which is not a study plan"
        )
