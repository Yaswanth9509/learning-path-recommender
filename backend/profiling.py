"""Learner profiling engine.

Turns raw profile facts (completed courses, self-declared skills, stated
experience) into the derived view the recommender and path generator consume,
and folds learner feedback back into the profile so later recommendations adapt.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .models import FeedbackEntry, LearnerProfile


@dataclass(frozen=True)
class DerivedProfile:
    """The profile as the engines see it."""

    profile: LearnerProfile
    #: skills stated directly or earned by finishing a course
    direct_skills: frozenset[str]
    #: skills assumed known because they are prerequisites of a direct skill
    implied_skills: frozenset[str]
    interest_domains: frozenset[str]

    @property
    def known_skills(self) -> frozenset[str]:
        return self.direct_skills | self.implied_skills


# Interest keywords are matched against catalog domains so free-text interests
# ("I like building web apps") still steer recommendations.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Data & AI": (
        "data", "ai", "machine learning", "ml", "analytics", "statistics",
        "deep learning", "nlp", "llm", "model", "science", "genai",
    ),
    "Web Development": (
        "web", "frontend", "front end", "front-end", "backend", "back end",
        "react", "javascript", "ui", "ux", "full stack", "fullstack", "app",
        "interface", "css",
    ),
    "Cloud & DevOps": (
        "cloud", "devops", "infrastructure", "kubernetes", "docker", "aws",
        "azure", "gcp", "platform", "sre", "reliability", "deployment",
        "networking", "security",
    ),
    "Foundations": (
        "programming", "coding", "python", "sql", "algorithms", "basics",
        "fundamentals", "computer science",
    ),
}


def derive(catalog: Catalog, profile: LearnerProfile) -> DerivedProfile:
    """Expand a stored profile into known-skill sets and interest domains."""
    direct: set[str] = set()

    for skill_id in profile.declared_skills:
        if skill_id in catalog.skills:
            direct.add(skill_id)

    for item_id in profile.completed_item_ids:
        item = catalog.items.get(item_id)
        if item is None:
            continue
        direct.update(s for s in item.teaches if s in catalog.skills)
        # Finishing a project or assessment demonstrates the skills it exercises.
        if item.type in ("project", "assessment"):
            direct.update(s for s in item.requires if s in catalog.skills)

    # Anything a known skill depends on is implied. Someone who can train a
    # neural network does not need to be re-taught Python syntax.
    implied = catalog.prerequisite_closure(direct) - direct

    return DerivedProfile(
        profile=profile,
        direct_skills=frozenset(direct),
        implied_skills=frozenset(implied),
        interest_domains=frozenset(_interest_domains(catalog, profile)),
    )


def _interest_domains(catalog: Catalog, profile: LearnerProfile) -> set[str]:
    domains: set[str] = set()
    known_domains = {s.domain for s in catalog.skills.values()}
    haystacks = [i.lower() for i in profile.interests]
    if profile.goal_text:
        haystacks.append(profile.goal_text.lower())
    if profile.goal_id and profile.goal_id in catalog.goals:
        domains.add(catalog.goals[profile.goal_id].domain)

    for text in haystacks:
        for domain in known_domains:
            if domain.lower() in text:
                domains.add(domain)
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                domains.add(domain)
    return domains


# --------------------------------------------------------------- adaptation
#: Human-readable description of what each feedback signal does to the profile.
FEEDBACK_EFFECTS: dict[str, str] = {
    "too_easy": "raised the difficulty of upcoming recommendations",
    "too_hard": "lowered the difficulty and favoured more foundational material",
    "not_relevant": "removed that item and will not suggest it again",
    "liked": "favoured similar providers and formats",
}


def apply_feedback(
    catalog: Catalog, profile: LearnerProfile, entry: FeedbackEntry
) -> tuple[LearnerProfile, str]:
    """Fold one feedback signal into the profile.

    Returns the updated profile and a sentence explaining what changed, so the
    UI can tell the learner exactly how their input was used.
    """
    item = catalog.items.get(entry.item_id)
    if item is None:
        return profile, "That item is not in the catalog, so nothing changed."

    note = ""
    if entry.signal == "too_easy":
        profile.difficulty_offset = min(1, profile.difficulty_offset + 1)
        # Material that was too easy is material already mastered.
        newly_known = [s for s in item.teaches if s not in profile.declared_skills]
        profile.declared_skills = sorted(set(profile.declared_skills) | set(item.teaches))
        if entry.item_id not in profile.excluded_item_ids:
            profile.excluded_item_ids.append(entry.item_id)
        learned = ", ".join(catalog.skill_name(s) for s in newly_known)
        note = (
            f"Marked {learned or item.title} as already known and "
            "raised the difficulty of the rest of your path."
        )

    elif entry.signal == "too_hard":
        profile.difficulty_offset = max(-1, profile.difficulty_offset - 1)
        note = (
            "Lowered the difficulty target and will favour more foundational "
            "material before returning to this topic."
        )

    elif entry.signal == "not_relevant":
        if entry.item_id not in profile.excluded_item_ids:
            profile.excluded_item_ids.append(entry.item_id)
        note = f"Removed “{item.title}” and will look for an alternative."

    elif entry.signal == "liked":
        profile.provider_affinity[item.provider] = (
            profile.provider_affinity.get(item.provider, 0) + 1
        )
        profile.format_affinity[item.format] = (
            profile.format_affinity.get(item.format, 0) + 1
        )
        note = (
            f"Will favour more {item.format} content, especially from "
            f"{item.provider}."
        )

    return profile, note


def sync_completions(
    catalog: Catalog,
    profile: LearnerProfile,
    completed_item_ids: list[str],
    tracked_item_ids: list[str] | None = None,
) -> LearnerProfile:
    """Keep `profile.completed_item_ids` in step with the progress table.

    Two things claim that field. The progress table owns any item it tracks —
    unticking a course there must un-complete it here. Everything else is prior
    learning history the learner declared before they ever had a path, and that
    must survive a rebuild rather than being silently erased.
    """
    tracked = set(tracked_item_ids if tracked_item_ids is not None else completed_item_ids)
    declared = {
        i for i in profile.completed_item_ids if i in catalog.items and i not in tracked
    }
    from_progress = {i for i in completed_item_ids if i in catalog.items}
    profile.completed_item_ids = sorted(declared | from_progress)
    return profile


def summarize(catalog: Catalog, derived: DerivedProfile) -> str:
    """One-paragraph plain-language description of the learner."""
    profile = derived.profile
    known = len(derived.known_skills)
    goal = (
        catalog.goals[profile.goal_id].title
        if profile.goal_id and profile.goal_id in catalog.goals
        else "an unset goal"
    )
    interests = ", ".join(sorted(derived.interest_domains)) or "no stated interests"
    return (
        f"{profile.name} is working toward {goal} at a {profile.experience_level} "
        f"level, with about {profile.weekly_hours} hours a week to study. "
        f"They already hold {known} catalog skills "
        f"({len(derived.direct_skills)} demonstrated, "
        f"{len(derived.implied_skills)} implied by prerequisites) and are "
        f"interested in {interests}."
    )
