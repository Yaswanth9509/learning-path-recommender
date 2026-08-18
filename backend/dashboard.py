"""Dashboard aggregation.

Reduces a path plus a progress table into the numbers the UI renders: overall
completion, per-milestone state, skill acquisition by domain, and the specific
next actions the learner should take.
"""

from __future__ import annotations

from .catalog import Catalog
from .models import (
    Dashboard,
    LearningPath,
    MilestoneProgress,
    NextAction,
    PaceReport,
    ProgressEntry,
    SkillProgress,
)
from .profiling import DerivedProfile

MAX_NEXT_ACTIONS = 3


def build(
    catalog: Catalog,
    derived: DerivedProfile,
    path: LearningPath,
    progress: dict[str, ProgressEntry],
    pace: PaceReport | None = None,
) -> Dashboard:
    all_items = [item for m in path.milestones for item in m.items]
    path_item_ids = {i.item_id for i in all_items}

    def status_of(item_id: str) -> str:
        entry = progress.get(item_id)
        return entry.status if entry else "not_started"

    completed_ids = {i.item_id for i in all_items if status_of(i.item_id) == "completed"}
    in_progress_ids = {
        i.item_id for i in all_items if status_of(i.item_id) == "in_progress"
    }

    # Finishing a course removes its skill from the gap, so the next rebuild
    # drops that course from the plan. Count it anyway: a learner's completed
    # work must not vanish from their progress just because the path moved on.
    retired = [
        catalog.items[item_id]
        for item_id, entry in progress.items()
        if entry.status == "completed"
        and item_id not in path_item_ids
        and item_id in catalog.items
    ]

    total_items = len(all_items) + len(retired)
    done_count = len(completed_ids) + len(retired)
    hours_total = sum(i.hours for i in all_items) + sum(i.hours for i in retired)
    hours_done = (
        sum(i.hours for i in all_items if i.item_id in completed_ids)
        + sum(i.hours for i in retired)
    )
    overall = round(100.0 * done_count / total_items, 1) if total_items else 0.0

    milestones, current_id = _milestone_progress(path, completed_ids, in_progress_ids)
    skill_progress = _skill_progress(
        catalog, derived, path, completed_ids, in_progress_ids
    )
    next_actions = _next_actions(path, completed_ids, in_progress_ids)

    domain_breakdown: dict[str, dict[str, int]] = {}
    for entry in skill_progress:
        bucket = domain_breakdown.setdefault(
            entry.domain, {"mastered": 0, "in_progress": 0, "planned": 0}
        )
        bucket[entry.state] += 1

    hours_remaining = max(0, hours_total - hours_done)
    weekly = max(1, path.weekly_hours)
    mastered = sum(1 for s in skill_progress if s.state == "mastered")

    return Dashboard(
        learner_id=path.learner_id,
        goal_title=path.goal_title,
        overall_percent=overall,
        items_completed=done_count,
        items_total=total_items,
        hours_completed=hours_done,
        hours_total=hours_total,
        hours_remaining=hours_remaining,
        weeks_remaining=round(hours_remaining / weekly, 1),
        skills_mastered=mastered,
        skills_total=len(skill_progress),
        skill_progress=skill_progress,
        domain_breakdown=domain_breakdown,
        milestones=milestones,
        next_actions=next_actions,
        current_milestone=current_id,
        streak_note=_streak_note(overall, done_count, total_items),
        pace=pace,
    )


def _milestone_progress(
    path: LearningPath, completed: set[str], in_progress: set[str]
) -> tuple[list[MilestoneProgress], str | None]:
    rows: list[MilestoneProgress] = []
    current: str | None = None
    previous_complete = True

    for milestone in path.milestones:
        total = len(milestone.items)
        done = sum(1 for i in milestone.items if i.item_id in completed)
        started = any(i.item_id in in_progress for i in milestone.items)
        percent = round(100.0 * done / total, 1) if total else 0.0

        if total and done == total:
            status = "completed"
        elif not previous_complete:
            status = "locked"
        elif done or started:
            status = "in_progress"
        else:
            status = "available"

        if current is None and status in ("in_progress", "available"):
            current = milestone.id

        rows.append(
            MilestoneProgress(
                milestone_id=milestone.id,
                title=milestone.title,
                order=milestone.order,
                total_items=total,
                completed_items=done,
                percent=percent,
                status=status,  # type: ignore[arg-type]
                est_weeks=milestone.est_weeks,
            )
        )
        previous_complete = total > 0 and done == total

    return rows, current


def _skill_progress(
    catalog: Catalog,
    derived: DerivedProfile,
    path: LearningPath,
    completed: set[str],
    in_progress: set[str],
) -> list[SkillProgress]:
    """Every skill in scope for the goal, tagged with how far along it is."""
    scope = set(path.gap.target_skills) | set(path.gap.known_skills) | set(
        path.gap.missing_skills
    )

    mastered = set(derived.known_skills)
    active: set[str] = set()
    for milestone in path.milestones:
        for item in milestone.items:
            if item.item_id in completed:
                mastered.update(item.teaches)
            elif item.item_id in in_progress:
                active.update(item.teaches)

    rows: list[SkillProgress] = []
    for skill_id in sorted(scope, key=lambda s: (catalog.skill(s).domain, catalog.skill(s).level, catalog.skill_name(s))):
        skill = catalog.skill(skill_id)
        if skill_id in mastered:
            state = "mastered"
        elif skill_id in active:
            state = "in_progress"
        else:
            state = "planned"
        rows.append(
            SkillProgress(
                skill_id=skill_id,
                name=skill.name,
                domain=skill.domain,
                level=skill.level,
                state=state,  # type: ignore[arg-type]
            )
        )
    return rows


def _next_actions(
    path: LearningPath, completed: set[str], in_progress: set[str]
) -> list[NextAction]:
    """The first few unfinished items, in path order.

    In-progress work is surfaced before anything new, so the dashboard never
    tells a learner to start a fifth thing while four are open.
    """
    started: list[NextAction] = []
    fresh: list[NextAction] = []

    for milestone in path.milestones:
        for item in milestone.items:
            if item.item_id in completed:
                continue
            action = NextAction(
                item_id=item.item_id,
                title=item.title,
                type=item.type,
                hours=item.hours,
                milestone_title=milestone.title,
                why=item.rationale,
            )
            if item.item_id in in_progress:
                started.append(action)
            elif len(fresh) < MAX_NEXT_ACTIONS:
                fresh.append(action)
        if len(started) + len(fresh) >= MAX_NEXT_ACTIONS and started:
            break

    for action in started:
        action.why = "Already in progress — finish this before starting anything new."
    return (started + fresh)[:MAX_NEXT_ACTIONS]


def _streak_note(percent: float, done: int, total: int) -> str:
    if total == 0:
        return "No items scheduled yet."
    if done == 0:
        return "Nothing started yet — the first item is the hardest one to open."
    if percent >= 100:
        return "Path complete. Time to set a new goal."
    if percent >= 75:
        return f"{done} of {total} done — the finish line is in sight."
    if percent >= 40:
        return f"{done} of {total} done — past the halfway grind."
    return f"{done} of {total} done — momentum is building."
