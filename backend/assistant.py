"""Atlas — the conversational learning coach.

Two jobs: understand a goal stated in plain language, and answer questions about
the path the system generated.

Both run against whichever LLM provider is configured (see `llm.py`), and both
have a complete deterministic fallback. The fallback is not a stub — it is
written to sound like a coach rather than a form letter: it varies its phrasing,
names the learner, and cites their actual stage, hours, and progress. It is
still deterministic, because the variation is seeded from the input rather than
from randomness, so the same question always produces the same answer.
"""

from __future__ import annotations

import json
import logging
import re
import zlib
from typing import Any, Optional

from . import llm
from .catalog import Catalog, Goal
from .config import CHAT_MAX_TOKENS, EXTRACT_MAX_TOKENS
from .models import GoalInterpretation, LearnerProfile, LearningPath

log = logging.getLogger("assistant")

ASSISTANT_NAME = "Atlas"

_FORMAT_HINTS: dict[str, tuple[str, ...]] = {
    "video": ("video", "watch", "lecture", "youtube"),
    "interactive": ("interactive", "hands on", "hands-on", "practice", "exercises"),
    "reading": ("reading", "read", "book", "article", "documentation", "text"),
    "project": ("project", "build", "portfolio", "making", "building things"),
}

_LEVEL_HINTS: dict[str, tuple[str, ...]] = {
    "beginner": (
        "beginner", "new to", "never", "no experience", "starting out",
        "just started", "from scratch", "complete novice", "zero experience",
        "no background", "fresher",
    ),
    "advanced": (
        "advanced", "senior", "expert", "years of experience", "experienced",
        "professional", "lead ", "architect", "5 years", "several years",
    ),
    "intermediate": (
        "intermediate", "some experience", "a bit of", "familiar with",
        "comfortable with", "dabbled", "self taught", "self-taught",
    ),
}


def _seed(text: str) -> int:
    """Stable across processes — `hash()` on str is salted per interpreter."""
    return zlib.crc32(text.strip().lower().encode("utf-8"))


def _pick(options: list[str], seed_text: str, offset: int = 0) -> str:
    """Deterministic variety: same input, same phrasing; different input, different."""
    return options[(_seed(seed_text) + offset) % len(options)]


# =============================================================== the persona
PERSONA = f"""You are {ASSISTANT_NAME}, a learning coach inside a personalised \
learning-path product. You are talking to one learner about the roadmap this \
system has already generated for them.

Who you are: you have coached a lot of career changers. You are warm but you do \
not flatter. You have opinions and you share them. You would rather say one \
specific useful thing than five generic ones.

How you talk:
- Open with the answer. No "Great question!", no restating what they asked.
- Be specific to THIS learner. Name their actual courses, stage names, hours, \
and what they have already finished. A sentence that would read identically to \
a different learner is a wasted sentence.
- Use their name occasionally, not every message.
- Vary your sentence shapes. Do not start consecutive replies the same way.
- Prose by default. Use a list only when you are genuinely listing items.
- Two or three short paragraphs maximum. Stop when you are done.
- End with one concrete nudge or one real question — never a menu of options, \
never "let me know if you have any other questions".
- Acknowledge effort when they have made some, and be honest when a stretch \
ahead of them is genuinely hard. Do not manufacture enthusiasm.

Hard rules:
- Ground every claim in the profile and path given to you. Never invent a \
course, provider, hour count, or skill that is not listed.
- If something is not in the context, say so plainly and name what they could \
change (goal, weekly hours, level) to get it.
- When asked why an item was recommended, cite the prerequisite chain and the \
specific skill gap it closes. That reasoning is the product's whole value — \
make it visible.
"""


# ======================================================== goal understanding
_INTERPRETATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "experience_level": {"type": ["string", "null"]},
        "weekly_hours": {"type": ["integer", "null"]},
        "interests": {"type": "array", "items": {"type": "string"}},
        "declared_skills": {"type": "array", "items": {"type": "string"}},
        "extra_target_skills": {"type": "array", "items": {"type": "string"}},
        "preferred_formats": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": [
        "goal_id", "confidence", "experience_level", "weekly_hours",
        "interests", "declared_skills", "extra_target_skills",
        "preferred_formats", "notes",
    ],
    "additionalProperties": False,
}


