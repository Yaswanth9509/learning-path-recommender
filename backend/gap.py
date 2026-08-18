"""Skill gap analysis.

Compares what a learner already holds against the transitive closure of what
their goal demands, and reports the difference in a form the UI can explain.
"""

from __future__ import annotations

from .catalog import Catalog
from .models import SkillGapReport
from .profiling import DerivedProfile


def resolve_target_skills(
    catalog: Catalog, goal_id: str, extra_skills: list[str] | None = None
) -> list[str]:
    """The goal's headline skills plus any the learner added explicitly."""
    goal = catalog.goal(goal_id)
    targets = list(goal.target_skills)
    for skill_id in extra_skills or []:
        if skill_id in catalog.skills and skill_id not in targets:
            targets.append(skill_id)
    return targets


def analyze(
    catalog: Catalog, derived: DerivedProfile, goal_id: str
) -> tuple[SkillGapReport, list[str]]:
    """Produce a gap report and the ordered list of skills still to learn.

    The second return value is the *required* set minus what the learner knows —
    the exact input to path generation.
    """
    targets = resolve_target_skills(
        catalog, goal_id, derived.profile.extra_target_skills
    )
    required = catalog.prerequisite_closure(targets)
    known = set(derived.known_skills)

    missing = sorted(
        required - known,
        key=lambda s: (catalog.depth(s), catalog.skill(s).level, catalog.skill(s).name),
    )
    # Skills that are not the goal itself but must be crossed to reach it.
    bridging = [s for s in missing if s not in targets]
    known_in_scope = sorted(required & known, key=catalog.skill_name)

    coverage = 100.0 if not required else round(
        100.0 * len(required & known) / len(required), 1
    )

    report = SkillGapReport(
        target_skills=targets,
        target_skill_names=[catalog.skill_name(s) for s in targets],
        known_skills=known_in_scope,
        known_skill_names=[catalog.skill_name(s) for s in known_in_scope],
        implied_skills=sorted(
            s for s in derived.implied_skills if s in required
        ),
        missing_skills=missing,
        missing_skill_names=[catalog.skill_name(s) for s in missing],
        bridging_skills=bridging,
        coverage_pct=coverage,
    )
    return report, missing


def explain(catalog: Catalog, report: SkillGapReport) -> str:
    """A sentence or two describing the gap in plain language."""
    if not report.missing_skills:
        return (
            "You already hold every skill this goal requires — the path below "
            "focuses on projects and assessments to prove it."
        )

    bridging = report.bridging_skills
    lead = (
        f"You hold {len(report.known_skills)} of the "
        f"{len(report.known_skills) + len(report.missing_skills)} skills this goal "
        f"requires ({report.coverage_pct}%). "
    )
    body = f"{len(report.missing_skills)} skills remain"
    if bridging:
        names = ", ".join(catalog.skill_name(s) for s in bridging[:3])
        more = f" and {len(bridging) - 3} more" if len(bridging) > 3 else ""
        body += (
            f", of which {len(bridging)} are prerequisites rather than headline "
            f"goal skills — {names}{more}. Those come first because the rest "
            "depend on them"
        )
    return lead + body + "."
