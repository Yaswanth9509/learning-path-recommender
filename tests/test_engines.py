"""Profiling, gap analysis, recommendation scoring, and the rule-based assistant."""

from __future__ import annotations

import pytest

from backend import assistant, recommender
from backend.gap import analyze, resolve_target_skills
from backend.models import FeedbackEntry, LearnerProfile
from backend.profiling import apply_feedback, derive, summarize


# ------------------------------------------------------------------ profiling
def test_completed_courses_grant_their_skills(catalog):
    profile = LearnerProfile(learner_id="a", completed_item_ids=["c-prog-101"])
    derived = derive(catalog, profile)
    assert "python" in derived.direct_skills
    assert "prog-fundamentals" in derived.direct_skills


def test_prerequisites_are_implied_not_direct(catalog):
    profile = LearnerProfile(learner_id="a", declared_skills=["kubernetes"])
    derived = derive(catalog, profile)
    assert "kubernetes" in derived.direct_skills
    assert "docker" in derived.implied_skills
    assert "linux" in derived.implied_skills
    assert "docker" not in derived.direct_skills
    assert derived.known_skills >= {"kubernetes", "docker", "linux", "cli-git"}


def test_unknown_skill_ids_are_ignored(catalog):
    profile = LearnerProfile(learner_id="a", declared_skills=["not-a-skill"])
    derived = derive(catalog, profile)
    assert derived.direct_skills == frozenset()


def test_interest_domains_from_free_text(catalog):
    profile = LearnerProfile(learner_id="a", interests=["I enjoy building web apps"])
    derived = derive(catalog, profile)
    assert "Web Development" in derived.interest_domains


def test_summary_mentions_the_goal(catalog):
    profile = LearnerProfile(learner_id="a", goal_id="goal-devops", name="Sam")
    derived = derive(catalog, profile)
    assert "DevOps" in summarize(catalog, derived)


# ---------------------------------------------------------------------- gap
def test_gap_is_the_closure_minus_the_known(catalog):
    profile = LearnerProfile(learner_id="a", goal_id="goal-devops",
                             declared_skills=["linux", "cli-git"])
    derived = derive(catalog, profile)
    report, missing = analyze(catalog, derived, "goal-devops")
    assert "linux" not in missing
    assert "docker" in missing
    assert set(report.missing_skills) == set(missing)
    assert 0 < report.coverage_pct < 100


def test_bridging_skills_are_outside_the_headline_goal(catalog):
    profile = LearnerProfile(learner_id="a", goal_id="goal-ai-engineer")
    derived = derive(catalog, profile)
    report, _ = analyze(catalog, derived, "goal-ai-engineer")
    targets = set(report.target_skills)
    assert all(s not in targets for s in report.bridging_skills)
    # Reaching LLM engineering requires deep learning, which is not a target.
    assert "deep-learning" in report.bridging_skills


def test_extra_target_skills_are_added(catalog):
    targets = resolve_target_skills(catalog, "goal-frontend", ["kubernetes"])
    assert "kubernetes" in targets


def test_full_knowledge_means_no_gap(catalog):
    goal = catalog.goal("goal-data-analyst")
    everything = list(catalog.prerequisite_closure(goal.target_skills))
    profile = LearnerProfile(learner_id="a", goal_id="goal-data-analyst",
                             declared_skills=everything)
    derived = derive(catalog, profile)
    report, missing = analyze(catalog, derived, "goal-data-analyst")
    assert missing == []
    assert report.coverage_pct == 100.0


# -------------------------------------------------------------- recommender
def _ctx(catalog, **profile_kwargs):
    profile_kwargs.setdefault("goal_id", "goal-ml-engineer")
    profile = LearnerProfile(learner_id="a", **profile_kwargs)
    derived = derive(catalog, profile)
    report, missing = analyze(catalog, derived, profile.goal_id)
    required = set(report.target_skills) | set(report.known_skills) | set(missing)
    return recommender.build_context(catalog, derived, missing, required)