def _interpretation_system(catalog: Catalog) -> str:
    goals = "\n".join(
        f"- {g.id}: {g.title} — {g.description}" for g in catalog.goals.values()
    )
    skills = "\n".join(f"- {s.id}: {s.name}" for s in catalog.skills.values())
    return (
        "You interpret a learner's natural-language description of what they "
        "want to learn, and map it onto a fixed catalog.\n\n"
        "Rules:\n"
        "- goal_id MUST be one of the listed goal ids, or null if none fits.\n"
        "- experience_level is one of beginner, intermediate, advanced, or null.\n"
        "- declared_skills are skills the learner says they ALREADY have.\n"
        "- extra_target_skills are catalog skills they explicitly want that the "
        "goal does not already imply. Use skill ids only.\n"
        "- preferred_formats from: video, interactive, reading, project.\n"
        "- Never invent ids. Omit anything you are unsure about.\n"
        "- confidence is 0.0-1.0 for the goal match.\n"
        "- weekly_hours only if the learner states available study time.\n"
        "- notes: one short sentence on anything notable about their situation "
        "(a career pivot, a deadline, a constraint) that a coach should remember.\n\n"
        f"GOALS:\n{goals}\n\nSKILLS:\n{skills}"
    )


def interpret_goal(catalog: Catalog, text: str) -> GoalInterpretation:
    """Map free text onto structured profile fields."""
    rules_result = _interpret_with_rules(catalog, text)

    raw, provider = llm.complete(
        system=_interpretation_system(catalog),
        messages=[{"role": "user", "content": text}],
        max_tokens=EXTRACT_MAX_TOKENS,
        schema=_INTERPRETATION_SCHEMA,
    )
    if raw is None or provider is None:
        return rules_result

    payload = llm.extract_json(raw)
    if payload is None:
        log.warning("%s returned non-JSON interpretation; using rules", provider)
        return rules_result
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("%s returned malformed JSON; using rules", provider)
        return rules_result
    if not isinstance(data, dict):
        return rules_result

    # The model is never trusted to return ids that exist or values in range.
    goal_id = data.get("goal_id")
    if goal_id not in catalog.goals:
        goal_id = rules_result.goal_id

    declared = [s for s in data.get("declared_skills") or [] if s in catalog.skills]
    extra = [s for s in data.get("extra_target_skills") or [] if s in catalog.skills]
    formats = [
        f
        for f in data.get("preferred_formats") or []
        if f in ("video", "interactive", "reading", "project")
    ]
    level = data.get("experience_level")
    if level not in ("beginner", "intermediate", "advanced"):
        level = rules_result.experience_level

    hours = data.get("weekly_hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 60:
        hours = rules_result.weekly_hours

    confidence = data.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0

    return GoalInterpretation(
        goal_id=goal_id,
        goal_title=catalog.goals[goal_id].title if goal_id else "",
        confidence=max(0.0, min(1.0, confidence)),
        experience_level=level,
        interests=[str(i) for i in (data.get("interests") or [])][:6]
        or rules_result.interests,
        weekly_hours=hours,
        declared_skills=declared or rules_result.declared_skills,
        extra_target_skills=extra,
        preferred_formats=formats or rules_result.preferred_formats,
        source=provider,
        notes=str(data.get("notes") or ""),
    )


def _score_goals(catalog: Catalog, text: str) -> list[tuple[Goal, float]]:
    """Every goal the wording points at, best first. Ties break on id."""
    lowered = f" {text.lower()} "
    scored: list[tuple[Goal, float]] = []
    for goal in catalog.goals.values():
        score = 0.0
        for keyword in goal.keywords:
            if keyword in lowered:
                score += 1.0 + len(keyword.split()) * 0.5
        if goal.title.lower() in lowered:
            score += 3.0
        if score > 0:
            scored.append((goal, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored


#: Below this score the best match is a guess rather than a reading.
CLARIFY_MIN_SCORE = 2.0
#: A runner-up this close to the leader means the wording fits both.
CLARIFY_CLOSE_RATIO = 0.7
#: Above this, a provider's own reading is trusted over the keyword rules.
CLARIFY_TRUST_CONFIDENCE = 0.6


def _spread_of_goals(catalog: Catalog, seed: list[Goal], limit: int = 4) -> list[Goal]:
    """A short, varied menu: one goal per domain, seeded with any near-matches."""
    chosen = list(seed)
    domains = {goal.domain for goal in chosen}
    for goal in sorted(catalog.goals.values(), key=lambda g: (g.domain, g.id)):
        if len(chosen) >= limit:
            break
        if goal.domain in domains or goal in chosen:
            continue
        chosen.append(goal)
        domains.add(goal.domain)
    return chosen[:limit]


def clarification(catalog: Catalog, text: str) -> tuple[str, list[str]] | None:
    """Ask instead of guessing when the wording does not name one goal.

    Returns the question and the replies that would resolve it, or `None` when
    the message points clearly enough at a single goal. Two situations qualify:
    nothing matched at all ("I want to work with data"), and two goals matched
    about equally well.
    """
    ranked = _score_goals(catalog, text)
    if ranked and ranked[0][1] >= CLARIFY_MIN_SCORE:
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if runner_up < ranked[0][1] * CLARIFY_CLOSE_RATIO:
            return None  # one clear reading — no need to ask

    near = [goal for goal, _ in ranked[:2]]
    options = _spread_of_goals(catalog, near)
    replies = [
        f"I want to become {'an' if goal.title[:1].upper() in 'AEIOU' else 'a'} "
        f"{goal.title}"
        for goal in options
    ]

    if len(near) >= 2:
        first, second = near[0].title, near[1].title
        question = (
            f"I can read that two ways — {first} and {second} share a lot of "
            "ground early on but diverge fast, and picking the wrong one costs "
            "you months. Which outcome are you actually after?"
        )
    elif near:
        question = (
            f"That points loosely at {near[0].title}, but I would rather build "
            "the right roadmap than a fast one. Which of these is closest to "
            "what you want to be able to do?"
        )
    else:
        question = (
            "Tell me the outcome you want rather than the subject — what do you "
            "want to be able to build or do when you are finished? If it helps, "
            "here are some common destinations:"
        )
    return question, replies


def _interpret_with_rules(catalog: Catalog, text: str) -> GoalInterpretation:
    """Keyword and pattern matching. Always available, never fails."""
    lowered = f" {text.lower()} "

    ranked = _score_goals(catalog, text)
    best_goal, best_score = (ranked[0] if ranked else (None, 0.0))
    confidence = 0.0 if best_goal is None else min(1.0, best_score / 6.0)

    level: Optional[str] = None
    for candidate, hints in _LEVEL_HINTS.items():
        if any(hint in lowered for hint in hints):
            level = candidate
            break

    hours: Optional[int] = None
    for pattern in (
        r"(\d+)\s*(?:\+)?\s*(?:hours?|hrs?)\s*(?:a|per|each|/)\s*week",
        r"(\d+)\s*(?:hours?|hrs?)\s*weekly",
        r"about\s+(\d+)\s*(?:hours?|hrs?)",
        r"(\d+)\s*(?:hours?|hrs?)",
    ):
        match = re.search(pattern, lowered)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 60:
                hours = value
                break
    if hours is None:
        if "full time" in lowered or "full-time" in lowered:
            hours = 35
        elif "weekend" in lowered:
            hours = 8

    declared: list[str] = []
    if any(
        phrase in lowered
        for phrase in (
            "i know", "i already know", "familiar with", "comfortable with",
            "experience with", "i can use", "i use", "worked with", "proficient",
            "i have used", "background in", "good at",
        )
    ):
        for skill in catalog.skills.values():
            if _skill_mentioned(skill.name, lowered):
                declared.append(skill.id)

    formats = [
        fmt
        for fmt, hints in _FORMAT_HINTS.items()
        if any(hint in lowered for hint in hints)
    ]

    interests: list[str] = []
    if best_goal is not None:
        interests.append(best_goal.domain)
    for domain in {s.domain for s in catalog.skills.values()}:
        if domain.lower() in lowered and domain not in interests:
            interests.append(domain)

    return GoalInterpretation(
        goal_id=best_goal.id if best_goal else None,
        goal_title=best_goal.title if best_goal else "",
        confidence=round(confidence, 2),
        experience_level=level,  # type: ignore[arg-type]
        interests=interests,
        weekly_hours=hours,
        declared_skills=sorted(set(declared)),
        extra_target_skills=[],
        preferred_formats=formats,
        source="rules",
        notes="Matched by keyword rules." if best_goal else "No goal matched.",
    )


_GENERIC_TAIL = {
    "programming", "fundamentals", "foundations", "administration",
    "engineering", "design", "management", "orchestration", "optimisation",
    "optimization", "essentials", "basics", "analysis", "governance",
    "modelling", "modeling", "validation", "reproducibility", "generation",
}

_SPLIT_PATTERN = re.compile(r"\s+&\s+|\s+with\s+|\s+and\s+|,\s*")


def _skill_candidates(skill_name: str) -> list[str]:
    """Phrases a learner might plausibly use for this skill."""
    name = skill_name.lower().strip()
    candidates = {name}
    for inner in re.findall(r"\(([^)]*)\)", name):
        candidates.add(inner.strip())
    stripped = re.sub(r"\s*\([^)]*\)", "", name).strip()
    candidates.add(stripped)

    for part in _SPLIT_PATTERN.split(stripped):
        part = part.strip()
        if not part:
            continue
        candidates.add(part)
        words = part.split()
        while len(words) > 1 and words[-1] in _GENERIC_TAIL:
            words = words[:-1]
            candidates.add(" ".join(words))
    return [c for c in candidates if len(c) >= 3]


def _skill_mentioned(skill_name: str, lowered_text: str) -> bool:
    for candidate in _skill_candidates(skill_name):
        if re.search(
            rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", lowered_text
        ):
            return True
    return False


# ===================================================================== context
def _progress_snapshot(path: Optional[LearningPath]) -> dict[str, Any]:
    if path is None:
        return {"done": 0, "total": 0, "next": None, "stage": None}
    done = total = 0
    nxt = stage = None
    for milestone in path.milestones:
        for item in milestone.items:
            total += 1
            if item.status == "completed":
                done += 1
            elif nxt is None:
                nxt, stage = item, milestone
    return {"done": done, "total": total, "next": nxt, "stage": stage}


def _context_block(
    catalog: Catalog, profile: LearnerProfile, path: Optional[LearningPath]
) -> str:
    lines = [
        "LEARNER",
        f"- name: {profile.name}",
        f"- their own words: {profile.goal_text or '(none given)'}",
        f"- experience: {profile.experience_level}",
        f"- study time: {profile.weekly_hours} hours/week",
        f"- interests: {', '.join(profile.interests) or '(none)'}",
        f"- preferred formats: "
        f"{', '.join(profile.preferred_formats) or '(no preference)'}",
        "- skills already held: "
        + (
            ", ".join(catalog.skill_name(s) for s in profile.declared_skills)
            or "(none recorded)"
        ),
    ]
    if profile.difficulty_offset:
        direction = "harder" if profile.difficulty_offset > 0 else "easier"
        lines.append(
            f"- they asked for {direction} material, so difficulty is adjusted"
        )
    if profile.excluded_item_ids:
        rejected = ", ".join(
            catalog.items[i].title
            for i in profile.excluded_item_ids
            if i in catalog.items
        )
        lines.append(f"- they rejected: {rejected} (never suggest these again)")

    if path is None:
        lines.append("\nNo learning path has been generated yet.")
        return "\n".join(lines)

    snap = _progress_snapshot(path)
    lines += [
        "",
        "PATH",
        f"- goal: {path.goal_title}",
        f"- {path.total_hours} hours over {path.total_weeks} weeks, "
        f"{len(path.milestones)} stages",
        f"- progress: {snap['done']} of {snap['total']} items complete",
        "- skills still missing: "
        + (", ".join(path.gap.missing_skill_names) or "(none)"),
    ]
    if snap["next"] is not None:
        lines.append(
            f"- their next item: {snap['next'].title} "
            f"(in {snap['stage'].title})"
        )
    lines.append("")
    lines.append("STAGES")
    for milestone in path.milestones:
        lines.append(f"{milestone.order}. {milestone.title} ({milestone.hours}h)")
        for item in milestone.items:
            lines.append(
                f"   - [{item.role}/{item.status}] {item.title} "
                f"({item.provider}, {item.hours}h, level {item.level}) — "
                f"{item.rationale}"
            )
    return "\n".join(lines)


# =================================================================== answering
def answer(
    catalog: Catalog,
    profile: LearnerProfile,
    path: Optional[LearningPath],
    question: str,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Answer a learner question. Returns (reply, source)."""
    messages: list[dict[str, str]] = []
    for turn in (history or [])[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    reply, provider = llm.complete(
        system=f"{PERSONA}\n\n{_context_block(catalog, profile, path)}",
        messages=messages,
        max_tokens=CHAT_MAX_TOKENS,
    )
    if reply and provider:
        return reply, provider
    return _answer_with_rules(catalog, profile, path, question), "rules"


def acknowledge_goal(
    catalog: Catalog,
    profile: LearnerProfile,
    path: LearningPath,
    interpretation: GoalInterpretation,
) -> tuple[str, str]:
    """The reply sent right after a goal is captured and a path built."""
    brief = (
        "The learner just described their goal and you have generated their "
        "roadmap. Write their welcome message. Reflect back what you understood "
        "about THEM (not a list of fields), tell them what the shape of the "
        "journey is and roughly how long it takes, call out the one thing that "
        "will surprise them about the ordering, and point them at their very "
        "first item by name. Do not enumerate every stage — they can see those."
    )
    reply, provider = llm.complete(
        system=f"{PERSONA}\n\n{_context_block(catalog, profile, path)}",
        messages=[{"role": "user", "content": brief}],
        max_tokens=CHAT_MAX_TOKENS,
    )
    if reply and provider:
        return reply, provider
    return _acknowledge_with_rules(catalog, profile, path, interpretation), "rules"


def _acknowledge_with_rules(
    catalog: Catalog,
    profile: LearnerProfile,
    path: LearningPath,
    interpretation: GoalInterpretation,
) -> str:
    seed = profile.goal_text or path.goal_title
    known = [catalog.skill_name(s) for s in profile.declared_skills]
    first = path.milestones[0].items[0] if path.milestones else None

    opener = _pick(
        [
            f"Right — {path.goal_title}. Here's how I'd get you there.",
            f"Got it. {path.goal_title} it is.",
            f"{path.goal_title}. That's a good target, and it's reachable from "
            "where you are.",
            f"Understood — we're aiming at {path.goal_title}.",
        ],
        seed,
    )

    if known:
        start = _pick(
            [
                f"You're not starting from zero: {', '.join(known[:3])} already "
                "counts, and everything those depend on comes free.",
                f"I've credited you with {', '.join(known[:3])}, plus their "
                "prerequisites — no point relearning them.",
                f"Starting position: {', '.join(known[:3])}. That trims real "
                "time off the front of this.",
            ],
            seed,
            offset=1,
        )
    else:
        start = _pick(
            [
                "You're starting fresh, which is cleaner than it sounds — "
                "nothing to unlearn.",
                "No prior skills recorded, so we build from the foundations up.",
                "Starting from scratch. That's fine; the ordering matters more "
                "than the starting point.",
            ],
            seed,
            offset=1,
        )

    shape = (
        f"The roadmap is {len(path.milestones)} stages and {path.total_hours} "
        f"hours — about {path.total_weeks} weeks at {profile.weekly_hours} hours "
        f"a week. {path.gap.coverage_pct}% of what this goal needs, you already "
        "have."
    )

    surprise = ""
    if path.gap.bridging_skills:
        bridge = catalog.skill_name(path.gap.bridging_skills[0])
        surprise = _pick(
            [
                f"One thing that will look odd: {bridge} shows up even though "
                "it isn't part of the job title. It's there because something "
                "later genuinely depends on it.",
                f"Expect at least one detour — {bridge} — that doesn't look "
                "related until you see what sits on top of it.",
                f"If you're wondering why {bridge} appears: a later skill "
                "requires it, so it has to come first.",
            ],
            seed,
            offset=2,
        )

    nudge = (
        f"Start with “{first.title}” — {first.hours} hours. "
        if first
        else "Open the Path tab to see the stages. "
    )
    nudge += _pick(
        [
            "Ask me why anything is where it is and I'll show you the chain.",
            "If a stage looks wrong for you, tell me and I'll rebuild around it.",
            "Anything on there feel too easy or irrelevant? Say so and the path "
            "adapts.",
        ],
        seed,
        offset=3,
    )

    parts = [f"{opener} {start}", shape]
    if surprise:
        parts.append(surprise)
    parts.append(nudge)
    return "\n\n".join(parts)


def _answer_with_rules(
    catalog: Catalog,
    profile: LearnerProfile,
    path: Optional[LearningPath],
    question: str,
) -> str:
    """Deterministic answers, written to sound like a person."""
    q = question.lower().strip()
    name = profile.name if profile.name and profile.name != "Learner" else ""

    if path is None:
        return _pick(
            [
                "I don't have a learning path for you yet — tell me what you're "
                "aiming at. Something like “I want to become a data analyst, I "
                "know a bit of SQL, and I've got 8 hours a week” gives me "
                "everything I need.",
                "Nothing to work from yet: I don't have a learning path for you. "
                "Describe the job or the thing you want to build, plus anything "
                "you already know and how much time you have, and I'll draft one.",
            ],
            question,
        )

    snap = _progress_snapshot(path)

    # A question about a specific item, matched by name.
    for milestone in path.milestones:
        for item in milestone.items:
            title = item.title.lower()
            if title in q or (len(title) > 12 and title[:18] in q):
                teaches = (
                    ", ".join(item.teaches_names)
                    if item.teaches_names
                    else "nothing new — it's there to apply and prove what you "
                    "already covered"
                )
                return (
                    f"“{item.title}” sits in {milestone.title}. {item.rationale}\n\n"
                    f"It's {item.hours} hours of {item.format} content from "
                    f"{item.provider}, pitched at level {item.level}. It teaches "
                    f"{teaches}. If that's not landing for you, mark it too easy "
                    "or not relevant and I'll rework the stage around it."
                )

    if any(k in q for k in ("how long", "how many weeks", "duration", "finish", "time")):
        remaining = path.total_hours - sum(
            i.hours
            for m in path.milestones
            for i in m.items
            if i.status == "completed"
        )
        lead = _pick(
            [
                f"About {path.total_weeks} weeks.",
                f"Roughly {path.total_weeks} weeks, end to end.",
                f"{path.total_weeks} weeks at your current pace.",
            ],
            question,
        )
        body = (
            f" That's {path.total_hours} hours of material at "
            f"{path.weekly_hours} hours a week, across {len(path.milestones)} "
            "stages."
        )
        if snap["done"]:
            body += (
                f" You've already cleared {snap['done']} item(s), so {remaining} "
                "hours actually remain."
            )
        tail = _pick(
            [
                " Bump your weekly hours in the Profile tab and that number moves "
                "immediately.",
                " If that's longer than you want, raising your weekly hours is "
                "the fastest lever.",
                " The estimate is honest, not padded — it assumes you actually "
                "do the projects.",
            ],
            question,
            offset=1,
        )
        return lead + body + tail

    if any(k in q for k in ("what next", "next", "start", "begin", "first")):
        nxt = snap["next"]
        if nxt is None:
            return (
                "Nothing left — you've completed every item on this path. "
                "That's the whole goal done. Set a new one and I'll build the "
                "next roadmap."
            )
        opener = _pick(
            [
                f"Next up: “{nxt.title}”.",
                f"Start with “{nxt.title}”.",
                f"“{nxt.title}” is what I'd open next.",
            ],
            question,
        )
        prefix = f"{name}, " if name else ""
        return (
            f"{prefix}{opener} It's {nxt.hours} hours from {nxt.provider}, in "
            f"{snap['stage'].title}.\n\n{nxt.rationale}"
        )

    if any(k in q for k in ("missing", "gap", "don't know", "do i need", "what skills")):
        missing = path.gap.missing_skill_names
        if not missing:
            return (
                "Nothing — you already hold every skill this goal requires. "
                "What's left on your path is the proof: a project and an "
                "assessment to show it."
            )

        head = ", ".join(missing[:5])
        more = f", plus {len(missing) - 5} more" if len(missing) > 5 else ""
        bridging = len(path.gap.bridging_skills)
        return (
            f"{len(missing)} skills stand between you and {path.goal_title}: "
            f"{head}{more}. You're at {path.gap.coverage_pct}% coverage.\n\n"
            + (
                f"{bridging} of those aren't part of the role itself — they're "
                "prerequisites other skills depend on, which is exactly why "
                "they're scheduled first rather than last."
                if bridging
                else "They're scheduled in dependency order, so each one is "
                "learnable by the time you reach it."
            )
        )

    if "why" in q:
        stage = path.milestones[0].title if path.milestones else "the first stage"
        return (
            f"The short version: nothing is ordered by popularity, it's ordered "
            f"by dependency. {stage} comes first because everything after it "
            "needs those skills to make sense.\n\n"
            "Every item on your path carries its own reason — the specific gap "
            "it closes and the prerequisites that pin it in place. Name any one "
            "of them and I'll walk you through that chain."
        )

    if any(k in q for k in ("harder", "easier", "too easy", "too hard", "change")):
        return (
            "Tell me directly and I'll act on it. Every item has feedback "
            "buttons — too easy, too hard, not relevant.\n\n"
            "Too easy marks that skill as already mastered and lifts the "
            "difficulty of everything downstream. Too hard pulls it down and "
            "favours more foundational material. Not relevant drops the item and "
            "finds another route to the same skill. Each one rebuilds the path."
        )

    if any(k in q for k in ("project", "portfolio", "build", "hands on", "hands-on")):
        projects = [
            item.title
            for m in path.milestones
            for item in m.items
            if item.role == "project"
        ]
        if not projects:
            return (
                "No projects scheduled on this path yet — that usually means the "
                "catalog has no project matching the skills in your stages. Add "
                "more target skills and I'll look again."
            )
        listed = "; ".join(projects)
        return (
            f"You've got {len(projects)} build(s) on this path: {listed}.\n\n"
            "They're placed deliberately — each one lands right after the stage "
            "that teaches what it needs, so you're applying material while it's "
            "still fresh rather than at the very end."
        )

    if any(k in q for k in ("progress", "how am i", "doing", "far")):
        if snap["total"] == 0:
            return "There's nothing scheduled yet, so there's no progress to report."
        pct = round(100 * snap["done"] / snap["total"])
        mood = (
            "You haven't started yet — the first item is the only hard one to open."
            if snap["done"] == 0
            else f"That's {pct}% of the path."
        )
        nxt = snap["next"]
        tail = f" Next is “{nxt.title}”." if nxt else " Nothing left to do."
        return f"{snap['done']} of {snap['total']} items done. {mood}{tail}"

    # Genuine fallback — still specific to this learner.
    prefix = f"{name}, here's " if name else "Here's "
    nxt = snap["next"]
    tail = (
        f" Right now the next thing is “{nxt.title}”."
        if nxt
        else " You've finished everything on it."
    )
    return (
        f"{prefix}where you stand: {path.goal_title}, {len(path.milestones)} "
        f"stages, {snap['done']} of {snap['total']} items done.{tail}\n\n"
        "Ask me why a particular item is where it is, what to start next, how "
        "long this takes, or what skills you're still missing — I can answer any "
        "of those against your actual path."
    )


# ================================================================= suggestions
def suggested_replies(
    catalog: Optional[Catalog] = None,
    path: Optional[LearningPath] = None,
    profile: Optional[LearnerProfile] = None,
) -> list[str]:
    """Follow-ups worth clicking, chosen for the learner's current state."""
    if path is None:
        return [
            "I want to become a data analyst",
            "Help me move into AI engineering",
            "I know some JavaScript and want a frontend job",
            "I'm a senior engineer moving into cloud architecture",
        ]

    snap = _progress_snapshot(path)
    options: list[str] = []
    if snap["next"] is not None:
        options.append(f"Why is “{snap['next'].title}” first?")
    options.append("What should I start next?")
    if path.gap.bridging_skills and catalog is not None:
        # Ask using the skill's real name, never its internal id.
        options.append(
            f"Why do I need {catalog.skill_name(path.gap.bridging_skills[0])}?"
        )
    options.append("How long will this take?")
    options.append("How am I doing?" if snap["done"] else "What am I still missing?")
    return options[:4]
