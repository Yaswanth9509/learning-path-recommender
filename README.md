# AI-Powered Personalised Learning Path Recommender

A learning assistant that takes a goal stated in plain language and returns a
**prerequisite-ordered roadmap** of courses, projects, and assessments — with an
explanation for every item, a dashboard for progress, and adaptation from
learner feedback.

The core idea: recommending *relevant* courses is easy and not the problem.
Learners get stuck on **sequence**. So the path is generated from a prerequisite
DAG and a topological sort, not from a similarity ranking. An item can never be
scheduled before the things it depends on — that property is asserted in the
test suite across every goal and learner shape.

---

## Quick start

Python 3.10 or newer.

```bash
pip install -r requirements.txt
python run.py
# open http://127.0.0.1:8000
```

That's it. **No API key is required** — see [Atlas](#atlas--the-assistant-and-the-provider-layer).
Three dependencies, no build step, no external services.

Run the tests:

```bash
pip install -r requirements-dev.txt
python -m pytest              # 315 tests, ~3s, fully offline
python -m ruff check .        # lint
```

Or in Docker:

```bash
docker build -t learning-path-recommender .
docker run -p 8000:8000 -v lpr-data:/data learning-path-recommender
```

---

## What it does

Type something like:

> *"I'm a beginner and I want to become an AI engineer building LLM apps with
> retrieval. I already know Python and SQL. I can study 12 hours a week and
> prefer hands-on projects."*

and the system:

1. **Interprets** the goal, experience level, weekly hours, known skills, and
   format preferences.
2. **Profiles** the learner — including *implied* skills: declaring Deep
   Learning means you are never sent back to learn Python.
3. **Analyses the gap** between what you hold and the transitive closure of what
   the goal requires.
4. **Generates the path**: topologically sorts the missing skills, greedily
   covers each with the best-fitting course, groups them into milestones, and
   caps each milestone with a project and an assessment.
5. **Explains** every placement, and answers follow-up questions.
6. **Adapts**: mark something *too easy* / *too hard* / *not relevant* and the
   path is rebuilt around the change.

If the goal is vague — *"I want to work with data"* — it **asks instead of
guessing**, offering the readings it can distinguish. A wrong roadmap costs a
learner months, so an unanswered question is cheaper than a confident mistake.

Example output for the goal above (12 h/week, knows Python + SQL):

```
Stage 1 · Foundations: JavaScript & Data Structures (+3 more)          158h
    core        Modern JavaScript
    core        Data Structures & Algorithms in Python
    core        Statistics & Probability for Data
    core        Applied Machine Learning End-to-End
    assessment  Assessment: ML Fundamentals Exam
Stage 2 · Web Development: Node.js & REST API Design (+1 more)         62h
Stage 3 · Data & AI: Deep Learning & Responsible AI (+1 more)         124h
Stage 4 · Data & AI: Vector Search & LLM Engineering (+1 more)        101h
    core        Retrieval-Augmented Generation Systems
    project     Project: RAG Knowledge Assistant
    assessment  Assessment: LLM Engineering Capstone Review
```

Note stage 2: Node.js and REST API design appear on an *AI* path because
`llm-engineering` depends on `rest-api`. That edge is in the graph, so the
planner finds it — a keyword recommender never would.

---

## The four screens

| Screen | What it shows |
|---|---|
| **Chat** | A goal in plain language, and the system's reading of it — level, hours, skills already held |
| **Path** | Milestones with hours, estimated weeks, and the rationale for every placement |
| **Skill graph** | The prerequisite DAG the roadmap was generated from, in dependency columns |
| **Dashboard** | Progress, skill development, observed pace, achievements, and next actions |

<!-- Screenshots: capture from a running instance (`python run.py`), save as
     docs/screenshot-{chat,path,graph,dashboard}.png, then uncomment this block.

