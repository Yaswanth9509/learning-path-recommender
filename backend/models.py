"""Pydantic schemas shared by the engines and the HTTP layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

ExperienceLevel = Literal["beginner", "intermediate", "advanced"]
ItemStatus = Literal["not_started", "in_progress", "completed"]
FeedbackSignal = Literal["too_easy", "too_hard", "not_relevant", "liked"]

LEVEL_TO_INT: dict[str, int] = {"beginner": 1, "intermediate": 2, "advanced": 3}

#: Bounds on anything a client can send. A chat message is forwarded to an LLM
#: provider and stored, so it is capped; ids are capped and pattern-checked
#: because `POST /api/chat` will create a learner row for an unknown one.
#: A learner can pursue several goals at once. More than a handful stops being
#: a plan and starts being a wish list, so the number is capped.
MAX_ACTIVE_PATHS = 3

MAX_MESSAGE_CHARS = 2000
MAX_NAME_CHARS = 80
MAX_NOTE_CHARS = 500
MAX_LIST_ITEMS = 200
LEARNER_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- learner side
class LearnerProfile(BaseModel):
    """Everything the system knows about a learner."""

    learner_id: str
    name: str = Field("Learner", max_length=MAX_NAME_CHARS)
    #: The goal whose roadmap is currently on screen.
    goal_id: Optional[str] = None
    #: Every goal being pursued, active one included, oldest first.
    goal_ids: list[str] = Field(default_factory=list, max_length=MAX_ACTIVE_PATHS)
    goal_text: str = Field("", max_length=MAX_MESSAGE_CHARS)
    experience_level: ExperienceLevel = "beginner"
    interests: list[str] = Field(default_factory=list)
    completed_item_ids: list[str] = Field(default_factory=list)
    declared_skills: list[str] = Field(default_factory=list)
    weekly_hours: int = 8
    preferred_formats: list[str] = Field(default_factory=list)
    cost_preference: Literal["free", "any"] = "any"
    #: extra skills the learner explicitly asked for beyond the goal template
    extra_target_skills: list[str] = Field(default_factory=list)
    #: item ids the learner rejected; never recommended again
    excluded_item_ids: list[str] = Field(default_factory=list)
    #: items the learner added to the path by hand; always scheduled
    pinned_item_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    #: which engine answers: "auto" prefers the configured provider and falls
    #: back to the rules; "rules" pins the deterministic engine.
    assistant_engine: Literal["auto", "rules"] = "auto"
    #: derived from feedback: shifts preferred course difficulty (-1, 0, +1)
    difficulty_offset: int = 0
    provider_affinity: dict[str, int] = Field(default_factory=dict)
    format_affinity: dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    @field_validator("weekly_hours")
    @classmethod
    def _sane_hours(cls, value: int) -> int:
        return max(1, min(60, value))

    @field_validator("difficulty_offset")
    @classmethod
    def _clamp_offset(cls, value: int) -> int:
        return max(-1, min(1, value))

    @property
    def level_int(self) -> int:
        return LEVEL_TO_INT[self.experience_level]

    @property
    def target_level(self) -> int:
        """Preferred item difficulty, after feedback adaptation."""
        return max(1, min(3, self.level_int + self.difficulty_offset))


class ProfileUpdate(BaseModel):
    """Partial profile update. Only supplied fields are applied."""

    name: Optional[str] = Field(None, max_length=MAX_NAME_CHARS)
    goal_id: Optional[str] = None
    goal_text: Optional[str] = Field(None, max_length=MAX_MESSAGE_CHARS)
    experience_level: Optional[ExperienceLevel] = None
    interests: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    completed_item_ids: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    declared_skills: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    weekly_hours: Optional[int] = Field(None, ge=1, le=60)
    preferred_formats: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    cost_preference: Optional[Literal["free", "any"]] = None
    extra_target_skills: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    pinned_item_ids: Optional[list[str]] = Field(None, max_length=MAX_LIST_ITEMS)
    assistant_engine: Optional[Literal["auto", "rules"]] = None


# ------------------------------------------------------------ recommendations
class ScoreBreakdown(BaseModel):
    goal_relevance: float
    skill_readiness: float
    level_fit: float
    interest_match: float
    format_fit: float


class Recommendation(BaseModel):
    item_id: str
    title: str
    provider: str
    type: str
    level: int
    hours: int
    format: str
    cost: str
    domain: str
    description: str
    #: Canonical link when the provider publishes one; empty otherwise.
    url: str = ""
    teaches: list[str]
    teaches_names: list[str]
    requires: list[str]
    requires_names: list[str]
    score: float
    breakdown: ScoreBreakdown
    reasons: list[str]
    #: gap skills this item unlocks for the learner right now
    closes_gap_skills: list[str] = Field(default_factory=list)
    prerequisites_met: bool = True
    missing_prerequisites: list[str] = Field(default_factory=list)


# -------------------------------------------------------------- learning path
class PathItem(BaseModel):
    item_id: str
    title: str
    provider: str
    type: str
    level: int
    hours: int
    format: str
    cost: str
    description: str
    url: str = ""
    teaches: list[str]
    teaches_names: list[str]
    requires_names: list[str]
    role: Literal["core", "project", "assessment"]
    rationale: str
    status: ItemStatus = "not_started"


class Milestone(BaseModel):
    id: str
    order: int
    title: str
    objective: str
    skills: list[str]
    skill_names: list[str]
    items: list[PathItem]
    hours: int
    est_weeks: float
    cumulative_weeks: float
    prerequisite_milestones: list[str] = Field(default_factory=list)


class SkillGapReport(BaseModel):
    target_skills: list[str]
    target_skill_names: list[str]
    known_skills: list[str]
    known_skill_names: list[str]
    #: known because a prerequisite of something the learner already has
    implied_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str]
    missing_skill_names: list[str]
    #: prerequisite skills not in the goal but needed to reach it
    bridging_skills: list[str] = Field(default_factory=list)
    coverage_pct: float


class LearningPath(BaseModel):
    path_id: str
    learner_id: str
    goal_id: str
    goal_title: str
    goal_description: str
    generated_at: str = Field(default_factory=utcnow)
    revision: int = 1
    summary: str
    gap: SkillGapReport
    milestones: list[Milestone]
    total_hours: int
    total_weeks: float
    weekly_hours: int
    #: skills in the goal that no course in the catalog could cover
    uncovered_skills: list[str] = Field(default_factory=list)
    adaptation_notes: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------- progress
class ProgressEntry(BaseModel):
    item_id: str
    status: ItemStatus
    updated_at: str = Field(default_factory=utcnow)


class ProgressUpdate(BaseModel):
    item_id: str
    status: ItemStatus


class FeedbackEntry(BaseModel):
    item_id: str
    signal: FeedbackSignal
    note: str = ""
    created_at: str = Field(default_factory=utcnow)


class FeedbackRequest(BaseModel):
    item_id: str
    signal: FeedbackSignal
    note: str = Field("", max_length=MAX_NOTE_CHARS)


# ------------------------------------------------------------------- dashboard
class SkillProgress(BaseModel):
    skill_id: str
    name: str
    domain: str
    level: int
    state: Literal["mastered", "in_progress", "planned"]


class MilestoneProgress(BaseModel):
    milestone_id: str
    title: str
    order: int
    total_items: int
    completed_items: int
    percent: float
    status: Literal["locked", "available", "in_progress", "completed"]
    est_weeks: float


class NextAction(BaseModel):
    item_id: str
    title: str
    type: str
    hours: int
    milestone_title: str
    why: str


class PaceReport(BaseModel):
    """Observed study behaviour, as opposed to what the learner said they'd do.

    `status` is `unknown` until there is enough history to say anything
    honest — one completed item on the same day it was started tells us nothing.
    """

    status: Literal["unknown", "ahead", "on_track", "behind"]
    planned_weekly_hours: int
    observed_weekly_hours: float
    days_observed: int
    hours_completed: int
    #: Weeks left at the observed rate, versus at the rate the learner planned.
    projected_weeks_remaining: Optional[float] = None
    planned_weeks_remaining: Optional[float] = None
    #: A weekly-hours figure worth re-planning around, when it differs enough.
    suggested_weekly_hours: Optional[int] = None
    message: str


class PathSummary(BaseModel):
    """One of the learner's roadmaps, as the switcher and dashboard show it."""

    goal_id: str
    goal_title: str
    domain: str
    is_active: bool
    percent: float
    items_completed: int
    items_total: int
    hours_total: int
    hours_remaining: int
    total_weeks: float
    stages: int
    completed: bool


