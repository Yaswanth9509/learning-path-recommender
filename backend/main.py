"""HTTP API and static hosting for the Personalised Learning Path Recommender."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (
    accounts,
    assistant,
    dashboard as dashboard_engine,
    insights,
    llm,
    mailer as mailer_mod,
    profiling,
    recommender,
    security,
    sqlstore,
)
from .catalog import Catalog, CatalogError, get_catalog
from .db import Database, get_db
from .gap import analyze
from .models import (
    MAX_ACTIVE_PATHS,
    Achievements,
    ChatRequest,
    ChatResponse,
    Dashboard,
    FeedbackEntry,
    FeedbackRequest,
    LearnerProfile,
    LearningPath,
    PaceReport,
    PathSummary,
    ProfileUpdate,
    ProgressUpdate,
    Recommendation,
    SkillGraph,
    WeeklyPlan,
)
from .pathgen import generate_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="AI-Powered Personalised Learning Path Recommender",
    description=(
        "Conversational goal capture, learner profiling, prerequisite-aware "
        "path generation, explainable recommendations, and progress tracking."
    ),
    version="1.0.0",
)

# Optional API key, per-caller rate limiting, and response hardening. All of it
# is off or generous by default so the local demo needs no configuration; see
# `security.py` for the environment variables that tighten it for a deployment.
app.middleware("http")(security.gate_request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=security.allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_error(request, exc: Exception) -> JSONResponse:
    """Log the traceback server-side; never return internals to the caller."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


def catalog_dep() -> Catalog:
    try:
        return get_catalog()
    except CatalogError as exc:  # pragma: no cover - fails at startup if ever
        raise HTTPException(status_code=500, detail=f"catalog error: {exc}") from exc


def db_dep() -> Database:
    return get_db()


# --------------------------------------------------------------- accounts
def current_user(request: Request, db: Database = Depends(db_dep)):
    """The signed-in user, or `None`.

    Signing in is optional by design: without a session the API behaves as a
    single shared workspace, which is what the test suite and a local run use.
    With one, every learner is scoped to that account and invisible to others.
    """
    return db.accounts.user_for_session(request.cookies.get(accounts.SESSION_COOKIE, ""))


def require_user(user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to do that")
    return user


#: Built once from the environment. `set_mailer` swaps it, which is how the
#: tests keep this whole feature off the network.
_mailer = None


def get_mailer():
    global _mailer
    if _mailer is None:
        _mailer = mailer_mod.build_mailer()
    return _mailer


def set_mailer(replacement) -> None:
    global _mailer
    _mailer = replacement


def _set_session(response: Response, db: Database, user) -> None:
    token, expires = db.accounts.start_session(user.id)
    response.set_cookie(
        accounts.SESSION_COOKIE,
        token,
        expires=expires.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        httponly=True,          # unreadable from JavaScript, so XSS cannot lift it
        samesite="lax",         # not sent on cross-site POSTs
        secure=_env_flag("COOKIE_SECURE", False),  # set behind HTTPS
        path="/",
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "")
    return default if not raw.strip() else raw.strip().lower() not in {"0", "false", "no"}


def _user_view(user, db: Database) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_guest": user.is_guest,
        "learners": db.count_learners(user.id),
        "max_learners": accounts.MAX_LEARNERS_PER_USER,
        "max_paths_per_learner": MAX_ACTIVE_PATHS,
    }


