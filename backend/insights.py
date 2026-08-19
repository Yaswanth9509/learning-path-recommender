"""Derived views over a learner's path.

Three features that make the underlying model visible rather than implied:

* **Skill graph** — the prerequisite DAG the path was generated from, laid out
  in dependency layers. This is the product's core argument made visual.
* **Weekly plan** — the roadmap resolved against real available hours, so a
  "445 hour path" becomes "week 3: finish Modern JavaScript, start DSA".
* **Achievements** — progress framed as earned rather than merely counted.
* **Pace** — what the learner actually does, as opposed to what they said they
  would do, measured from the timestamps the progress table already keeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .catalog import Catalog
from .models import (
    Achievements,
    Badge,
    LearnerProfile,
    LearningPath,
    PaceReport,
    PlannedItem,
    PlanWeek,
    ProgressEntry,
    SkillEdge,
    SkillGraph,
    SkillNode,
    Streak,
    WeeklyPlan,
)
from .profiling import DerivedProfile

MAX_PLAN_WEEKS = 60

#: Below this many days of history, any rate we computed would be noise.
MIN_DAYS_FOR_PACE = 7
#: How far the observed rate must drift from the plan before we say so.
PACE_BEHIND_RATIO = 0.75
PACE_AHEAD_RATIO = 1.25

#: XP per hour of completed material. Round numbers make the UI legible.
XP_PER_HOUR = 10
LEVEL_TITLES = [
    "Newcomer", "Apprentice", "Practitioner", "Specialist",
    "Senior", "Principal", "Distinguished",
]
#: XP needed to *enter* each level.
LEVEL_THRESHOLDS = [0, 300, 900, 2000, 4000, 7000, 11000]


# ----------------------------------------------------------------------- pace
def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_pace(
    catalog: Catalog,
    profile: LearnerProfile,
    path: LearningPath,
    progress: dict[str, ProgressEntry],
    now: datetime | None = None,
) -> PaceReport:
    """Measure the learner's real study rate against the one they planned.

    The learner told us how many hours a week they had. The progress table
    records when each item actually changed state. The difference between those
    two numbers is the only evidence we have of how they really study, and it
    is what `POST /replan` acts on.
    """
    now = now or datetime.now(timezone.utc)
    planned = max(1, profile.weekly_hours)

    stamps = [s for s in (_parse(e.updated_at) for e in progress.values()) if s]
    completed_ids = {i for i, e in progress.items() if e.status == "completed"}
    hours_completed = sum(
        catalog.items[i].hours for i in completed_ids if i in catalog.items
    )
    hours_remaining = sum(
        item.hours
        for milestone in path.milestones
        for item in milestone.items
        if item.item_id not in completed_ids
    )

    days = int((now - min(stamps)).total_seconds() // 86400) if stamps else 0
    if not stamps or days < MIN_DAYS_FOR_PACE or hours_completed <= 0:
        return PaceReport(
            status="unknown",
            planned_weekly_hours=planned,
            observed_weekly_hours=0.0,
            days_observed=days,
            hours_completed=hours_completed,
            planned_weeks_remaining=round(hours_remaining / planned, 1),
            message=(
                "Not enough history yet to measure your pace — finish a couple "
                "of items and this will start tracking your real rate."
            ),
        )

    observed = round(hours_completed / (days / 7), 1)
    ratio = observed / planned
    if ratio < PACE_BEHIND_RATIO:
        status = "behind"
    elif ratio > PACE_AHEAD_RATIO:
        status = "ahead"
    else:
        status = "on_track"

    projected = round(hours_remaining / observed, 1) if observed > 0 else None
    planned_weeks = round(hours_remaining / planned, 1)
    suggested = (
        max(1, min(60, round(observed))) if status in ("behind", "ahead") else None
    )
    if suggested == planned:
        suggested = None

    if status == "on_track":
        message = (
            f"You are averaging {observed}h a week against the {planned}h you "
            f"planned — the schedule you are looking at is realistic."
        )
    elif status == "behind":
        message = (
            f"You planned {planned}h a week and are averaging {observed}h. At "
            f"your real rate the rest takes about {projected} weeks rather than "
            f"{planned_weeks}. Re-planning around {suggested}h would give you "
            "dates you can actually trust."
        )
    else:
        message = (
            f"You are averaging {observed}h a week against {planned}h planned — "
            f"about {projected} weeks left instead of {planned_weeks}. Re-plan "
            f"around {suggested}h and the milestones will keep up with you."
        )

    return PaceReport(
        status=status,
        planned_weekly_hours=planned,
        observed_weekly_hours=observed,
        days_observed=days,
        hours_completed=hours_completed,
        projected_weeks_remaining=projected,
        planned_weeks_remaining=planned_weeks,
        suggested_weekly_hours=suggested,
        message=message,
    )


# ---------------------------------------------------------------- skill graph
def build_graph(
    catalog: Catalog,
    derived: DerivedProfile,
    path: LearningPath,
    progress: dict[str, ProgressEntry],
) -> SkillGraph:
    """The prerequisite subgraph for this goal, in dependency layers."""
    scope = (
        set(path.gap.target_skills)
        | set(path.gap.known_skills)
        | set(path.gap.missing_skills)
    )
    scope = {s for s in scope if s in catalog.skills}
    if not scope:
        return SkillGraph(
            goal_title=path.goal_title, nodes=[], edges=[],
            layer_count=0, widest_layer=0,
        )

    # Which item teaches each skill, and in which stage.
    teacher: dict[str, tuple[str, int]] = {}
    for milestone in path.milestones:
        for item in milestone.items:
            for skill_id in item.teaches:
                teacher.setdefault(skill_id, (item.title, milestone.order))

    mastered = set(derived.known_skills)
    active: set[str] = set()
    for milestone in path.milestones:
        for item in milestone.items:
            entry = progress.get(item.item_id)
            status = entry.status if entry else item.status
            if status == "completed":
                mastered.update(item.teaches)
            elif status == "in_progress":
                active.update(item.teaches)

    layers = catalog.topological_layers(scope)
    targets = set(path.gap.target_skills)

    nodes: list[SkillNode] = []
    for layer_index, layer in enumerate(layers):
        for slot, skill_id in enumerate(layer):
            skill = catalog.skill(skill_id)
            if skill_id in mastered:
                state = "mastered"
            elif skill_id in active:
                state = "in_progress"
            else:
                state = "planned"
            taught_by, milestone_order = teacher.get(skill_id, (None, None))
            nodes.append(
                SkillNode(
                    id=skill_id,
                    name=skill.name,
                    domain=skill.domain,
                    level=skill.level,
                    state=state,  # type: ignore[arg-type]
                    layer=layer_index,
                    slot=slot,
                    is_target=skill_id in targets,
                    taught_by=taught_by,
                    milestone=milestone_order,
                )
            )

    edges = [
        SkillEdge(
            source=prereq,
            target=skill_id,
            satisfied=prereq in mastered,
        )
        for skill_id in scope
        for prereq in catalog.skill(skill_id).prerequisites
        if prereq in scope
    ]
    edges.sort(key=lambda e: (e.source, e.target))

    return SkillGraph(
        goal_title=path.goal_title,
        nodes=nodes,
        edges=edges,
        layer_count=len(layers),
        widest_layer=max((len(layer) for layer in layers), default=0),
    )


# ---------------------------------------------------------------- weekly plan
def build_weekly_plan(
    path: LearningPath, progress: dict[str, ProgressEntry]
) -> WeeklyPlan:
    """Pour the remaining path into fixed-capacity weeks.

    Items are allowed to span weeks, which is what actually happens — a 60-hour
    course at 10 hours a week is six weeks of work, not one impossible one.
    """
    capacity = max(1, path.weekly_hours)
    weeks: list[PlanWeek] = []

    current_items: list[PlannedItem] = []
    current_hours = 0.0
    current_milestone = ""
    week_number = 1
    truncated = False

    def flush() -> None:
        nonlocal current_items, current_hours, week_number, current_milestone
        if not current_items:
            return
        weeks.append(
            PlanWeek(
                week=week_number,
                hours=round(current_hours, 1),
                capacity=capacity,
                milestone_title=current_milestone,
                items=current_items,
                focus=_focus_line(current_items),
            )
        )
        week_number += 1
        current_items = []
        current_hours = 0.0

    for milestone in path.milestones:
        for item in milestone.items:
            entry = progress.get(item.item_id)
            status = entry.status if entry else item.status
            if status == "completed":
                continue  # nothing left to schedule

            remaining = float(item.hours)
            first_slice = True
            while remaining > 0:
                if len(weeks) >= MAX_PLAN_WEEKS:
                    truncated = True
                    break

                free = capacity - current_hours
                if free <= 0.01:
                    flush()
                    current_milestone = milestone.title
                    continue

                take = min(free, remaining)
                if not current_milestone:
                    current_milestone = milestone.title
                current_items.append(
                    PlannedItem(
                        item_id=item.item_id,
                        title=item.title,
                        type=item.type,
                        hours_this_week=round(take, 1),
                        total_hours=item.hours,
                        portion=round(take / item.hours, 2) if item.hours else 1.0,
                        continues=not first_slice,
                        status=status,
                    )
                )
                current_hours += take
                remaining -= take
                first_slice = False
            if truncated:
                break
        if truncated:
            break

    flush()

    return WeeklyPlan(
        learner_id=path.learner_id,
        goal_title=path.goal_title,
        weekly_hours=capacity,
        total_weeks=len(weeks),
        weeks=weeks,
        truncated=truncated,
    )


def _focus_line(items: list[PlannedItem]) -> str:
    if not items:
        return "Nothing scheduled."
    finishing = [i for i in items if i.portion >= 0.999 or not i.continues]
    lead = items[0]
    if len(items) == 1:
        if lead.continues and lead.portion < 0.999:
            return f"Keep going on {lead.title}."
        if lead.portion >= 0.999:
            return f"Complete {lead.title}."
        return f"Start {lead.title}."
    titles = ", ".join(i.title for i in items[:2])
    extra = f" (+{len(items) - 2} more)" if len(items) > 2 else ""
    verb = "Wrap up and move on" if finishing else "Continue"
    return f"{verb}: {titles}{extra}."


# --------------------------------------------------------------- achievements
def build_streak(progress: dict[str, ProgressEntry]) -> Streak:
    """Consecutive days with at least one completion.

    Derived from the timestamps already recorded, so it needs no new storage
    and can never disagree with the progress log. A day counts once however
    many items were finished in it — the point is turning up, not volume.

    Today not yet counted does not break the run: a streak ending yesterday is
    still live until the day is over, otherwise it would read as broken every
    morning.
    """
    days = sorted({
        parsed.date()
        for entry in progress.values()
        if entry.status == "completed" and (parsed := _parse(entry.updated_at))
    }, reverse=True)
    if not days:
        return Streak()

    today = datetime.now(timezone.utc).date()
    week_ago = today - timedelta(days=6)
    days_this_week = sum(1 for day in days if day >= week_ago)
    active_today = days[0] == today

    # The current run only counts if it reaches today or yesterday.
    current = 0
    if days[0] >= today - timedelta(days=1):
        current = 1
        for earlier, later in zip(days[1:], days, strict=False):
            if (later - earlier).days == 1:
                current += 1
            else:
                break

    best = run = 1
    for earlier, later in zip(days[1:], days, strict=False):
        run = run + 1 if (later - earlier).days == 1 else 1
        best = max(best, run)

    return Streak(
        current_days=current,
        best_days=max(best, current),
        days_this_week=days_this_week,
        active_today=active_today,
    )


def build_achievements(
    catalog: Catalog, path: LearningPath, progress: dict[str, ProgressEntry]
) -> Achievements:
    completed_ids = {
        item_id for item_id, entry in progress.items() if entry.status == "completed"
    }

    hours_done = 0
    completed_types: set[str] = set()
    for item_id in completed_ids:
        item = catalog.items.get(item_id)
        if item is None:
            continue
        hours_done += item.hours
        completed_types.add(item.type)

    xp = hours_done * XP_PER_HOUR

    level_index = 0
    for index, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            level_index = index
    at_top = level_index >= len(LEVEL_THRESHOLDS) - 1
    floor = LEVEL_THRESHOLDS[level_index]
    ceiling = LEVEL_THRESHOLDS[level_index + 1] if not at_top else floor

    # Milestone completion.
    stages_done = 0
    for milestone in path.milestones:
        if milestone.items and all(i.item_id in completed_ids for i in milestone.items):
            stages_done += 1

    streak = build_streak(progress)
    stages_total = len(path.milestones)

    # Which domains the learner has actually finished something in. The
    # catalogue is no longer only software, so breadth is worth rewarding.
    domains_done = {
        catalog.items[item_id].domain
        for item_id in completed_ids
        if item_id in catalog.items
    }

    total_items = sum(len(m.items) for m in path.milestones)
    done_in_path = sum(
        1 for m in path.milestones for i in m.items if i.item_id in completed_ids
    )
    percent = (done_in_path / total_items) if total_items else 0.0
    foundations_done = _foundations_cleared(catalog, path, completed_ids)

    definitions = [
        ("first-step", "First Step", "Complete your first item.", "🚀",
         bool(completed_ids), "Finish any item on your path."),
        ("stage-clear", "Stage Clear", "Finish an entire stage.", "🏁",
         stages_done >= 1, "Complete every item in one stage."),
        ("foundations", "Solid Ground", "Master every foundational skill.", "🧱",
         foundations_done, "Clear the level-1 skills on your path."),
        ("builder", "Builder", "Ship a hands-on project.", "🛠️",
         "project" in completed_types, "Complete any project on your path."),
        ("proven", "Proven", "Pass an assessment.", "🎯",
         "assessment" in completed_types, "Complete any assessment."),
        ("halfway", "Halfway There", "Reach 50% of your path.", "⛰️",
         percent >= 0.5, "Get to 50% completion."),
        ("marathon", "Marathon", "Log 100 hours of study.", "⏳",
         hours_done >= 100, f"{max(0, 100 - hours_done)} hours to go."),
        ("three-stages", "Momentum", "Finish three stages.", "🔥",
         stages_done >= 3, f"{max(0, 3 - stages_done)} more stage(s)."),
        ("goal-reached", "Goal Reached", "Complete the entire path.", "🏆",
         total_items > 0 and done_in_path == total_items,
         "Complete every item on your path."),
        # --- turning up repeatedly, which is what finishing actually takes ---
        ("streak-3", "Three in a Row", "Study three days running.", "📅",
         streak.best_days >= 3,
         f"{max(0, 3 - streak.best_days)} more consecutive day(s)."),
        ("streak-7", "Full Week", "Study seven days running.", "🗓️",
         streak.best_days >= 7,
         f"{max(0, 7 - streak.best_days)} more consecutive day(s)."),
        ("consistent", "Consistent", "Study four separate days in one week.",
         "📈", streak.days_this_week >= 4,
         f"{max(0, 4 - streak.days_this_week)} more day(s) this week."),
        # --- per stage, so a long path pays out before the very end ---------
        ("two-stages", "Halfway Up", "Finish two stages.", "🧗",
         stages_done >= 2, f"{max(0, 2 - stages_done)} more stage(s)."),
        ("every-stage", "Clean Sweep", "Finish every stage on the path.", "🧹",
         stages_total > 0 and stages_done == stages_total,
         f"{max(0, stages_total - stages_done)} stage(s) left."),
        # --- breadth, now that the catalogue reaches past software ----------
        ("two-domains", "Well Rounded", "Finish work in two different fields.",
         "🌍", len(domains_done) >= 2,
         f"{max(0, 2 - len(domains_done))} more field(s)."),
        ("deep-work", "Deep Work", "Log 250 hours of study.", "🌋",
         hours_done >= 250, f"{max(0, 250 - hours_done)} hours to go."),
    ]

    badges = [
        Badge(
            id=badge_id, name=name, description=description, icon=icon,
            earned=earned, hint="" if earned else hint,
        )
        for badge_id, name, description, icon, earned, hint in definitions
    ]

    return Achievements(
        xp=xp,
        level=level_index + 1,
        level_title=LEVEL_TITLES[min(level_index, len(LEVEL_TITLES) - 1)],
        xp_into_level=xp - floor,
        xp_for_next_level=max(0, ceiling - floor),
        badges=badges,
        earned_count=sum(1 for b in badges if b.earned),
        total_count=len(badges),
        streak=streak,
        stages_completed=stages_done,
        stages_total=stages_total,
    )


def _foundations_cleared(
    catalog: Catalog, path: LearningPath, completed_ids: set[str]
) -> bool:
    """True when every level-1 skill in scope is held."""
    scope = set(path.gap.target_skills) | set(path.gap.missing_skills)
    foundational = {
        s for s in scope if s in catalog.skills and catalog.skill(s).level == 1
    }
    if not foundational:
        return False
    covered = set(path.gap.known_skills)
    for milestone in path.milestones:
        for item in milestone.items:
            if item.item_id in completed_ids:
                covered.update(item.teaches)
    return foundational.issubset(covered)


# --------------------------------------------------------------- export
def export_markdown(path: LearningPath, achievements: Achievements) -> str:
    """A shareable roadmap the learner can paste anywhere."""
    lines = [
        f"# Learning Path — {path.goal_title}",
        "",
        f"> {path.goal_description}",
        "",
        f"**{path.total_hours} hours** · **{path.total_weeks} weeks** at "
        f"{path.weekly_hours} h/week · {len(path.milestones)} stages · "
        f"revision {path.revision}",
        "",
        "## Where you stand",
        "",
        path.summary,
        "",
        f"- Skills already held: {len(path.gap.known_skills)}",
        f"- Skills to learn: {len(path.gap.missing_skills)}",
        f"- Coverage: {path.gap.coverage_pct}%",
        f"- XP: {achievements.xp} (level {achievements.level} — "
        f"{achievements.level_title})",
        "",
    ]

    if path.gap.missing_skill_names:
        lines += [
            "## Skills to acquire, in dependency order",
            "",
            *[f"{i}. {name}" for i, name in enumerate(path.gap.missing_skill_names, 1)],
            "",
        ]

    lines.append("## Roadmap")
    lines.append("")
    for milestone in path.milestones:
        lines += [
            f"### {milestone.title}",
            "",
            f"*{milestone.objective}*",
            "",
            f"`{milestone.hours}h` · ~{milestone.est_weeks} weeks · "
            f"cumulative week {milestone.cumulative_weeks}",
            "",
            "| | Item | Provider | Hours | Why it is here |",
            "|---|---|---|---:|---|",
        ]
        for item in milestone.items:
            mark = "x" if item.status == "completed" else " "
            reason = item.rationale.replace("|", "\\|")
            lines.append(
                f"| [{mark}] | **{item.title}** | {item.provider} | "
                f"{item.hours} | {reason} |"
            )
        lines.append("")

    earned = [b for b in achievements.badges if b.earned]
    if earned:
        lines += [
            "## Badges earned",
            "",
            *[f"- {b.icon} **{b.name}** — {b.description}" for b in earned],
            "",
        ]

    lines += [
        "---",
        "",
        "*Generated by the AI-Powered Personalised Learning Path Recommender. "
        "Stages are ordered by prerequisite dependency, not popularity.*",
    ]
    return "\n".join(lines)