class Dashboard(BaseModel):
    learner_id: str
    goal_title: str
    overall_percent: float
    items_completed: int
    items_total: int
    hours_completed: int
    hours_total: int
    hours_remaining: int
    weeks_remaining: float
    skills_mastered: int
    skills_total: int
    skill_progress: list[SkillProgress]
    domain_breakdown: dict[str, dict[str, int]]
    milestones: list[MilestoneProgress]
    next_actions: list[NextAction]
    current_milestone: Optional[str] = None
    streak_note: str = ""
    pace: Optional[PaceReport] = None
    #: Every roadmap this learner is running, active one flagged.
    paths: list[PathSummary] = Field(default_factory=list)
    paths_completed: int = 0
    paths_total: int = 0


# --------------------------------------------------------------- skill graph
class SkillNode(BaseModel):
    id: str
    name: str
    domain: str
    level: int
    state: Literal["mastered", "in_progress", "planned"]
    #: dependency depth within this goal's subgraph — drives layout
    layer: int
    #: index within the layer
    slot: int
    is_target: bool = False
    taught_by: Optional[str] = None
    milestone: Optional[int] = None


class SkillEdge(BaseModel):
    source: str
    target: str
    #: true when the prerequisite is already satisfied
    satisfied: bool


class SkillGraph(BaseModel):
    goal_title: str
    nodes: list[SkillNode]
    edges: list[SkillEdge]
    layer_count: int
    widest_layer: int