| | |
|---|---|
| ![Conversational goal capture](docs/screenshot-chat.png) | ![The prerequisite-ordered roadmap](docs/screenshot-path.png) |
| ![The prerequisite graph](docs/screenshot-graph.png) | ![The dashboard](docs/screenshot-dashboard.png) |
-->

---

## Architecture

![Architecture](docs/architecture.svg)

```
Natural language goal
        │
        ▼
  assistant.py ──── Claude (or the built-in rule engine)
        │  goal_id, level, hours, known skills, formats
        ▼
  profiling.py ──── direct skills + prerequisite-implied skills
        │
        ▼
    gap.py ──────── required = closure(goal targets)
        │           missing  = required − known
        ▼
  pathgen.py ────── topological_layers(missing)
        │           ├─ greedy course selection per layer
        │           ├─ chunk into 3–6 milestones
        │           └─ attach project + assessment per milestone
        ▼
  LearningPath ──── milestones, hours, weeks, rationale per item
        │
        ├──► recommender.py   six-component explainable scoring
        └──► dashboard.py     progress, skills, milestones, next actions
```

| Module | Responsibility |
|---|---|
| `catalog.py` | Loads and **validates** the catalog; graph queries (closure, topological layers, depth) |
| `profiling.py` | Learner profiling engine; folds feedback into the profile |
| `gap.py` | Skill gap analysis and its plain-language explanation |
| `recommender.py` | Weighted, explainable scoring of every catalog item |
| `pathgen.py` | Milestone path generation with prerequisites |
| `dashboard.py` | Progress, skill development, and next actions |
| `assistant.py` | Atlas: NL understanding and Q&A, with a full rule-based fallback |
| `llm.py` | Provider-agnostic LLM layer (Anthropic / Gemini / Groq / OpenRouter / Ollama) |
| `insights.py` | Skill graph, weekly plan, achievements, pace, Markdown export |
| `db.py` | SQLite persistence (stdlib only) |
| `security.py` | Optional API key, rate limiting, response headers |
| `main.py` | FastAPI routes + static hosting |

---

## Beyond the brief

Four features that make the underlying model visible rather than implied:

**Interactive skill graph** (`/api/learners/{id}/graph`) renders the actual
prerequisite DAG as an SVG, laid out in dependency columns. Every arrow is a
constraint the planner obeyed; click any skill to see what it needs, what it
unlocks, and which course covers it. This is the product's core argument made
visual — and a test asserts every edge points strictly forward, so the drawing
can never contradict the plan.

**Weekly plan** (`/api/learners/{id}/plan`) pours the remaining roadmap into
fixed-capacity weeks, splitting long courses across them. A "445-hour path"
becomes "week 3: wrap up Modern JavaScript, start Data Structures". Tests assert
no week exceeds capacity and that path order is preserved exactly.

**Achievements** (`/api/learners/{id}/achievements`) — XP at 10 per completed
hour, seven levels, and nine badges tied to real milestones (first item, stage
cleared, foundations mastered, project shipped, assessment passed, 100 hours,
whole path). Unearned badges show what would earn them.

**Markdown export** (`/api/learners/{id}/export`) emits the whole roadmap as a
shareable document with task checkboxes, per-item rationale, and earned badges.

---

## It learns how you actually study, not how you said you would

Every learner overestimates their weekly hours. The progress table already
records *when* each item changed state, so the real rate is measurable — and the
difference between the two numbers is the only honest evidence of how someone
studies.

`GET /api/learners/{id}/pace` reports it: hours planned versus hours observed,
days tracked, and the weeks remaining at each rate. Below a week of history it
says `unknown` rather than inventing a trend from one afternoon.

```
You planned 20h a week and are averaging 4.2h. At your real rate the rest takes
about 61 weeks rather than 12.8. Re-planning around 4h would give you dates you
can actually trust.
```

`POST /api/learners/{id}/replan` acts on it, refitting the whole schedule to the
observed rate. It is deliberately a separate, explicit action: ticking a box
never reshuffles a learner's plan underneath them, but asking for a re-plan
should change everything downstream of it.

