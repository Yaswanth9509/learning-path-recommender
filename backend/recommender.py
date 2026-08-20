"""Recommendation engine.

Every recommendation is a weighted sum of six named, independently computable
components. Nothing is a black box: the same numbers that produce the ranking
are returned to the client and drive the human-readable reasons, so "why was
this recommended?" always has a precise answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog, LearningItem
from .models import Recommendation, ScoreBreakdown
from .profiling import DerivedProfile

# Weights sum to 1.0. Goal relevance dominates because a learning path that is
# pleasant but off-target is worse than useless.
# There was a sixth component, `quality`, worth 0.05 and computed from a
# rating and a learner count that were both invented. Once every provider
# became a real one those numbers stopped being harmless flavour and started
# lending false precision to MIT and OpenStax, so they are gone and the
# remaining five carry their share.
WEIGHTS: dict[str, float] = {
    "goal_relevance": 0.37,
    "skill_readiness": 0.21,
    "level_fit": 0.16,
    "interest_match": 0.16,
    "format_fit": 0.10,
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "recommendation weights must sum to 1"


@dataclass(frozen=True)
class ScoringContext:
    """Everything scoring needs, resolved once per request."""

    catalog: Catalog
    derived: DerivedProfile
    #: skills still to be learned for the current goal
    gap_skills: frozenset[str]
    #: every skill the goal requires, gap or not
    required_skills: frozenset[str]

    @property
    def known(self) -> frozenset[str]:
        return self.derived.known_skills


def build_context(
    catalog: Catalog, derived: DerivedProfile, gap_skills: list[str],
    required_skills: set[str] | None = None,
) -> ScoringContext:
    gaps = frozenset(gap_skills)
    return ScoringContext(
        catalog=catalog,
        derived=derived,
        gap_skills=gaps,
        required_skills=frozenset(required_skills or (gaps | set(derived.known_skills))),
    )


# ------------------------------------------------------------------ components
def _goal_relevance(ctx: ScoringContext, item: LearningItem) -> float:
    """How much of this item's value lands on skills the learner still needs."""
    if item.type in ("course", "resource"):
        # A resource covers material the same way a course does; it is simply
        # never scheduled, so it is judged on the same basis.
        subject = set(item.teaches)
        if not subject:
            return 0.0
        hits = subject & ctx.gap_skills
        # Courses that teach only things already known score zero.
        return len(hits) / len(subject)

    # Projects and assessments do not teach new skills; their value is in
    # exercising skills the goal actually requires.
    subject = set(item.requires)
    if not subject:
        return 0.0
    return len(subject & ctx.required_skills) / len(subject)


def _skill_readiness(ctx: ScoringContext, item: LearningItem) -> float:
    """Can the learner start this today?"""
    if not item.requires:
        return 1.0
    met = sum(1 for s in item.requires if s in ctx.known)
    return met / len(item.requires)


def _level_fit(ctx: ScoringContext, item: LearningItem) -> float:
    """Closeness to the learner's adapted difficulty target."""
    distance = abs(item.level - ctx.derived.profile.target_level)
    return max(0.0, 1.0 - distance / 2.0)


def _interest_match(ctx: ScoringContext, item: LearningItem) -> float:
    domains = ctx.derived.interest_domains
    if not domains:
        return 0.5  # neutral when we know nothing about their interests
    if item.domain in domains:
        return 1.0
    # Foundations serve every domain, so never penalise them fully.
    return 0.6 if item.domain == "Foundations" else 0.2


def _format_fit(ctx: ScoringContext, item: LearningItem) -> float:
    profile = ctx.derived.profile
    score = 0.5
    if profile.preferred_formats:
        score = 1.0 if item.format in profile.preferred_formats else 0.25
    if profile.format_affinity.get(item.format):
        score = min(1.0, score + 0.25)
    if profile.cost_preference == "free":
        score = score if item.cost == "free" else score * 0.4
    if profile.provider_affinity.get(item.provider):
        score = min(1.0, score + 0.15)
    return score




_COMPONENTS = {
    "goal_relevance": _goal_relevance,
    "skill_readiness": _skill_readiness,
    "level_fit": _level_fit,
    "interest_match": _interest_match,
    "format_fit": _format_fit,
}


# ---------------------------------------------------------------------- scoring
def score_item(ctx: ScoringContext, item: LearningItem) -> tuple[float, ScoreBreakdown]:
    values = {name: fn(ctx, item) for name, fn in _COMPONENTS.items()}
    total = sum(WEIGHTS[name] * value for name, value in values.items())
    return round(total, 4), ScoreBreakdown(**{k: round(v, 3) for k, v in values.items()})