# --------------------------------------------------------------- weekly plan
class PlannedItem(BaseModel):
    item_id: str
    title: str
    type: str
    hours_this_week: float
    total_hours: int
    portion: float
    continues: bool = False
    status: ItemStatus = "not_started"


class PlanWeek(BaseModel):
    week: int
    hours: float
    capacity: int
    milestone_title: str
    items: list[PlannedItem]
    focus: str


class WeeklyPlan(BaseModel):
    learner_id: str
    goal_title: str
    weekly_hours: int
    total_weeks: int
    weeks: list[PlanWeek]
    truncated: bool = False


# -------------------------------------------------------------- achievements
class Badge(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    earned: bool
    hint: str = ""


class Streak(BaseModel):
    """Consecutive days with at least one item completed.

    Counted from completion timestamps that already exist, so it costs no new
    storage and cannot disagree with the progress record.
    """

    #: Days in the run ending today or yesterday. Zero once a day is missed.
    current_days: int = 0
    #: The best run ever recorded, which a broken streak does not reset.
    best_days: int = 0
    #: Distinct days studied in the last seven.
    days_this_week: int = 0
    #: True when today already counts, so the UI can say "keep it going".
    active_today: bool = False


class Achievements(BaseModel):
    xp: int
    level: int
    level_title: str
    xp_into_level: int
    xp_for_next_level: int
    badges: list[Badge]
    earned_count: int
    total_count: int
    streak: Streak = Field(default_factory=Streak)
    #: Stages finished, and how many there are, for per-stage rewards.
    stages_completed: int = 0
    stages_total: int = 0


# ----------------------------------------------------------------- assistant
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    #: An unknown id creates a learner, so it must look like one of ours rather
    #: than arbitrary client-supplied text.
    learner_id: str = Field(pattern=LEARNER_ID_PATTERN)


class GoalInterpretation(BaseModel):
    """Structured reading of a learner's natural-language goal statement."""

    goal_id: Optional[str] = None
    goal_title: str = ""
    confidence: float = 0.0
    experience_level: Optional[ExperienceLevel] = None
    interests: list[str] = Field(default_factory=list)
    weekly_hours: Optional[int] = None
    declared_skills: list[str] = Field(default_factory=list)
    extra_target_skills: list[str] = Field(default_factory=list)
    preferred_formats: list[str] = Field(default_factory=list)
    #: "rules", or the name of the provider that answered ("gemini", "groq", …)
    source: str = "rules"
    notes: str = ""


class ChatResponse(BaseModel):
    reply: str
    interpretation: Optional[GoalInterpretation] = None
    profile: Optional[LearnerProfile] = None
    path_generated: bool = False
    #: "rules", or the name of the provider that answered
    source: str = "rules"
    suggested_replies: list[str] = Field(default_factory=list)
    #: True when the assistant asked which goal was meant instead of guessing.
    needs_clarification: bool = False