**Prior learning history.** The Profile tab lists every course in the catalog,
so a learner arriving with ten years of experience can tick what they have
already done. Those completions remove skills from the gap, are never scheduled
again, and — unlike progress entries — survive every rebuild, because history
that predates the app must not be erased by it.

**Reference resources.** Alongside courses, projects and assessments, the
catalog carries reference material with real, canonical links (MDN, the Python
tutorial, Pro Git, the RAG paper). Resources are recommendable and filterable
but are *never* scheduled: path coverage is only ever satisfied by a course, and
a test asserts it for every goal.

---

## The recommendation score is not a black box

Every recommendation is a weighted sum of six independently computed
components, all returned to the client:

| Component | Weight | Meaning |
|---|---:|---|
| `goal_relevance` | 0.35 | Fraction of the item's value landing on skills you still need |
| `skill_readiness` | 0.20 | Fraction of its prerequisites you already hold |
| `level_fit` | 0.15 | Closeness to your difficulty target (feedback-adjusted) |
| `interest_match` | 0.15 | Domain overlap with your stated interests |
| `format_fit` | 0.10 | Format, cost, and provider preferences |
| `quality` | 0.05 | Rating, with popularity as a weak tiebreaker |

The same numbers that produce the ranking generate the human-readable reasons,
so *"why was this recommended?"* always has a precise, checkable answer.
`GET /api/learners/{id}/explain/{item_id}` returns the full breakdown plus where
the item sits in the path and why — and every item in the UI, on the path and in
the recommendations list alike, has a **Why this?** button that opens it:
each component's score, its weight, the product of the two, and the placement
rationale.

---

## Atlas — the assistant, and the provider layer

The assistant is **Atlas**, a coaching persona rather than a query responder. It
opens with the answer, cites the learner's own courses and hours, and closes
with one concrete nudge instead of a menu.

### Bring your own provider (or none)

The app is not tied to a vendor. [`llm.py`](backend/llm.py) adapts a single
`complete()` call to whichever provider is configured, auto-detected in this
order:

| Provider | Env var | Cost | Notes |
|---|---|---|---|
| Anthropic Claude | `ANTHROPIC_API_KEY` | Paid credits | Native structured outputs, refusal fallbacks |
| **Google Gemini** | `GEMINI_API_KEY` | **Free tier, no card** | Recommended free option |
| **Groq** | `GROQ_API_KEY` | **Free tier** | Very fast open models |
| OpenRouter | `OPENROUTER_API_KEY` | Free `:free` models | Many models, one endpoint |
| **Ollama** | `LLM_PROVIDER=ollama` | **Free forever** | Fully local, no key, works offline |

Set `LLM_PROVIDER` to pin one explicitly, or `DISABLE_LLM=1` to force the rule
engine. Every model id is overridable (`GEMINI_MODEL`, `GROQ_MODEL`, …).

Groq, OpenRouter, Together, vLLM and LM Studio all speak the OpenAI wire format,
so one adapter covers them. **No provider adds a dependency** — the whole layer
is stdlib `urllib`.

### The fallback is not a stub

Goal understanding and Q&A are implemented **twice**. Without a provider, the
rule engine handles both — and it is written to sound like a coach, not a form
letter: it varies phrasing, names the learner, and cites their actual stage and
progress. The variation is seeded from the input rather than randomness, so it
is still fully deterministic and testable.

Everything a provider returns is treated as untrusted: unknown goal or skill
ids are discarded, out-of-range hours rejected, invalid levels and formats
filtered, and JSON is recovered from markdown fences or surrounding prose.
Any failure — no credential, network error, rate limit, refusal, malformed
output — degrades to the rule engine rather than surfacing an error. The UI
badge and `/api/health` always report which engine actually answered.

```bash
cp .env.example .env      # then set one provider key
```

---

## Serving it to other people

Locally the app needs no configuration. The moment it is reachable by anyone
else, four things matter, and all four are environment variables — no code
changes:

