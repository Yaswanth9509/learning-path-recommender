"""Catalog loading, validation, and graph queries.

The catalog is the single source of truth for skills, learning items, and career
goals. It is validated exhaustively at import time: any dangling reference,
prerequisite cycle, or uncoverable skill raises `CatalogError` immediately rather
than surfacing as a confusing failure deep inside path generation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

#: `resource` is reference material — documentation, a specification, a
#: reference guide. It supports a skill but is never scheduled into a path:
#: coverage is only ever satisfied by a course (see `_select_courses`).
ITEM_TYPES = {"course", "project", "assessment", "resource"}

#: What a goal is for. Everything was implicitly a job before subjects and
#: exams existed in the catalogue.
GOAL_KINDS = {"job", "subject", "exam", "certification"}

#: How each kind reads in a sentence: "your path to <outcome>".
GOAL_OUTCOMES = {
    "job": "working as",
    "subject": "learning",
    "exam": "sitting",
    "certification": "certifying in",
}
FORMATS = {"video", "interactive", "reading", "project"}
COSTS = {"free", "paid"}


class CatalogError(RuntimeError):
    """Raised when the catalog data is internally inconsistent."""


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    domain: str
    level: int
    prerequisites: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class LearningItem:
    id: str
    title: str
    provider: str
    type: str
    level: int
    hours: int
    format: str
    cost: str
    domain: str
    teaches: tuple[str, ...]
    requires: tuple[str, ...]
    description: str
    #: Canonical link, when one exists. Empty for the sample catalogue's
    #: fictional providers — the UI offers a search instead of a dead link.
    url: str = ""

    @property
    def is_course(self) -> bool:
        return self.type == "course"


@dataclass(frozen=True)
class Goal:
    id: str
    title: str
    domain: str
    description: str
    typical_roles: tuple[str, ...]
    keywords: tuple[str, ...]
    target_skills: tuple[str, ...]
    #: What finishing this path actually means. A roadmap toward an exam is not
    #: a roadmap toward a job, and saying "become a GATE" would be nonsense, so
    #: the wording downstream keys off this.
    kind: str = "job"

    @property
    def outcome(self) -> str:
        """How to refer to the finish line in a sentence."""
        return GOAL_OUTCOMES.get(self.kind, GOAL_OUTCOMES["job"])


@dataclass
class Catalog:
    skills: dict[str, Skill]
    items: dict[str, LearningItem]
    goals: dict[str, Goal]
    #: skill id -> ids of items that teach it
    taught_by: dict[str, list[str]] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def skill(self, skill_id: str) -> Skill:
        try:
            return self.skills[skill_id]
        except KeyError:
            raise CatalogError(f"unknown skill '{skill_id}'") from None

    def item(self, item_id: str) -> LearningItem:
        try:
            return self.items[item_id]
        except KeyError:
            raise CatalogError(f"unknown learning item '{item_id}'") from None

    def goal(self, goal_id: str) -> Goal:
        try:
            return self.goals[goal_id]
        except KeyError:
            raise CatalogError(f"unknown goal '{goal_id}'") from None

    def skill_name(self, skill_id: str) -> str:
        return self.skills[skill_id].name if skill_id in self.skills else skill_id

    def items_of_type(self, item_type: str) -> list[LearningItem]:
        return [it for it in self.items.values() if it.type == item_type]

    # ------------------------------------------------------------ graph walks
    def prerequisite_closure(self, skill_ids: Iterable[str]) -> set[str]:
        """Every skill required to reach `skill_ids`, including the targets."""
        seen: set[str] = set()
        stack = list(skill_ids)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.skill(current).prerequisites)
        return seen

    def topological_layers(self, skill_ids: Iterable[str]) -> list[list[str]]:
        """Group skills into dependency layers within the given subgraph.

        Layer 0 has no unmet prerequisites inside the subgraph; each later layer
        depends only on earlier ones. Ordering inside a layer is deterministic
        (level, then name) so identical inputs always produce identical paths.
        """
        remaining = set(skill_ids)
        for sid in remaining:
            self.skill(sid)  # validate membership

        layers: list[list[str]] = []
        placed: set[str] = set()
        while remaining:
            ready = [
                sid
                for sid in remaining
                if all(p in placed or p not in remaining for p in self.skill(sid).prerequisites)
            ]
            if not ready:
                # Unreachable: validation rejects cycles at load time.
                raise CatalogError(
                    f"cycle detected among skills: {sorted(remaining)}"
                )
            ready.sort(key=lambda s: (self.skill(s).level, self.skill(s).name))
            layers.append(ready)
            placed.update(ready)
            remaining -= set(ready)
        return layers

    def depth(self, skill_id: str) -> int:
        """Longest prerequisite chain leading to this skill."""
        return _depth(self, skill_id, {})


def _depth(catalog: Catalog, skill_id: str, memo: dict[str, int]) -> int:
    if skill_id in memo:
        return memo[skill_id]
    prereqs = catalog.skill(skill_id).prerequisites
    value = 0 if not prereqs else 1 + max(_depth(catalog, p, memo) for p in prereqs)
    memo[skill_id] = value
    return value


# --------------------------------------------------------------------- loading
def _read(name: str) -> list[dict]:
    path = DATA_DIR / name
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise CatalogError(f"catalog file missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog file {path.name} is not valid JSON: {exc}") from None
    if not isinstance(data, list):
        raise CatalogError(f"catalog file {path.name} must contain a JSON array")
    return data


def _require_unique(rows: list[dict], label: str) -> None:
    seen: set[str] = set()
    for row in rows:
        identifier = row.get("id")
        if not identifier:
            raise CatalogError(f"{label} entry missing 'id': {row}")
        if identifier in seen:
            raise CatalogError(f"duplicate {label} id '{identifier}'")
        seen.add(identifier)


def load_catalog() -> Catalog:
    skill_rows = _read("skills.json")
    item_rows = _read("courses.json")
    goal_rows = _read("goals.json")

    _require_unique(skill_rows, "skill")
    _require_unique(item_rows, "learning item")
    _require_unique(goal_rows, "goal")

    skills = {
        row["id"]: Skill(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            level=int(row["level"]),
            prerequisites=tuple(row.get("prerequisites", [])),
            description=row["description"],
        )
        for row in skill_rows
    }

    items = {
        row["id"]: LearningItem(
            id=row["id"],
            title=row["title"],
            provider=row["provider"],
            type=row["type"],
            level=int(row["level"]),
            hours=int(row["hours"]),
            format=row["format"],
            cost=row["cost"],
            domain=row["domain"],
            teaches=tuple(row.get("teaches", [])),
            requires=tuple(row.get("requires", [])),
            description=row["description"],
            url=row.get("url", ""),
        )
        for row in item_rows
    }

    goals = {
        row["id"]: Goal(
            id=row["id"],
            title=row["title"],
            domain=row["domain"],
            description=row["description"],
            typical_roles=tuple(row.get("typical_roles", [])),
            keywords=tuple(k.lower() for k in row.get("keywords", [])),
            target_skills=tuple(row["target_skills"]),
            kind=row.get("kind", "job"),
        )
        for row in goal_rows
    }

    catalog = Catalog(skills=skills, items=items, goals=goals)
    validate(catalog)

    taught_by: dict[str, list[str]] = {sid: [] for sid in skills}
    for item in items.values():
        for sid in item.teaches:
            taught_by[sid].append(item.id)
    catalog.taught_by = taught_by
    return catalog


def validate(catalog: Catalog) -> None:
    """Fail loudly on any internal inconsistency."""
    skill_ids = set(catalog.skills)

    # 1. Skill prerequisites resolve, and no skill requires itself.
    for skill in catalog.skills.values():
        if skill.level not in (1, 2, 3):
            raise CatalogError(f"skill '{skill.id}' has invalid level {skill.level}")
        for prereq in skill.prerequisites:
            if prereq not in skill_ids:
                raise CatalogError(
                    f"skill '{skill.id}' requires unknown skill '{prereq}'"
                )
            if prereq == skill.id:
                raise CatalogError(f"skill '{skill.id}' lists itself as a prerequisite")

    # 2. The prerequisite graph is acyclic (Kahn's algorithm).
    indegree = {sid: len(catalog.skills[sid].prerequisites) for sid in skill_ids}
    dependents: dict[str, list[str]] = {sid: [] for sid in skill_ids}
    for skill in catalog.skills.values():
        for prereq in skill.prerequisites:
            dependents[prereq].append(skill.id)
    queue = [sid for sid, deg in indegree.items() if deg == 0]
    resolved = 0
    while queue:
        current = queue.pop()
        resolved += 1
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if resolved != len(skill_ids):
        stuck = sorted(sid for sid, deg in indegree.items() if deg > 0)
        raise CatalogError(f"prerequisite cycle detected involving: {stuck}")

    # 3. Learning items reference real skills and use valid enums.
    for item in catalog.items.values():
        if item.type not in ITEM_TYPES:
            raise CatalogError(f"item '{item.id}' has invalid type '{item.type}'")
        if item.format not in FORMATS:
            raise CatalogError(f"item '{item.id}' has invalid format '{item.format}'")
        if item.cost not in COSTS:
            raise CatalogError(f"item '{item.id}' has invalid cost '{item.cost}'")
        if item.level not in (1, 2, 3):
            raise CatalogError(f"item '{item.id}' has invalid level {item.level}")
        if item.hours <= 0:
            raise CatalogError(f"item '{item.id}' must have positive hours")
        for sid in (*item.teaches, *item.requires):
            if sid not in skill_ids:
                raise CatalogError(f"item '{item.id}' references unknown skill '{sid}'")
        overlap = set(item.teaches) & set(item.requires)
        if overlap:
            raise CatalogError(
                f"item '{item.id}' both teaches and requires {sorted(overlap)}"
            )
        if item.type == "course" and not item.teaches:
            raise CatalogError(f"course '{item.id}' teaches no skills")
        if item.type in ("project", "assessment") and not item.requires:
            raise CatalogError(f"{item.type} '{item.id}' requires no skills")
        if item.type == "resource":
            # A resource exists to be opened, and to hang off a skill.
            if not item.url:
                raise CatalogError(f"resource '{item.id}' has no url")
            if not item.teaches:
                raise CatalogError(f"resource '{item.id}' supports no skill")
        if item.url and not item.url.startswith("https://"):
            raise CatalogError(f"item '{item.id}' has a non-https url: {item.url}")

    # 4. A course's own prerequisites must be consistent with what it teaches:
    #    it must never require a skill that depends on something it teaches.
    for item in catalog.items.values():
        taught = set(item.teaches)
        for required in item.requires:
            closure = catalog.prerequisite_closure([required])
            clash = closure & taught
            if clash:
                raise CatalogError(
                    f"item '{item.id}' requires '{required}', which itself depends on "
                    f"skill(s) it claims to teach: {sorted(clash)}"
                )

    # 5. Every skill is teachable by at least one *course*, otherwise a
    #    generated path could contain a gap that can never be filled. Resources
    #    do not count: the planner never schedules them.
    teachable = {
        sid for item in catalog.items.values() if item.is_course for sid in item.teaches
    }
    missing = skill_ids - teachable
    if missing:
        raise CatalogError(
            f"no course teaches these skills: {sorted(missing)}"
        )

    # 6. Goals reference real skills, and say what kind of goal they are.
    for goal in catalog.goals.values():
        if goal.kind not in GOAL_KINDS:
            raise CatalogError(
                f"goal '{goal.id}' has unknown kind '{goal.kind}'; "
                f"expected one of {sorted(GOAL_KINDS)}"
            )
        if not goal.target_skills:
            raise CatalogError(f"goal '{goal.id}' has no target skills")
        for sid in goal.target_skills:
            if sid not in skill_ids:
                raise CatalogError(f"goal '{goal.id}' targets unknown skill '{sid}'")


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    """Process-wide singleton. Validated on first access."""
    return load_catalog()
