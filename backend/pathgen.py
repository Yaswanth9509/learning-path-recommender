"""Personalised learning path generation.

The path is built from the prerequisite DAG, not from a similarity ranking:
skills are topologically ordered so nothing is ever scheduled before the things
it depends on. Courses are then chosen greedily to cover each outstanding skill,
grouped into milestones, and each milestone is capped with a project and an
assessment that exercise what it just taught.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .catalog import Catalog, LearningItem
from .gap import analyze, explain as explain_gap
from .models import (
    LearningPath,
    Milestone,
    PathItem,
    ProgressEntry,
    SkillGapReport,
    utcnow,
)
from .profiling import DerivedProfile
from .recommender import (
    ScoringContext,
    best_course_for_skill,
    build_context,
    score_item,
)

#: A milestone should be a meaningful chunk of work, not a single video.
MIN_MILESTONES = 3
MAX_MILESTONES = 6
TARGET_COURSES_PER_MILESTONE = 3


@dataclass
class _Selection:
    """Ordered courses covering the gap, plus anything left uncovered."""

    courses: list[LearningItem]
    covered: dict[str, str]  # skill_id -> item_id that covers it
    uncovered: list[str]


def _select_courses(
    ctx: ScoringContext, ordered_layers: list[list[str]]
) -> _Selection:
    """Greedily cover every gap skill, in dependency order.

    A course that teaches several outstanding skills covers all of them at once,
    so the learner is never sent to two courses where one would have done.

    If no course can cover a skill — because the only one teaching it was
    excluded by the learner, say — that skill and everything downstream of it
    are reported as uncovered rather than scheduled anyway. Promising a course
    whose prerequisite the path never teaches would be worse than admitting the
    gap.
    """
    courses: list[LearningItem] = []
    covered: dict[str, str] = {}
    unreachable: list[str] = []
    unreachable_set: set[str] = set()
    chosen_ids: set[str] = set()

    for layer in ordered_layers:
        for skill_id in layer:
            if skill_id in covered:
                continue  # already picked up by an earlier multi-skill course

            blocked = [
                p
                for p in ctx.catalog.skill(skill_id).prerequisites
                if p in unreachable_set
            ]
            if blocked:
                unreachable.append(skill_id)
                unreachable_set.add(skill_id)
                continue

            course = best_course_for_skill(ctx, skill_id, exclude=chosen_ids)
            if course is None:
                unreachable.append(skill_id)
                unreachable_set.add(skill_id)
                continue

            chosen_ids.add(course.id)
            courses.append(course)
            for taught in course.teaches:
                if taught in ctx.gap_skills and taught not in covered:
                    covered[taught] = course.id

    return _Selection(courses=courses, covered=covered, uncovered=unreachable)


def _chunk(courses: list[LearningItem]) -> list[list[LearningItem]]:
    """Split the ordered course list into milestone-sized groups.

    Order is preserved, so grouping can never violate a prerequisite.
    """
    if not courses:
        return []
    target = max(
        1, min(len(courses), round(len(courses) / TARGET_COURSES_PER_MILESTONE) or 1)
    )
    count = max(MIN_MILESTONES, min(MAX_MILESTONES, target))
    count = min(count, len(courses))

    base, remainder = divmod(len(courses), count)
    groups: list[list[LearningItem]] = []
    cursor = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        groups.append(courses[cursor : cursor + size])
        cursor += size
    return [g for g in groups if g]


def _theme(catalog: Catalog, skills: list[str]) -> str:
    if not skills:
        return "Consolidation"
    domains: dict[str, int] = {}
    for skill_id in skills:
        domain = catalog.skill(skill_id).domain
        domains[domain] = domains.get(domain, 0) + 1
    return max(domains.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _milestone_title(catalog: Catalog, order: int, skills: list[str]) -> str:
    theme = _theme(catalog, skills)
    if not skills:
        return f"Stage {order}: {theme}"
    names = [catalog.skill_name(s) for s in skills[:2]]
    label = " & ".join(names)
    if len(skills) > 2:
        label += f" (+{len(skills) - 2} more)"
    return f"Stage {order} · {theme}: {label}"


def _milestone_objective(catalog: Catalog, skills: list[str]) -> str:
    if not skills:
        return "Consolidate and demonstrate what you have already learned."
    descriptions = [catalog.skill(s).description.rstrip(".") for s in skills[:3]]
    body = "; ".join(d[0].lower() + d[1:] for d in descriptions)
    extra = (
        f" Plus {len(skills) - 3} further skill(s) in this stage."
        if len(skills) > 3
        else ""
    )
    return f"By the end of this stage you can work with {body}.{extra}"


def _pick_support_item(
    ctx: ScoringContext,
    item_type: str,
    milestone_skills: set[str],
    available_skills: set[str],
    used: set[str],
) -> LearningItem | None:
    """Choose a project or assessment that fits this milestone.

    It must (a) only require skills the learner will hold by this point and
    (b) actually exercise something this milestone taught.
    """
    candidates: list[LearningItem] = []
    for item in ctx.catalog.items_of_type(item_type):
        if item.id in used or item.id in ctx.derived.profile.excluded_item_ids:
            continue
        if item.id in ctx.derived.profile.completed_item_ids:
            continue
        requires = set(item.requires)
        if not requires or not requires.issubset(available_skills):
            continue
        if not requires & milestone_skills:
            continue
        candidates.append(item)

    if not candidates:
        return None
    candidates.sort(
        key=lambda it: (
            -len(set(it.requires) & milestone_skills),
            -score_item(ctx, it)[0],
            it.id,
        )
    )
    return candidates[0]


def _to_path_item(
    catalog: Catalog,
    item: LearningItem,
    role: str,
    rationale: str,
    progress: dict[str, ProgressEntry],
) -> PathItem:
    entry = progress.get(item.id)
    return PathItem(
        item_id=item.id,
        title=item.title,
        provider=item.provider,
        type=item.type,
        level=item.level,
        hours=item.hours,
        rating=item.rating,
        format=item.format,
        cost=item.cost,
        description=item.description,
        url=item.url,
        teaches=list(item.teaches),
        teaches_names=[catalog.skill_name(s) for s in item.teaches],
        requires_names=[catalog.skill_name(s) for s in item.requires],
        role=role,  # type: ignore[arg-type]
        rationale=rationale,
        status=entry.status if entry else "not_started",
    )


def generate_path(
    catalog: Catalog,
    derived: DerivedProfile,
    goal_id: str,
    progress: dict[str, ProgressEntry] | None = None,
    adaptation_notes: list[str] | None = None,
    revision: int = 1,
) -> LearningPath:
    """Build a complete, prerequisite-ordered learning path."""
    profile = derived.profile
    progress = progress or {}
    goal = catalog.goal(goal_id)

    report, missing = analyze(catalog, derived, goal_id)
    required = set(report.target_skills) | set(report.known_skills) | set(missing)
    ctx = build_context(catalog, derived, missing, required_skills=required)

    layers = catalog.topological_layers(missing) if missing else []
    selection = _select_courses(ctx, layers)
    groups = _chunk(selection.courses)
    pinned = _pinned_items(catalog, profile, selection.courses)

    # Skills the learner will hold as each milestone completes.
    running_skills = set(derived.known_skills)
    used_support: set[str] = set()
    milestones: list[Milestone] = []
    cumulative_weeks = 0.0
    weekly = max(1, profile.weekly_hours)

    for index, group in enumerate(groups, start=1):
        milestone_skills: list[str] = []
        items: list[PathItem] = []

        for course in group:
            new_skills = [s for s in course.teaches if s in ctx.gap_skills]
            for skill_id in new_skills:
                if skill_id not in milestone_skills:
                    milestone_skills.append(skill_id)
            unmet = [s for s in course.requires if s not in running_skills]
            covered = ", ".join(catalog.skill_name(s) for s in new_skills)
            if unmet:
                # Unreachable for a well-formed catalog: the topological order
                # guarantees prerequisites are scheduled first. Kept as an
                # explicit, visible fallback rather than a silent wrong answer.
                names = ", ".join(catalog.skill_name(s) for s in unmet)
                rationale = (
                    f"Teaches {covered}. Note that it also assumes {names}, which "
                    "your path does not yet cover — review that first."
                )
            else:
                rationale = (
                    f"Closes your gap in {covered}. Every prerequisite is already "
                    "met by the time you reach this item."
                )
            items.append(_to_path_item(catalog, course, "core", rationale, progress))
            # Advance immediately: later items in the same stage may depend on
            # what this course just taught.
            running_skills |= set(new_skills)

        # Anything the learner pinned goes in as soon as it is possible — the
        # first stage by which they hold everything it requires.
        for extra in list(pinned):
            if not set(extra.requires).issubset(running_skills):
                continue
            pinned.remove(extra)
            taught = [catalog.skill_name(s) for s in extra.teaches]
            covers = f" It covers {', '.join(taught)}." if taught else ""
            items.append(
                _to_path_item(
                    catalog,
                    extra,
                    "core" if extra.type in ("course", "resource") else extra.type,
                    f"You added this yourself, and this is the earliest stage "
                    f"where every prerequisite for it is met.{covers}",
                    progress,
                )
            )
            running_skills |= set(extra.teaches)

        milestone_skill_set = set(milestone_skills)

        project = _pick_support_item(
            ctx, "project", milestone_skill_set, running_skills, used_support
        )
        if project is not None:
            used_support.add(project.id)
            exercised = ", ".join(
                catalog.skill_name(s)
                for s in project.requires
                if s in milestone_skill_set
            )
            items.append(
                _to_path_item(
                    catalog,
                    project,
                    "project",
                    f"Applies {exercised} on a realistic brief, turning this stage's "
                    "theory into something you can show.",
                    progress,
                )
            )

        assessment = _pick_support_item(
            ctx, "assessment", milestone_skill_set, running_skills, used_support
        )
        if assessment is not None:
            used_support.add(assessment.id)
            checked = ", ".join(
                catalog.skill_name(s)
                for s in assessment.requires
                if s in milestone_skill_set
            )
            items.append(
                _to_path_item(
                    catalog,
                    assessment,
                    "assessment",
                    f"Verifies {checked} before you move on, so a weak spot here "
                    "does not compound in later stages.",
                    progress,
                )
            )

        hours = sum(i.hours for i in items)
        est_weeks = round(hours / weekly, 1)
        cumulative_weeks = round(cumulative_weeks + est_weeks, 1)
        milestone_id = f"m{index}"
        milestones.append(
            Milestone(
                id=milestone_id,
                order=index,
                title=_milestone_title(catalog, index, milestone_skills),
                objective=_milestone_objective(catalog, milestone_skills),
                skills=milestone_skills,
                skill_names=[catalog.skill_name(s) for s in milestone_skills],
                items=items,
                hours=hours,
                est_weeks=est_weeks,
                cumulative_weeks=cumulative_weeks,
                prerequisite_milestones=[f"m{index - 1}"] if index > 1 else [],
            )
        )

    # No gap at all: still give the learner a way to prove the goal.
    if not milestones:
        milestones = _proof_only_milestone(catalog, ctx, report, progress, weekly)
        cumulative_weeks = milestones[0].cumulative_weeks if milestones else 0.0

    # Pinned items whose prerequisites this path never covers still belong to
    # the learner. Put them at the end and say plainly why they are there.
    if pinned and milestones:
        last = milestones[-1]
        for extra in pinned:
            unmet = [s for s in extra.requires if s not in running_skills]
            reason = (
                "You added this yourself. "
                + (
                    "This path does not cover "
                    + ", ".join(catalog.skill_name(s) for s in unmet)
                    + ", which it assumes, so treat it as optional."
                    if unmet
                    else "It sits at the end because nothing else depends on it."
                )
            )
            last.items.append(
                _to_path_item(
                    catalog,
                    extra,
                    "core" if extra.type in ("course", "resource") else extra.type,
                    reason,
                    progress,
                )
            )
        last.hours = sum(item.hours for item in last.items)
        last.est_weeks = round(last.hours / weekly, 1)
        cumulative_weeks = round(
            sum(milestone.est_weeks for milestone in milestones), 1
        )
        last.cumulative_weeks = cumulative_weeks

    total_hours = sum(m.hours for m in milestones)
    summary = _summarize(catalog, goal.title, report, milestones, total_hours, weekly)

    return LearningPath(
        path_id=uuid.uuid4().hex[:12],
        learner_id=profile.learner_id,
        goal_id=goal_id,
        goal_title=goal.title,
        goal_description=goal.description,
        generated_at=utcnow(),
        revision=revision,
        summary=summary,
        gap=report,
        milestones=milestones,
        total_hours=total_hours,
        total_weeks=round(total_hours / weekly, 1),
        weekly_hours=weekly,
        uncovered_skills=[catalog.skill_name(s) for s in selection.uncovered],
        adaptation_notes=adaptation_notes or [],
    )


def _pinned_items(
    catalog: Catalog, profile, already_selected: list[LearningItem]
) -> list[LearningItem]:
    """Items the learner added to the path by hand.

    The planner picks what closes the gap; this is the learner overruling it —
    "I want this course too". They are still placed by prerequisite, never
    before the stage that makes them possible.
    """
    chosen = {item.id for item in already_selected}
    return [
        catalog.items[item_id]
        for item_id in profile.pinned_item_ids
        if item_id in catalog.items
        and item_id not in chosen
        and item_id not in profile.excluded_item_ids
        and item_id not in profile.completed_item_ids
    ]


def _proof_only_milestone(
    catalog: Catalog,
    ctx: ScoringContext,
    report: SkillGapReport,
    progress: dict[str, ProgressEntry],
    weekly: int,
) -> list[Milestone]:
    """When there is no skill gap, build a portfolio-and-proof stage."""
    target_skills = set(report.target_skills)
    known = set(report.known_skills) | target_skills
    items: list[PathItem] = []
    used: set[str] = set()

    for item_type, role, verb in (
        ("project", "project", "Demonstrates"),
        ("assessment", "assessment", "Certifies"),
    ):
        for _ in range(2):
            pick = _pick_support_item(ctx, item_type, target_skills, known, used)
            if pick is None:
                break
            used.add(pick.id)
            names = ", ".join(
                catalog.skill_name(s) for s in pick.requires if s in target_skills
            )
            items.append(
                _to_path_item(
                    catalog,
                    pick,
                    role,
                    f"{verb} {names}. You already hold the underlying skills, so "
                    "this stage is about producing evidence.",
                    progress,
                )
            )

    if not items:
        return []

    hours = sum(i.hours for i in items)
    weeks = round(hours / weekly, 1)
    return [
        Milestone(
            id="m1",
            order=1,
            title="Stage 1 · Proof: portfolio and assessment",
            objective=(
                "You already hold every skill this goal requires. This stage "
                "produces the evidence — a portfolio project and a formal check."
            ),
            skills=sorted(target_skills),
            skill_names=[catalog.skill_name(s) for s in sorted(target_skills)],
            items=items,
            hours=hours,
            est_weeks=weeks,
            cumulative_weeks=weeks,
        )
    ]


def _summarize(
    catalog: Catalog,
    goal_title: str,
    report: SkillGapReport,
    milestones: list[Milestone],
    total_hours: int,
    weekly: int,
) -> str:
    weeks = round(total_hours / weekly, 1)
    item_count = sum(len(m.items) for m in milestones)
    projects = sum(1 for m in milestones for i in m.items if i.role == "project")
    assessments = sum(1 for m in milestones for i in m.items if i.role == "assessment")
    gap_line = explain_gap(catalog, report)
    stage_word = "stage" if len(milestones) == 1 else "stages"
    item_word = "item" if item_count == 1 else "items"
    parts = []
    if projects:
        parts.append(f"{projects} project" + ("" if projects == 1 else "s"))
    if assessments:
        parts.append(f"{assessments} assessment" + ("" if assessments == 1 else "s"))
    mix = f" including {' and '.join(parts)}" if parts else ""
    return (
        f"{gap_line} Your path to {goal_title} runs {len(milestones)} {stage_word} "
        f"and {item_count} {item_word}{mix}, about {total_hours} hours — roughly "
        f"{weeks} weeks at {weekly} hours a week. Stages are ordered so every "
        "course's prerequisites are covered before you reach it."
    )