@app.post("/api/auth/register", status_code=201)
def register(response: Response, payload: dict = Body(...), db: Database = Depends(db_dep)):
    try:
        user = db.accounts.create_user(
            str(payload.get("email", "")),
            str(payload.get("password", "")),
            str(payload.get("display_name", "")),
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Learners with no owner are NOT adopted by default. The feature exists to
    # migrate a database that predates accounts, but on any instance reachable
    # by more than one person it leaks: an anonymous visitor creates learners,
    # and the next person to register inherits them. Off unless asked for.
    if _env_flag("ADOPT_ORPHAN_LEARNERS", False):
        adopted = db.adopt_orphan_learners(user.id)
        if adopted:
            log.warning(
                "adopted %d unowned learner(s) into %s because "
                "ADOPT_ORPHAN_LEARNERS is set", adopted, user.id,
            )
    _set_session(response, db, user)
    return _user_view(user, db)


@app.post("/api/auth/login")
def login(response: Response, payload: dict = Body(...), db: Database = Depends(db_dep)):
    try:
        user = db.accounts.authenticate(
            str(payload.get("email", "")), str(payload.get("password", ""))
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session(response, db, user)
    return _user_view(user, db)


@app.post("/api/auth/guest", status_code=201)
def guest(response: Response, db: Database = Depends(db_dep)):
    """A throwaway account, so anyone can try the product without signing up."""
    user, _ = db.accounts.create_guest()
    _set_session(response, db, user)
    return _user_view(user, db)


@app.post("/api/auth/upgrade")
def upgrade_guest(
    payload: dict = Body(...),
    user=Depends(require_user),
    db: Database = Depends(db_dep),
) -> dict:
    """Keep a guest's work by giving the same account real credentials."""
    try:
        upgraded = db.accounts.upgrade_guest(
            user.id, str(payload.get("email", "")), str(payload.get("password", ""))
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _user_view(upgraded, db)


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Database = Depends(db_dep)):
    db.accounts.end_session(request.cookies.get(accounts.SESSION_COOKIE, ""))
    response.delete_cookie(accounts.SESSION_COOKIE, path="/")


@app.get("/api/auth/me")
def whoami(user=Depends(current_user), db: Database = Depends(db_dep)) -> dict:
    if user is None:
        return {"signed_in": False, "max_learners": accounts.MAX_LEARNERS_PER_USER}
    return {"signed_in": True, **_user_view(user, db)}


@app.patch("/api/auth/profile")
def update_account(
    payload: dict = Body(...),
    user=Depends(require_user),
    db: Database = Depends(db_dep),
) -> dict:
    """Change your own account details. Only the display name so far."""
    name = str(payload.get("display_name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="display name must not be empty")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="display name is too long")
    db.accounts.set_display_name(user.id, name)
    updated = db.accounts.get_user(user.id)
    return _user_view(updated, db)


@app.post("/api/auth/forgot", status_code=202)
def forgot_password(payload: dict = Body(...), db: Database = Depends(db_dep)) -> dict:
    """Mail a single-use reset link, if there is an account to mail it to.

    Always the same 202 and the same body. Whether the address is registered,
    belongs to a guest, or has never been seen, the caller learns nothing — the
    property the recovery question could only approximate with a decoy.
    """
    if not _env_flag("ALLOW_PASSWORD_RESET", True):
        raise HTTPException(
            status_code=403, detail="password reset is disabled on this instance"
        )
    email = str(payload.get("email", "")).strip()
    mailer = get_mailer()
    if not mailer.configured:
        # No key on this instance. Say so plainly rather than pretending to
        # have sent something that will never arrive.
        raise HTTPException(
            status_code=503,
            detail="email is not configured on this instance — "
                   "use your recovery question instead",
        )

    issued = None
    try:
        issued = db.accounts.begin_password_reset(email)
    except accounts.AuthError:
        issued = None

    if issued is not None:
        user, token = issued
        link = f"{mailer_mod.public_base_url()}/#reset/{token}"
        try:
            mailer.send(mailer_mod.reset_message(
                user.email, link, accounts.RESET_TTL_MINUTES))
            log.info("reset link mailed to %s", user.email)
        except mailer_mod.MailError as exc:
            # Do not surface this: the reply must not vary with whether an
            # address exists, and that includes varying by failure.
            log.error("could not mail a reset link to %s: %s", user.email, exc)

    return {"sent": True, "expires_in_minutes": accounts.RESET_TTL_MINUTES}


@app.post("/api/auth/reset")
def reset_with_token(
    payload: dict = Body(...),
    response: Response = None,  # noqa: RUF013 - FastAPI fills this in
    db: Database = Depends(db_dep),
) -> dict:
    """Spend a reset link and sign the account back in.

    Signing in here is deliberate: the person just proved control of the
    mailbox and chose a new password, and bouncing them to a login form to
    retype it immediately is friction with no security to show for it.
    """
    try:
        user = db.accounts.complete_password_reset(
            str(payload.get("token", "")),
            str(payload.get("password", "")),
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.warning("password reset by email link for %s", user.email)
    _set_session(response, db, user)
    return _user_view(user, db)


@app.patch("/api/auth/password", status_code=204)
def change_password(
    payload: dict = Body(...), user=Depends(require_user), db: Database = Depends(db_dep)
) -> None:
    try:
        db.accounts.change_password(
            user.id, str(payload.get("current", "")), str(payload.get("new", ""))
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------- helpers
def _load_profile(db: Database, learner_id: str, user=None) -> LearnerProfile:
    profile = db.get_profile(learner_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown learner '{learner_id}'")
    # A learner belongs to exactly one workspace: an account, or — when nobody
    # is signed in, which is how the tests and a local run behave — the
    # ownerless one. The caller must be in the same workspace.
    #
    # This used to read `if user is not None and owner and ...`, which let two
    # things through: a signed-in user could reach any ownerless learner, and a
    # caller with no session at all could reach *anyone's* learner by id —
    # read it, rewrite it, or delete it outright.
    #
    # Not found rather than forbidden, so the caller learns nothing about
    # whether that id exists.
    owner = db.owner_of(learner_id) or ""
    caller = user.id if user is not None else ""
    if owner != caller:
        raise HTTPException(status_code=404, detail=f"unknown learner '{learner_id}'")
    return profile


def _resolve_goal(catalog: Catalog, profile: LearnerProfile) -> str:
    if not profile.goal_id:
        raise HTTPException(
            status_code=400,
            detail="This learner has no goal yet. Describe a goal in the chat, "
            "or set goal_id on the profile first.",
        )
    if profile.goal_id not in catalog.goals:
        raise HTTPException(
            status_code=400, detail=f"unknown goal '{profile.goal_id}'"
        )
    return profile.goal_id


def _track_goal(profile: LearnerProfile, goal_id: str, db: Database) -> list[str]:
    """Make `goal_id` the active one, keeping it in the learner's goal list.

    At the cap, the goal being replaced is the one currently active — a learner
    who names a fourth goal is swapping what they are working on now, not
    silently dropping something they set up days ago. Whatever is dropped has
    its roadmap deleted too: leaving the row behind made the stored paths and
    the profile disagree, and the cap stopped meaning anything.
    """
    stored = [p.goal_id for p in db.list_paths(profile.learner_id)]
    goals = [g for g in dict.fromkeys(stored + profile.goal_ids) if g != goal_id]
    dropped: list[str] = []
    while len(goals) >= MAX_ACTIVE_PATHS:
        victim = profile.goal_id if profile.goal_id in goals else goals[0]
        goals = [g for g in goals if g != victim]
        dropped.append(victim)

    for goal in dropped:
        db.delete_path(profile.learner_id, goal)
    profile.goal_ids = goals + [goal_id]
    profile.goal_id = goal_id
    return dropped


def _regenerate(
    catalog: Catalog,
    db: Database,
    profile: LearnerProfile,
    notes: list[str] | None = None,
) -> LearningPath:
    """Rebuild the path from the current profile, preserving progress."""
    goal_id = _resolve_goal(catalog, profile)
    dropped = _track_goal(profile, goal_id, db)
    profiling.sync_completions(
        catalog,
        profile,
        db.completed_items(profile.learner_id),
        tracked_item_ids=list(db.get_progress(profile.learner_id)),
    )
    db.save_profile(profile)

    derived = profiling.derive(catalog, profile)
    previous = db.get_path(profile.learner_id, goal_id)
    revision = (previous.revision + 1) if previous else 1

    messages = list(notes or [])
    if dropped:
        names = ", ".join(
            catalog.goals[g].title for g in dropped if g in catalog.goals
        )
        messages.append(
            f"You can run {MAX_ACTIVE_PATHS} paths at once, so {names} made room "
            "for this one. Your progress on its courses is kept."
        )
    path = generate_path(
        catalog=catalog,
        derived=derived,
        goal_id=goal_id,
        progress=db.get_progress(profile.learner_id),
        adaptation_notes=messages,
        revision=revision,
    )
    db.save_path(path)
    return path


# -------------------------------------------------------------------- system
@app.get("/api/health")
def health() -> dict:
    status = llm.status()
    catalog = get_catalog()
    return {
        "status": "ok",
        "llm_enabled": status["enabled"],
        "provider": status["provider"],
        "model": status["model"],
        "assistant_mode": status["mode"],
        "assistant_name": assistant.ASSISTANT_NAME,
        "configured_providers": status["configured_providers"],
        "available_providers": list(llm.DEFAULT_MODELS),
        "catalog": {
            "skills": len(catalog.skills),
            "items": len(catalog.items),
            "goals": len(catalog.goals),
        },
        # Which engine is configured, read from the environment rather than
        # from a live connection. This is a liveness probe — Render points its
        # health check here — so it must answer while a serverless database is
        # suspended. Waking Neon took over fifteen seconds on a cold start,
        # which would have failed the deploy's health check.
        "storage": "postgres" if sqlstore.database_url() else "sqlite",
        "security": security.status(),
    }


# ------------------------------------------------------------------- catalog
@app.get("/api/catalog/goals")
def list_goals(catalog: Catalog = Depends(catalog_dep)) -> list[dict]:
    return [
        {
            "id": goal.id,
            "title": goal.title,
            "domain": goal.domain,
            "description": goal.description,
            "kind": goal.kind,
            "outcome": goal.outcome,
            "typical_roles": list(goal.typical_roles),
            "target_skills": list(goal.target_skills),
            "target_skill_names": [catalog.skill_name(s) for s in goal.target_skills],
        }
        for goal in catalog.goals.values()
    ]


@app.get("/api/catalog/skills")
def list_skills(catalog: Catalog = Depends(catalog_dep)) -> list[dict]:
    return [
        {
            "id": s.id,
            "name": s.name,
            "domain": s.domain,
            "level": s.level,
            "description": s.description,
            "prerequisites": list(s.prerequisites),
            "prerequisite_names": [catalog.skill_name(p) for p in s.prerequisites],
        }
        for s in catalog.skills.values()
    ]


@app.get("/api/catalog/items")
def list_items(
    catalog: Catalog = Depends(catalog_dep),
    item_type: Optional[str] = Query(None, alias="type"),
) -> list[dict]:
    items = catalog.items.values()
    if item_type:
        items = [i for i in items if i.type == item_type]
    return [
        {
            "id": i.id,
            "title": i.title,
            "provider": i.provider,
            "type": i.type,
            "level": i.level,
            "hours": i.hours,
            "rating": i.rating,
            "format": i.format,
            "cost": i.cost,
            "domain": i.domain,
            "description": i.description,
            "url": i.url,
            "teaches": list(i.teaches),
            "teaches_names": [catalog.skill_name(s) for s in i.teaches],
        }
        for i in items
    ]


# ------------------------------------------------------------------ learners
@app.post("/api/learners", response_model=LearnerProfile, status_code=201)
def create_learner(
    payload: ProfileUpdate = Body(default_factory=ProfileUpdate),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearnerProfile:
    owner = user.id if user else ""
    if db.count_learners(owner) >= accounts.MAX_LEARNERS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"You can keep {accounts.MAX_LEARNERS_PER_USER} learners. "
            "Delete one before adding another.",
        )
    profile = LearnerProfile(learner_id=uuid.uuid4().hex[:10])
    _apply_update(catalog, profile, payload)
    return db.save_profile(profile, user_id=owner)


@app.get("/api/learners", response_model=list[LearnerProfile])
def list_learners(
    db: Database = Depends(db_dep), user=Depends(current_user)
) -> list[LearnerProfile]:
    """Only your own learners — never anyone else's."""
    return db.list_profiles(user_id=user.id if user else "")


@app.get("/api/learners/{learner_id}/profile", response_model=LearnerProfile)
def get_profile(
    learner_id: str,
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearnerProfile:
    return _load_profile(db, learner_id, user)


@app.get("/api/learners/{learner_id}/profile/summary")
def profile_summary(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    profile = _load_profile(db, learner_id, user)
    derived = profiling.derive(catalog, profile)
    return {
        "summary": profiling.summarize(catalog, derived),
        "direct_skills": sorted(derived.direct_skills),
        "direct_skill_names": sorted(
            catalog.skill_name(s) for s in derived.direct_skills
        ),
        "implied_skills": sorted(derived.implied_skills),
        "implied_skill_names": sorted(
            catalog.skill_name(s) for s in derived.implied_skills
        ),
        "interest_domains": sorted(derived.interest_domains),
        "target_level": profile.target_level,
    }


def _apply_update(
    catalog: Catalog, profile: LearnerProfile, payload: ProfileUpdate
) -> None:
    data = payload.model_dump(exclude_unset=True, exclude_none=True)

    if "goal_id" in data and data["goal_id"] not in catalog.goals:
        raise HTTPException(status_code=400, detail=f"unknown goal '{data['goal_id']}'")

    for field in ("declared_skills", "extra_target_skills"):
        if field in data:
            unknown = [s for s in data[field] if s not in catalog.skills]
            if unknown:
                raise HTTPException(
                    status_code=400, detail=f"unknown skill ids in {field}: {unknown}"
                )

    if "completed_item_ids" in data:
        unknown = [i for i in data["completed_item_ids"] if i not in catalog.items]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown item ids: {unknown}"
            )

    if "preferred_formats" in data:
        allowed = {"video", "interactive", "reading", "project"}
        bad = [f for f in data["preferred_formats"] if f not in allowed]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown formats: {bad}")

    for key, value in data.items():
        setattr(profile, key, value)


@app.patch("/api/learners/{learner_id}/profile", response_model=LearnerProfile)
def update_profile(
    learner_id: str,
    payload: ProfileUpdate,
    regenerate: bool = Query(True, description="Rebuild the path after updating"),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearnerProfile:
    profile = _load_profile(db, learner_id, user)
    _apply_update(catalog, profile, payload)
    db.save_profile(profile)

    if regenerate and profile.goal_id:
        _regenerate(catalog, db, profile, notes=["Path rebuilt after a profile change."])
    return profile


@app.delete("/api/learners/{learner_id}", status_code=204)
def delete_learner(
    learner_id: str,
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> None:
    _load_profile(db, learner_id, user)
    db.delete_learner(learner_id)


# ---------------------------------------------------------------------- chat
@app.post("/api/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> ChatResponse:
    """Conversational entry point.

    A message either (a) states a goal, in which case the profile is updated and
    a path generated, or (b) asks a question, which is answered against the
    learner's existing path.
    """
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    profile = db.get_profile(payload.learner_id)
    if profile is None:
        # An unseen id starts a learner in the caller's own workspace. Without
        # the owner it landed unowned, so a signed-in user could not see the
        # learner they had just created by talking to it.
        profile = LearnerProfile(learner_id=payload.learner_id)
        db.save_profile(profile, user_id=user.id if user is not None else "")
    else:
        # Chat reads the learner's whole path as context and writes to their
        # conversation, so it needs the same ownership gate as every other
        # learner route.
        _load_profile(db, payload.learner_id, user)

    db.add_message(profile.learner_id, "user", message)
    existing_path = db.get_path(profile.learner_id, profile.goal_id)

    use_provider = profile.assistant_engine != "rules"
    interpretation = assistant.interpret_goal(catalog, message, use_provider)

    # A learner with no goal yet who says something the rules cannot pin to one
    # goal gets asked, not guessed at — a wrong roadmap costs them months. A
    # provider that identified a goal confidently is trusted over the keywords.
    if profile.goal_id is None and existing_path is None:
        ambiguous = assistant.clarification(catalog, message)
        if ambiguous is not None and (
            interpretation.goal_id is None
            or interpretation.confidence < assistant.CLARIFY_TRUST_CONFIDENCE
        ):
            question, options = ambiguous
            db.add_message(profile.learner_id, "assistant", question)
            return ChatResponse(
                reply=question,
                interpretation=interpretation,
                profile=db.get_profile(profile.learner_id),
                path_generated=False,
                # Not a fallback: the assistant is deliberately asking rather
                # than guessing, and the UI must not report it as the model
                # having failed.
                source="assistant",
                suggested_replies=options,
                needs_clarification=True,
            )

    # Treat the message as a goal statement only when it identifies a goal we
    # do not already have, or the learner is explicitly switching goals.
    switching = any(
        phrase in message.lower()
        for phrase in ("instead", "switch", "change my goal", "actually i want", "now i want")
    )
    # Naming a different goal outright is itself a switch — a learner who says
    # "I want to be a data analyst" should not be told to go and edit a form.
    # Questions about a goal ("should I be a data analyst?") are not switches.
    restated = (
        interpretation.goal_id is not None
        and interpretation.goal_id != profile.goal_id
        and interpretation.confidence >= assistant.GOAL_SWITCH_CONFIDENCE
        and assistant.reads_as_goal_statement(message)
    )
    # The goal is known but no path exists yet, which is the gap between the
    # intake question and its answer. A path that is merely missing still
    # counts as a goal statement; one we are deliberately waiting on does not,
    # or every follow-up question would rebuild the path under a new goal.
    answering_intake = profile.goal_id is not None and existing_path is None
    first_goal = profile.goal_id is None and existing_path is None

    is_goal_statement = interpretation.goal_id is not None and (
        profile.goal_id is None
        or (existing_path is None and not answering_intake)
        or ((switching or restated) and interpretation.goal_id != profile.goal_id)
    )

    path_generated = False
    if is_goal_statement:
        profile.goal_id = interpretation.goal_id
        profile.goal_text = message
        if interpretation.experience_level:
            profile.experience_level = interpretation.experience_level
        if interpretation.weekly_hours:
            profile.weekly_hours = interpretation.weekly_hours
        if interpretation.interests:
            profile.interests = sorted(
                set(profile.interests) | set(interpretation.interests)
            )
        if interpretation.declared_skills:
            profile.declared_skills = sorted(
                set(profile.declared_skills) | set(interpretation.declared_skills)
            )
        if interpretation.extra_target_skills:
            profile.extra_target_skills = sorted(
                set(profile.extra_target_skills)
                | set(interpretation.extra_target_skills)
            )
        if interpretation.preferred_formats:
            profile.preferred_formats = interpretation.preferred_formats
        db.save_profile(profile)

        # Where the path starts and how long it runs are the learner's to say.
        # Ask once, before building anything — unless they told us to choose.
        missing = assistant.intake_gaps(interpretation)
        if first_goal and missing and not assistant.defers_to_assistant(message):
            question, options = assistant.intake_question(
                catalog.goals[profile.goal_id], missing
            )
            db.add_message(profile.learner_id, "assistant", question)
            return ChatResponse(
                reply=question,
                interpretation=interpretation,
                profile=db.get_profile(profile.learner_id),
                path_generated=False,
                # Not a fallback: the assistant is deliberately asking rather
                # than guessing, and the UI must not report it as the model
                # having failed.
                source="assistant",
                suggested_replies=options,
                needs_clarification=True,
            )

        path = _regenerate(
            catalog, db, profile, notes=["Path created from your goal description."]
        )
        path_generated = True
        reply, source = assistant.acknowledge_goal(
            catalog, profile, path, interpretation, use_provider,
            all_paths=db.list_paths(profile.learner_id),
        )
    elif answering_intake and (
        interpretation.experience_level
        or interpretation.weekly_hours
        or assistant.defers_to_assistant(message)
    ):
        # Only a message that actually answers the intake question builds the
        # path. Anything else — a question, a change of subject — is answered
        # normally below, and the intake question simply stays open.
        if interpretation.experience_level:
            profile.experience_level = interpretation.experience_level
        if interpretation.weekly_hours:
            profile.weekly_hours = interpretation.weekly_hours
        db.save_profile(profile)

        # Anything still unstated keeps the profile default, which is already
        # in place — so nothing is overwritten here. A fact only counts as
        # assumed when neither the goal message nor this answer supplied it.
        already_stated = assistant.interpret_goal(
            catalog, profile.goal_text or "", use_provider=False
        )
        assumed = [
            gap
            for gap in assistant.intake_gaps(interpretation)
            if gap in assistant.intake_gaps(already_stated)
        ]

        path = _regenerate(
            catalog, db, profile, notes=["Path created from your goal description."]
        )
        path_generated = True
        reply, source = assistant.acknowledge_goal(
            catalog, profile, path, interpretation, use_provider,
            all_paths=db.list_paths(profile.learner_id),
        )
        # Only flag an assumption the learner did not ask us to make.
        if assumed and not assistant.defers_to_assistant(message):
            reply += assistant.intake_assumption_note(assumed)
    else:
        reply, source = assistant.answer(
            catalog,
            profile,
            existing_path,
            message,
            history=db.get_conversation(profile.learner_id, limit=10)[:-1],
            use_provider=use_provider,
            all_paths=db.list_paths(profile.learner_id),
        )
        # "I already know SQL" is an instruction, not small talk: credit the
        # skill and rebuild, so the plan shrinks instead of the learner being
        # told to go and edit their profile.
        newly_declared = [
            skill_id
            for skill_id in interpretation.declared_skills
            if skill_id not in profile.declared_skills and skill_id in catalog.skills
        ]
        if newly_declared and assistant.declares_existing_knowledge(message):
            profile.declared_skills = sorted(
                set(profile.declared_skills) | set(newly_declared)
            )
            db.save_profile(profile)
            names = ", ".join(catalog.skill_name(s) for s in newly_declared)
            if profile.goal_id:
                rebuilt = _regenerate(
                    catalog, db, profile,
                    notes=[f"Marked {names} as already known and rebuilt the path."],
                )
                reply += (
                    f"\n\nI have marked {names} as already known and rebuilt your "
                    f"path around it — it is now {rebuilt.total_hours} hours "
                    f"across {len(rebuilt.milestones)} stages."
                )
            else:
                reply += f"\n\nNoted that you already know {names}."

    db.add_message(profile.learner_id, "assistant", reply)
    updated_profile = db.get_profile(profile.learner_id)
    return ChatResponse(
        reply=reply,
        interpretation=interpretation,
        profile=updated_profile,
        path_generated=path_generated,
        source=source,
        suggested_replies=assistant.suggested_replies(
            catalog, db.get_path(profile.learner_id, updated_profile.goal_id),
            updated_profile,
        ),
    )


@app.get("/api/learners/{learner_id}/conversation")
def get_conversation(
    learner_id: str,
    db: Database = Depends(db_dep),
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
) -> list[dict]:
    _load_profile(db, learner_id, user)
    return db.get_conversation(learner_id, limit=limit)


@app.delete("/api/learners/{learner_id}/conversation", status_code=204)
def clear_conversation(
    learner_id: str,
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> None:
    _load_profile(db, learner_id, user)
    db.clear_conversation(learner_id)


# ---------------------------------------------------------------------- path
@app.post("/api/learners/{learner_id}/path", response_model=LearningPath)
def create_path(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    profile = _load_profile(db, learner_id, user)
    return _regenerate(catalog, db, profile, notes=["Path generated on request."])


@app.get("/api/learners/{learner_id}/path", response_model=LearningPath)
def get_path(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    profile = _load_profile(db, learner_id, user)
    path = db.get_path(learner_id, profile.goal_id)
    if path is None:
        path = _regenerate(catalog, db, profile)
    return path


# --------------------------------------------------------------- many paths
def _summaries(
    catalog: Catalog, db: Database, profile: LearnerProfile
) -> list[PathSummary]:
    return dashboard_engine.summarise_paths(
        catalog,
        db.list_paths(profile.learner_id),
        db.get_progress(profile.learner_id),
        profile.goal_id,
    )


@app.get("/api/learners/{learner_id}/paths", response_model=list[PathSummary])
def list_learner_paths(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> list[PathSummary]:
    """Every roadmap this learner is running, active one flagged."""
    profile = _load_profile(db, learner_id, user)
    return _summaries(catalog, db, profile)


@app.post("/api/learners/{learner_id}/paths", response_model=LearningPath, status_code=201)
def add_learner_path(
    learner_id: str,
    payload: dict = Body(...),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    """Start a second or third roadmap alongside the current one."""
    profile = _load_profile(db, learner_id, user)
    goal_id = str(payload.get("goal_id") or "")
    if goal_id not in catalog.goals:
        raise HTTPException(status_code=400, detail=f"unknown goal '{goal_id}'")

    existing = {p.goal_id for p in db.list_paths(learner_id)}
    if goal_id in existing:
        profile.goal_id = goal_id
        db.save_profile(profile)
        path = db.get_path(learner_id, goal_id)
        if path is not None:
            return path
    elif len(existing) >= MAX_ACTIVE_PATHS:
        raise HTTPException(
            status_code=400,
            detail=f"You can run {MAX_ACTIVE_PATHS} paths at once. Finish or "
            "remove one before adding another.",
        )

    profile.goal_id = goal_id
    return _regenerate(
        catalog, db, profile, notes=["Path created alongside your other goals."]
    )


@app.post("/api/learners/{learner_id}/paths/{goal_id}/activate", response_model=LearningPath)
def activate_learner_path(
    learner_id: str,
    goal_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    """Switch which roadmap the app is showing."""
    profile = _load_profile(db, learner_id, user)
    path = db.get_path(learner_id, goal_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no path for goal '{goal_id}'")
    profile.goal_id = goal_id
    if goal_id not in profile.goal_ids:
        profile.goal_ids = (profile.goal_ids + [goal_id])[-MAX_ACTIVE_PATHS:]
    db.save_profile(profile)
    return path


@app.delete("/api/learners/{learner_id}/paths/{goal_id}", status_code=204)
def delete_learner_path(
    learner_id: str,
    goal_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> None:
    """Drop a roadmap. Progress on its items is kept — you did that work."""
    profile = _load_profile(db, learner_id, user)
    if db.get_path(learner_id, goal_id) is None:
        raise HTTPException(status_code=404, detail=f"no path for goal '{goal_id}'")
    db.delete_path(learner_id, goal_id)
    profile.goal_ids = [g for g in profile.goal_ids if g != goal_id]
    if profile.goal_id == goal_id:
        profile.goal_id = profile.goal_ids[-1] if profile.goal_ids else None
    db.save_profile(profile)


# ------------------------------------------------- shaping the path by hand
@app.post("/api/learners/{learner_id}/path/items", response_model=LearningPath)
def add_path_item(
    learner_id: str,
    payload: dict = Body(...),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    """Add a course to the path yourself, overruling the planner.

    It is still placed by prerequisite — the earliest stage where everything it
    needs is already covered — so adding an item cannot break the ordering.
    """
    profile = _load_profile(db, learner_id, user)
    item_id = str(payload.get("item_id") or "")
    if item_id not in catalog.items:
        raise HTTPException(status_code=404, detail=f"unknown item '{item_id}'")
    if item_id in profile.pinned_item_ids:
        raise HTTPException(status_code=400, detail="that item is already on your path")

    profile.pinned_item_ids = sorted(set(profile.pinned_item_ids) | {item_id})
    profile.excluded_item_ids = [i for i in profile.excluded_item_ids if i != item_id]
    db.save_profile(profile)
    title = catalog.items[item_id].title
    return _regenerate(catalog, db, profile, notes=[f"Added “{title}” at your request."])


@app.delete("/api/learners/{learner_id}/path/items/{item_id}", response_model=LearningPath)
def remove_path_item(
    learner_id: str,
    item_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> LearningPath:
    """Drop an item from the path and find another route to the same skill."""
    profile = _load_profile(db, learner_id, user)
    if item_id not in catalog.items:
        raise HTTPException(status_code=404, detail=f"unknown item '{item_id}'")

    before = db.get_path(learner_id, profile.goal_id)
    was_uncovered = set(before.uncovered_skills) if before else set()

    profile.pinned_item_ids = [i for i in profile.pinned_item_ids if i != item_id]
    if item_id not in profile.excluded_item_ids:
        profile.excluded_item_ids.append(item_id)
    db.save_profile(profile)

    title = catalog.items[item_id].title
    path = _regenerate(
        catalog, db, profile, notes=[f"Removed “{title}” from your path."]
    )

    # Dropping the only course that teaches a skill takes everything downstream
    # of it with them. That is deliberate — a path that promises a course whose
    # prerequisite it never teaches is worse — but the learner has to be told,
    # in those words, that their own removal caused it.
    newly_uncovered = [s for s in path.uncovered_skills if s not in was_uncovered]
    if newly_uncovered:
        listed = ", ".join(newly_uncovered)
        path.adaptation_notes.append(
            f"“{title}” was the only course covering {listed}, so those are now "
            "out of reach and everything depending on them left the path. Add it "
            "back if that is not what you wanted."
        )
        db.save_path(path)
    return path


@app.get("/api/learners/{learner_id}/gap")
def get_gap(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    profile = _load_profile(db, learner_id, user)
    goal_id = _resolve_goal(catalog, profile)
    derived = profiling.derive(catalog, profile)
    report, _ = analyze(catalog, derived, goal_id)
    from .gap import explain as explain_gap

    return {"report": report.model_dump(), "explanation": explain_gap(catalog, report)}


# ----------------------------------------------------------- recommendations
@app.get(
    "/api/learners/{learner_id}/recommendations",
    response_model=list[Recommendation],
)
def get_recommendations(
    learner_id: str,
    limit: int = Query(10, ge=1, le=50),
    item_type: Optional[str] = Query(None, alias="type"),
    ready_only: bool = Query(False, description="Only items you can start today"),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> list[Recommendation]:
    profile = _load_profile(db, learner_id, user)
    goal_id = _resolve_goal(catalog, profile)
    derived = profiling.derive(catalog, profile)
    report, missing = analyze(catalog, derived, goal_id)
    required = set(report.target_skills) | set(report.known_skills) | set(missing)
    ctx = recommender.build_context(catalog, derived, missing, required)

    types = {item_type} if item_type else None
    if types and item_type not in ("course", "project", "assessment", "resource"):
        raise HTTPException(status_code=400, detail=f"unknown type '{item_type}'")

    return recommender.recommend(
        ctx, limit=limit, item_types=types, require_ready=ready_only
    )


@app.get("/api/learners/{learner_id}/explain/{item_id}")
def explain_item(
    learner_id: str,
    item_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    """Full, itemised justification for one recommendation."""
    profile = _load_profile(db, learner_id, user)
    if item_id not in catalog.items:
        raise HTTPException(status_code=404, detail=f"unknown item '{item_id}'")
    goal_id = _resolve_goal(catalog, profile)
    derived = profiling.derive(catalog, profile)
    report, missing = analyze(catalog, derived, goal_id)
    required = set(report.target_skills) | set(report.known_skills) | set(missing)
    ctx = recommender.build_context(catalog, derived, missing, required)
    rec = recommender.to_recommendation(ctx, catalog.items[item_id])

    path = db.get_path(learner_id, profile.goal_id)
    placement = None
    if path:
        for milestone in path.milestones:
            for entry in milestone.items:
                if entry.item_id == item_id:
                    placement = {
                        "milestone": milestone.title,
                        "order": milestone.order,
                        "rationale": entry.rationale,
                    }
    return {
        "recommendation": rec.model_dump(),
        "weights": recommender.WEIGHTS,
        "placement": placement,
    }


# ------------------------------------------------------------------ progress
@app.post("/api/learners/{learner_id}/progress")
def set_progress(
    learner_id: str,
    payload: ProgressUpdate,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    profile = _load_profile(db, learner_id, user)
    if payload.item_id not in catalog.items:
        raise HTTPException(status_code=404, detail=f"unknown item '{payload.item_id}'")

    db.set_progress(learner_id, payload.item_id, payload.status)
    profiling.sync_completions(
        catalog,
        profile,
        db.completed_items(learner_id),
        tracked_item_ids=list(db.get_progress(learner_id)),
    )
    db.save_profile(profile)

    # Reflect the new status in the stored path without reshuffling it: the
    # learner's plan should not move under them just because they ticked a box.
    path = db.get_path(learner_id, profile.goal_id)
    if path is not None:
        for milestone in path.milestones:
            for item in milestone.items:
                if item.item_id == payload.item_id:
                    item.status = payload.status
        db.save_path(path)

    return {
        "item_id": payload.item_id,
        "status": payload.status,
        "completed_items": db.completed_items(learner_id),
    }


@app.get("/api/learners/{learner_id}/progress")
def get_progress(
    learner_id: str,
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    _load_profile(db, learner_id, user)
    return {
        item_id: entry.model_dump()
        for item_id, entry in db.get_progress(learner_id).items()
    }


# ------------------------------------------------------------------ feedback
@app.post("/api/learners/{learner_id}/feedback")
def submit_feedback(
    learner_id: str,
    payload: FeedbackRequest,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    """Record feedback, adapt the profile, and regenerate the path."""
    profile = _load_profile(db, learner_id, user)
    if payload.item_id not in catalog.items:
        raise HTTPException(status_code=404, detail=f"unknown item '{payload.item_id}'")

    entry = FeedbackEntry(
        item_id=payload.item_id, signal=payload.signal, note=payload.note
    )
    db.add_feedback(learner_id, entry)

    profile, note = profiling.apply_feedback(catalog, profile, entry)
    db.save_profile(profile)

    path = None
    if profile.goal_id:
        path = _regenerate(catalog, db, profile, notes=[note] if note else [])

    return {
        "applied": note,
        "effect": profiling.FEEDBACK_EFFECTS.get(payload.signal, ""),
        "difficulty_offset": profile.difficulty_offset,
        "excluded_items": profile.excluded_item_ids,
        "path_revision": path.revision if path else None,
    }


@app.get("/api/learners/{learner_id}/feedback")
def list_feedback(
    learner_id: str,
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> list[dict]:
    _load_profile(db, learner_id, user)
    return [e.model_dump() for e in db.get_feedback(learner_id)]


# ----------------------------------------------------------------- dashboard
@app.get("/api/learners/{learner_id}/dashboard", response_model=Dashboard)
def get_dashboard(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> Dashboard:
    profile = _load_profile(db, learner_id, user)
    path = db.get_path(learner_id, profile.goal_id)
    if path is None:
        path = _regenerate(catalog, db, profile)
    derived = profiling.derive(catalog, profile)
    progress = db.get_progress(learner_id)
    return dashboard_engine.build(
        catalog,
        derived,
        path,
        progress,
        pace=insights.build_pace(catalog, profile, path, progress),
        paths=_summaries(catalog, db, profile),
    )


# ------------------------------------------------------ graph / plan / badges
def _path_and_profile(
    catalog: Catalog, db: Database, learner_id: str, user=None
) -> tuple[LearnerProfile, LearningPath]:
    profile = _load_profile(db, learner_id, user)
    path = db.get_path(learner_id, profile.goal_id)
    if path is None:
        path = _regenerate(catalog, db, profile)
    return profile, path


@app.get("/api/learners/{learner_id}/graph", response_model=SkillGraph)
def get_skill_graph(
    learner_id: str,
    goal_id: Optional[str] = Query(
        None, description="Graph this goal instead of the active one"
    ),
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> SkillGraph:
    """The prerequisite DAG a path was generated from.

    Defaults to the active path. A learner running several goals can ask for
    any of them by id, so the graph is not stuck on whichever one happens to
    be active.
    """
    profile = _load_profile(db, learner_id, user)
    if goal_id:
        path = db.get_path(learner_id, goal_id)
        if path is None:
            raise HTTPException(
                status_code=404, detail=f"no path for goal '{goal_id}'"
            )
    else:
        _, path = _path_and_profile(catalog, db, learner_id, user)
    derived = profiling.derive(catalog, profile)
    return insights.build_graph(
        catalog, derived, path, db.get_progress(learner_id)
    )


@app.get("/api/learners/{learner_id}/pace", response_model=PaceReport)
def get_pace(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> PaceReport:
    """How fast the learner is actually going, versus what they planned."""
    profile, path = _path_and_profile(catalog, db, learner_id, user)
    return insights.build_pace(catalog, profile, path, db.get_progress(learner_id))


@app.post("/api/learners/{learner_id}/replan")
def replan_to_pace(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> dict:
    """Rebuild the schedule around the learner's observed rate, not their hopes.

    Progress alone never reshuffles the plan — a learner's roadmap should not
    move under them because they ticked a box. This is the explicit opposite:
    they asked for the schedule to be refitted to how they actually study.
    """
    profile, path = _path_and_profile(catalog, db, learner_id, user)
    progress = db.get_progress(learner_id)
    pace = insights.build_pace(catalog, profile, path, progress)

    if pace.suggested_weekly_hours is None:
        return {
            "changed": False,
            "pace": pace.model_dump(),
            "weekly_hours": profile.weekly_hours,
            "path_revision": path.revision,
            "message": (
                pace.message
                if pace.status != "unknown"
                else "Not enough history yet — nothing to re-plan around."
            ),
        }

    previous = profile.weekly_hours
    profile.weekly_hours = pace.suggested_weekly_hours
    db.save_profile(profile)
    rebuilt = _regenerate(
        catalog,
        db,
        profile,
        notes=[
            f"Schedule re-planned around your real pace: {previous}h/week "
            f"→ {profile.weekly_hours}h/week."
        ],
    )
    return {
        "changed": True,
        "pace": pace.model_dump(),
        "weekly_hours": profile.weekly_hours,
        "previous_weekly_hours": previous,
        "path_revision": rebuilt.revision,
        "total_weeks": rebuilt.total_weeks,
        "message": (
            f"Rebuilt at {profile.weekly_hours}h a week — "
            f"{rebuilt.total_weeks} weeks to finish."
        ),
    }


@app.get("/api/learners/{learner_id}/plan", response_model=WeeklyPlan)
def get_weekly_plan(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> WeeklyPlan:
    """The roadmap resolved against the learner's actual weekly hours."""
    _, path = _path_and_profile(catalog, db, learner_id, user)
    return insights.build_weekly_plan(path, db.get_progress(learner_id))


@app.get("/api/learners/{learner_id}/achievements", response_model=Achievements)
def get_achievements(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> Achievements:
    _, path = _path_and_profile(catalog, db, learner_id, user)
    return insights.build_achievements(catalog, path, db.get_progress(learner_id))


@app.get("/api/learners/{learner_id}/export", response_class=PlainTextResponse)
def export_path(
    learner_id: str,
    catalog: Catalog = Depends(catalog_dep),
    db: Database = Depends(db_dep),
    user=Depends(current_user),
) -> PlainTextResponse:
    """The whole roadmap as shareable Markdown."""
    _, path = _path_and_profile(catalog, db, learner_id, user)
    progress = db.get_progress(learner_id)
    achievements = insights.build_achievements(catalog, path, progress)
    markdown = insights.export_markdown(path, achievements)
    filename = f"learning-path-{path.goal_id}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------------- static
if FRONTEND_DIR.exists():
    app.mount(
        "/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static"
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))


def _sweep_expired() -> None:
    """Drop dead sessions and spent reset links.

    Both tables only ever grew: `purge_expired` existed from the beginning and
    nothing ever called it. On a free instance that sleeps and wakes this runs
    on every wake, which is often enough for a table that gains a handful of
    rows a day and costs nothing when there is nothing to remove.
    """
    try:
        database = get_db()
        sessions = database.accounts.purge_expired()
        resets = database.accounts.purge_expired_resets()
    except Exception:  # pragma: no cover - never block startup on housekeeping
        log.exception("could not sweep expired sessions and reset links")
        return
    if sessions or resets:
        log.info("swept %d expired session(s) and %d expired reset link(s)",
                 sessions, resets)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    catalog = get_catalog()  # validates; raises loudly if the data is broken
    gate = security.configure()  # re-read the environment after .env is loaded
    status = llm.status()
    log.info(
        "catalog loaded: %d skills, %d items, %d goals",
        len(catalog.skills),
        len(catalog.items),
        len(catalog.goals),
    )
    log.info(
        "assistant: %s",
        f"{status['provider']} ({status['model']})"
        if status["enabled"]
        else "rule engine — no provider credential found",
    )
    log.info(
        "gates: auth %s, rate limiting %s (%d/min, chat %d/min)",
        "required" if gate.api_key else "off",
        "on" if gate.enabled else "off",
        gate.general.limit,
        gate.chat.limit,
    )
    _sweep_expired()
    yield


app.router.lifespan_context = _lifespan