def test_weights_sum_to_one():
    assert abs(sum(recommender.WEIGHTS.values()) - 1.0) < 1e-9


def test_score_is_bounded(catalog):
    ctx = _ctx(catalog)
    for item in catalog.items.values():
        score, breakdown = recommender.score_item(ctx, item)
        assert 0.0 <= score <= 1.0
        for value in breakdown.model_dump().values():
            assert 0.0 <= value <= 1.0


def test_recommendations_are_sorted_and_limited(catalog):
    ctx = _ctx(catalog)
    recs = recommender.recommend(ctx, limit=5)
    assert len(recs) == 5
    assert recs == sorted(recs, key=lambda r: (-r.score, r.hours, r.item_id))


def test_every_recommendation_has_reasons(catalog):
    ctx = _ctx(catalog)
    for rec in recommender.recommend(ctx, limit=15):
        assert rec.reasons, f"{rec.item_id} has no explanation"


def test_ready_only_filters_out_blocked_items(catalog):
    ctx = _ctx(catalog)
    ready = recommender.recommend(ctx, limit=30, require_ready=True)
    assert ready
    for rec in ready:
        assert rec.prerequisites_met
        assert not rec.missing_prerequisites


def test_completed_and_excluded_items_are_never_recommended(catalog):
    ctx = _ctx(
        catalog,
        completed_item_ids=["c-prog-101"],
        excluded_item_ids=["c-stats"],
    )
    ids = {r.item_id for r in recommender.recommend(ctx, limit=50)}
    assert "c-prog-101" not in ids
    assert "c-stats" not in ids


def test_goal_domain_counts_as_an_interest(catalog):
    """Setting a goal implies interest in its domain, with no extra input."""
    ctx = _ctx(catalog, goal_id="goal-frontend")
    assert "Web Development" in ctx.derived.interest_domains


def test_interest_match_favours_the_learners_domain(catalog):
    ctx = _ctx(catalog, goal_id="goal-frontend", interests=["Web Development"])
    in_domain = recommender.score_item(ctx, catalog.items["c-react"])[1]
    out_of_domain = recommender.score_item(ctx, catalog.items["c-k8s"])[1]
    assert in_domain.interest_match == 1.0
    assert out_of_domain.interest_match < in_domain.interest_match


def test_extra_interests_widen_the_match(catalog):
    """Naming a second domain lifts that domain's items."""
    narrow = _ctx(catalog, goal_id="goal-frontend")
    wide = _ctx(catalog, goal_id="goal-frontend", interests=["Cloud & DevOps"])
    item = catalog.items["c-k8s"]
    assert (
        recommender.score_item(wide, item)[1].interest_match
        > recommender.score_item(narrow, item)[1].interest_match
    )


def test_free_preference_penalises_paid_items(catalog):
    free = _ctx(catalog, cost_preference="free")
    any_cost = _ctx(catalog, cost_preference="any")
    paid_item = next(i for i in catalog.items.values() if i.cost == "paid")
    free_score = recommender.score_item(free, paid_item)[0]
    any_score = recommender.score_item(any_cost, paid_item)[0]
    assert free_score < any_score


def test_best_course_prefers_broader_coverage(catalog):
    ctx = _ctx(catalog, goal_id="goal-ml-engineer")
    course = recommender.best_course_for_skill(ctx, "ml-foundations")
    assert course is not None
    assert "ml-foundations" in course.teaches


# ------------------------------------------------------------ rule assistant
@pytest.mark.parametrize(
    "text,expected",
    [
        ("I want to become a data analyst", "goal-data-analyst"),
        ("help me get into machine learning and deep learning", "goal-ml-engineer"),
        ("I'd like to build RAG chatbots with LLMs", "goal-ai-engineer"),
        ("I want a frontend job working with React", "goal-frontend"),
        ("teach me kubernetes and ci/cd for devops", "goal-devops"),
        ("I want to be a site reliability engineer on call", "goal-sre"),
    ],
)
def test_rule_engine_maps_goals(catalog, text, expected):
    result = assistant.interpret_goal(catalog, text)
    assert result.source == "rules"  # LLM disabled in tests
    assert result.goal_id == expected