| Variable | Default | Set it to |
|---|---|---|
| `API_KEY` | unset | a shared secret; then every `/api` route except `/api/health` needs `X-API-Key` or `Authorization: Bearer` |
| `ALLOWED_ORIGINS` | `*` | the exact browser origins you serve from |
| `RATE_LIMIT_PER_MINUTE` | `240` | per-caller budget across the API |
| `RATE_LIMIT_CHAT_PER_MINUTE` | `20` | budget for `POST /api/chat` alone |

`/api/chat` gets its own, much tighter budget: it is the only route that spends
provider tokens, and the only one that creates a learner record for an id it
has not seen. Ids are pattern-checked, messages are capped at 2000 characters,
and every response carries `nosniff`, `DENY`, and `same-origin`. Behind a proxy,
set `TRUST_PROXY_HEADER=1` so the limiter keys on the real client IP — and only
there, since an exposed app would let callers forge that header.

`/api/health` stays public so probes need no secret, and it reports which gates
are active.

The Docker image runs as a non-root user with the SQLite file on a volume:

```bash
docker build -t learning-path-recommender .
docker run -p 8000:8000 -v lpr-data:/data --env-file .env learning-path-recommender
```

State is one SQLite file (`LEARNING_DB_PATH`, `/data/learning.db` in the image).
Deleting it resets the application; there is nothing else to migrate or back up.

---

## Catalog integrity

The catalog (48 skills, 90 learning items, 9 career goals) is validated
exhaustively at import time. `CatalogError` is raised for:

- a skill prerequisite that does not resolve, or a skill requiring itself
- **any cycle** in the prerequisite graph (Kahn's algorithm)
- a learning item referencing an unknown skill
- an item that both teaches and requires the same skill
- an item requiring a skill that transitively depends on something it teaches
- **any skill no course teaches** — otherwise a path could contain an unfillable gap
- a goal targeting an unknown skill
- out-of-range levels, ratings, hours, formats, costs, or duplicate ids

This runs at startup and in `run.py`, so broken data fails loudly and
immediately instead of producing a subtly wrong roadmap.

To extend the catalog, edit `backend/data/{skills,courses,goals}.json` and run
the tests — the validator and the path invariants will catch inconsistencies.

---

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Status, catalog size, live assistant engine, gate settings |
| `GET` | `/api/catalog/{goals,skills,items}` | Browse the catalog |
| `GET/POST` | `/api/learners` | List learners / create one |
| `DELETE` | `/api/learners/{id}` | Delete a learner and everything of theirs |
| `GET/PATCH` | `/api/learners/{id}/profile` | Read/update the profile (rebuilds the path) |
| `GET` | `/api/learners/{id}/profile/summary` | Profiling-engine output, incl. implied skills |
| `POST` | `/api/chat` | Conversational interface — states a goal or asks a question |
| `GET/DELETE` | `/api/learners/{id}/conversation` | Chat history; clear it |
| `GET/POST` | `/api/learners/{id}/path` | Fetch or regenerate the learning path |
| `GET` | `/api/learners/{id}/gap` | Skill gap report + explanation |
| `GET` | `/api/learners/{id}/recommendations` | Ranked, explained recommendations |
| `GET` | `/api/learners/{id}/explain/{item_id}` | Full justification for one item |
| `GET/POST` | `/api/learners/{id}/progress` | Track item status |
| `GET/POST` | `/api/learners/{id}/feedback` | Adapt the path from feedback |
| `GET` | `/api/learners/{id}/dashboard` | Progress, skills, milestones, next actions, pace |
| `GET` | `/api/learners/{id}/pace` | Observed study rate versus the planned one |
| `POST` | `/api/learners/{id}/replan` | Refit the schedule to the observed rate |
| `GET` | `/api/learners/{id}/graph` | Prerequisite DAG, laid out in dependency layers |
| `GET` | `/api/learners/{id}/plan` | Week-by-week schedule at the learner's real hours |
| `GET` | `/api/learners/{id}/achievements` | XP, level, and badges |
| `GET` | `/api/learners/{id}/export` | The roadmap as shareable Markdown |

Interactive docs at `/docs` while the server runs.

---

## Testing

315 tests at 90% branch coverage, all offline and deterministic — no network,
no API key, no tokens. CI runs them on Python 3.10–3.12 and on Windows, lints
with ruff, validates the catalog, then builds the Docker image and boots it.

| File | Covers |
|---|---|
| `test_catalog.py` | Catalog integrity, graph properties, and **negative cases** — cycles, dangling refs, unteachable skills are each proven to raise |
| `test_pathgen.py` | The prerequisite invariant across all 9 goals × 3 levels, full gap coverage, no duplicates, determinism, exclusions, the no-gap case |
| `test_engines.py` | Profiling, implied skills, gap analysis, score bounds, ranking, feedback adaptation, rule-based NL understanding |
| `test_llm_providers.py` | Each adapter's exact request shape and response parsing, JSON recovery from fences and prose, provider priority and selection |
| `test_assistant_llm.py` | The LLM branch driven by a stubbed provider: hallucinated ids, out-of-range values, prose instead of JSON, and total API failure all degrade correctly |
| `test_insights.py` | Graph edges point forward, weekly plan never exceeds capacity, achievements are monotonic, export escapes table pipes |
| `test_api.py` | Every endpoint, validation errors, and a full learner journey: chat → path → progress → feedback → dashboard |
| `test_security.py` | The limiter's sliding window, the API-key gate, payload bounds, and the response headers |
| `test_frontend.py` | Every selector resolves, every endpoint the client calls exists, and nothing is loaded from a remote host |
| `test_adaptation.py` | Prior history survives rebuilds, pace is measured and re-planned around, resources are never scheduled, vague goals are asked about |

The load-bearing test is `assert_prerequisites_respected` — it walks the path in
the exact order a learner would and asserts no item is ever reached before the
skills it requires have been taught.

---

## Design decisions worth flagging

**Uncoverable skills cascade.** If the only course teaching a skill is excluded
by learner feedback, that skill *and everything downstream of it* are reported
as uncovered rather than scheduled anyway. Promising a course whose prerequisite
the path never teaches would be worse than admitting the gap.

**Progress survives rebuilds.** Completing a course removes its skill from the
gap, so the next rebuild drops that course from the plan. The dashboard still
counts it — a learner's finished work must not vanish because the path moved on.

**Ticking a box does not reshuffle the plan.** Progress updates mutate status in
place; only profile changes and feedback trigger regeneration.

**Generation is deterministic.** Identical inputs always produce an identical
path — ties are broken by explicit sort keys, never by dict ordering.

---

## Project layout

```
backend/
  data/{skills,courses,goals}.json   catalog data
  catalog.py  profiling.py  gap.py
  recommender.py  pathgen.py  dashboard.py  insights.py
  assistant.py  llm.py  security.py
  models.py  db.py  config.py  main.py
frontend/
  index.html  styles.css  app.js     no build step, no CDN
tests/
  conftest.py  test_catalog.py  test_pathgen.py  test_engines.py
  test_llm_providers.py  test_assistant_llm.py  test_insights.py
  test_api.py  test_security.py  test_frontend.py  test_adaptation.py
docs/architecture.svg                diagram + screenshots
.github/workflows/ci.yml             lint, tests, catalog check, Docker boot
Dockerfile  pyproject.toml  run.py  make_zip.py
requirements.txt  requirements-dev.txt  .env.example
LICENSE  CONTRIBUTING.md  CHANGELOG.md
```

The frontend is dependency-free vanilla JS and is theme-aware (light/dark).

---

## Author

Vangara Yaswanth Sai

Full write-up in [docs/solution-documentation.html](docs/solution-documentation.html) —
problem understanding, solution approach, architecture, AI/ML techniques, key
features and workflows, and challenges faced.