def build_reasons(
    ctx: ScoringContext, item: LearningItem, breakdown: ScoreBreakdown
) -> list[str]:
    """Turn the score components into sentences a learner can act on."""
    catalog = ctx.catalog
    profile = ctx.derived.profile
    reasons: list[str] = []

    closes = sorted(set(item.teaches) & ctx.gap_skills)
    if closes:
        names = ", ".join(catalog.skill_name(s) for s in closes)
        reasons.append(f"Closes {len(closes)} skill gap(s) on your path: {names}.")
    elif item.type in ("project", "assessment"):
        exercised = sorted(set(item.requires) & ctx.required_skills)
        if exercised:
            names = ", ".join(catalog.skill_name(s) for s in exercised)
            verb = "Proves" if item.type == "assessment" else "Puts into practice"
            reasons.append(f"{verb} {names} — skills your goal is measured on.")
    if item.type == "resource":
        reasons.append(
            "Reference material, not a scheduled step — open it alongside the "
            "course that covers the same skill."
        )

    missing = [s for s in item.requires if s not in ctx.known]
    if not missing:
        reasons.append("You already meet every prerequisite, so you can start now.")
    else:
        names = ", ".join(catalog.skill_name(s) for s in missing)
        reasons.append(f"Scheduled after you cover its prerequisites: {names}.")

    if breakdown.level_fit >= 0.99:
        reasons.append(
            f"Difficulty matches your {profile.experience_level} level"
            + (
                " as adjusted by your feedback."
                if profile.difficulty_offset
                else "."
            )
        )
    elif breakdown.level_fit < 0.5:
        reasons.append(
            "A stretch relative to your current level — expect to work harder here."
        )

    if breakdown.interest_match >= 0.99:
        reasons.append(f"Matches your stated interest in {item.domain}.")

    if profile.preferred_formats and item.format in profile.preferred_formats:
        reasons.append(f"Delivered as {item.format} content, which you prefer.")
    if profile.cost_preference == "free" and item.cost == "free":
        reasons.append("Free, matching your cost preference.")
    if profile.provider_affinity.get(item.provider):
        reasons.append(f"From {item.provider}, a provider you rated highly before.")

    reasons.append(f"About {item.hours} hours of work, from {item.provider}.")
    return reasons


def to_recommendation(
    ctx: ScoringContext, item: LearningItem
) -> Recommendation:
    catalog = ctx.catalog
    score, breakdown = score_item(ctx, item)
    missing = [s for s in item.requires if s not in ctx.known]
    return Recommendation(
        item_id=item.id,
        title=item.title,
        provider=item.provider,
        type=item.type,
        level=item.level,
        hours=item.hours,
        format=item.format,
        cost=item.cost,
        domain=item.domain,
        description=item.description,
        url=item.url,
        teaches=list(item.teaches),
        teaches_names=[catalog.skill_name(s) for s in item.teaches],
        requires=list(item.requires),
        requires_names=[catalog.skill_name(s) for s in item.requires],
        score=score,
        breakdown=breakdown,
        reasons=build_reasons(ctx, item, breakdown),
        closes_gap_skills=sorted(set(item.teaches) & ctx.gap_skills),
        prerequisites_met=not missing,
        missing_prerequisites=[catalog.skill_name(s) for s in missing],
    )


def recommend(
    ctx: ScoringContext,
    limit: int = 10,
    item_types: set[str] | None = None,
    require_ready: bool = False,
    exclude: set[str] | None = None,
) -> list[Recommendation]:
    """Rank the catalog for this learner.

    `require_ready` restricts results to items the learner can start immediately.
    """
    profile = ctx.derived.profile
    blocked = set(exclude or set()) | set(profile.excluded_item_ids)
    blocked |= set(profile.completed_item_ids)

    results: list[Recommendation] = []
    for item in ctx.catalog.items.values():
        if item.id in blocked:
            continue
        if item_types and item.type not in item_types:
            continue
        rec = to_recommendation(ctx, item)
        if require_ready and not rec.prerequisites_met:
            continue
        if rec.score <= 0:
            continue
        results.append(rec)

    # Deterministic ordering: score, then shorter work, then id — so equal-scoring
    # items never shuffle between requests.
    results.sort(key=lambda r: (-r.score, r.hours, r.item_id))
    return results[:limit]


def best_course_for_skill(
    ctx: ScoringContext, skill_id: str, exclude: set[str] | None = None
) -> LearningItem | None:
    """Pick the single best course teaching `skill_id`.

    Prefers courses that also cover other outstanding gap skills, so a learner
    never takes two courses where one would have done.
    """
    blocked = set(exclude or set())
    blocked |= set(ctx.derived.profile.excluded_item_ids)
    blocked |= set(ctx.derived.profile.completed_item_ids)

    candidates = [
        ctx.catalog.items[item_id]
        for item_id in ctx.catalog.taught_by.get(skill_id, [])
        if item_id not in blocked and ctx.catalog.items[item_id].type == "course"
    ]
    if not candidates:
        return None

    def rank(item: LearningItem) -> tuple:
        extra_coverage = len(set(item.teaches) & ctx.gap_skills)
        score, _ = score_item(ctx, item)
        return (-extra_coverage, -score, item.hours, item.id)

    candidates.sort(key=rank)
    return candidates[0]