def test_rule_engine_reads_level_and_hours(catalog):
    result = assistant.interpret_goal(
        catalog, "I'm a complete beginner and have 6 hours a week for data analysis"
    )
    assert result.experience_level == "beginner"
    assert result.weekly_hours == 6


def test_rule_engine_reads_advanced_level(catalog):
    result = assistant.interpret_goal(
        catalog, "I'm a senior engineer moving into cloud architecture"
    )
    assert result.experience_level == "advanced"


def test_rule_engine_extracts_known_skills(catalog):
    result = assistant.interpret_goal(
        catalog, "I already know Python and SQL, I want to be a data scientist"
    )
    assert "python" in result.declared_skills
    assert "sql" in result.declared_skills


def test_rule_engine_does_not_invent_skills(catalog):
    result = assistant.interpret_goal(catalog, "I want to learn python one day")
    # No "I know" signal, so nothing should be marked as already-held.
    assert result.declared_skills == []


def test_rule_engine_handles_unmatched_text(catalog):
    result = assistant.interpret_goal(catalog, "asdfgh qwerty zxcvb")
    assert result.goal_id is None
    assert result.confidence == 0.0


def test_rule_answers_are_grounded(catalog):
    profile = LearnerProfile(learner_id="a", goal_id="goal-frontend")
    derived = derive(catalog, profile)
    from backend.pathgen import generate_path

    path = generate_path(catalog, derived, "goal-frontend")
    reply, source = assistant.answer(catalog, profile, path, "how long will this take?")
    assert source == "rules"
    assert str(path.total_weeks) in reply


def test_rule_answer_without_a_path(catalog):
    profile = LearnerProfile(learner_id="a")
    reply, _ = assistant.answer(catalog, profile, None, "what next?")
    assert "don't have a learning path" in reply


# ------------------------------------------------------------------ feedback
def test_too_easy_marks_skills_known_and_raises_difficulty(catalog):
    profile = LearnerProfile(learner_id="a", goal_id="goal-frontend",
                             experience_level="beginner")
    entry = FeedbackEntry(item_id="c-htmlcss", signal="too_easy")
    profile, note = apply_feedback(catalog, profile, entry)
    assert profile.difficulty_offset == 1
    assert "html-css" in profile.declared_skills
    assert "c-htmlcss" in profile.excluded_item_ids
    assert note
    assert profile.target_level == 2


def test_too_hard_lowers_difficulty(catalog):
    profile = LearnerProfile(learner_id="a", experience_level="intermediate")
    entry = FeedbackEntry(item_id="c-dl", signal="too_hard")
    profile, note = apply_feedback(catalog, profile, entry)
    assert profile.difficulty_offset == -1
    assert profile.target_level == 1
    assert note


def test_not_relevant_excludes_the_item(catalog):
    profile = LearnerProfile(learner_id="a")
    entry = FeedbackEntry(item_id="c-cv", signal="not_relevant")
    profile, _ = apply_feedback(catalog, profile, entry)
    assert "c-cv" in profile.excluded_item_ids


def test_liked_builds_provider_and_format_affinity(catalog):
    profile = LearnerProfile(learner_id="a")
    entry = FeedbackEntry(item_id="c-react", signal="liked")
    profile, _ = apply_feedback(catalog, profile, entry)
    # Whatever the catalogue says this item's provider is — not a name
    # pinned here, which broke the moment the real one replaced the invented one.
    assert profile.provider_affinity.get(catalog.items["c-react"].provider) == 1
    assert profile.format_affinity.get("interactive") == 1


def test_difficulty_offset_is_clamped(catalog):
    profile = LearnerProfile(learner_id="a")
    for item_id in ("c-htmlcss", "c-js", "c-cli-git"):
        profile, _ = apply_feedback(
            catalog, profile, FeedbackEntry(item_id=item_id, signal="too_easy")
        )
    assert profile.difficulty_offset == 1
