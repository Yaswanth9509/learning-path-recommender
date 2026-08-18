# Changelog

All notable changes to this project are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.2.0] — 2026-08-18

Closes the four capabilities the brief names that a static roadmap does not
cover: learning history, learning patterns, resources, and asking rather than
guessing.

### Added

- **Prior learning history.** The Profile tab lists every course in the catalog
  so a learner can tick what they finished before this app existed. Those
  completions leave the gap, are never scheduled, and now survive a path rebuild
  — `sync_completions` gives the progress table ownership only of the items it
  actually tracks.
- **Observed pace.** `insights.build_pace` measures the real study rate from the
  timestamps the progress table already kept, and reports `behind` / `on_track`
  / `ahead` — or `unknown` below a week of history rather than inventing a trend.
  Surfaced on the dashboard and at `GET /api/learners/{id}/pace`.
- **`POST /api/learners/{id}/replan`** refits the schedule to that observed rate.
  Deliberately explicit: ticking a box still never reshuffles the plan.
- **Reference resources.** A fourth item type carrying real canonical links
  (MDN, the Python tutorial, Pro Git, the RAG paper). Recommendable and
  filterable, never scheduled — coverage is only ever satisfied by a course, and
  the teachability invariant now excludes resources explicitly.
- **Item links everywhere.** Catalog entries expose `url`; the UI links to it, or
  offers a search when a provider publishes no canonical page.
- **Clarifying questions.** A vague goal ("I want to work with data") is now
  asked about, with the distinguishable readings offered as one-click replies,
  instead of being guessed at. A provider's own confident reading still wins.
- `tests/test_adaptation.py` — 30 tests covering all four.
- `docs/architecture.svg`, and a screenshots section in the README.

### Changed

- The frontend contract test now forbids remote *assets* rather than all remote
  URLs, and asserts outbound links carry `rel="noopener noreferrer"`.

## [1.1.0] — 2026-08-17

Release-readiness pass: the application behaviour is unchanged, but it can now
be run, verified, and deployed by someone other than its author.

### Added

- **Request gates** (`backend/security.py`): optional `API_KEY` for every `/api`
  route except `/api/health`, per-caller sliding-window rate limiting with a
  separate and much tighter budget for `POST /api/chat`, configurable CORS
  origins, and `nosniff` / `DENY` / `same-origin` response headers. All of it is
  off or generous by default, so the local demo still needs no configuration.
- **Payload bounds** on everything a client can send: chat messages, learner
  ids, profile names, feedback notes, and list lengths.
- **Gap explanation in the UI** — the path tab now shows the engine's
  plain-language reading of the gap from `/api/learners/{id}/gap`.
- **"Why this?" on every item** — a modal built on
  `/api/learners/{id}/explain/{item_id}` showing each score component, its
  weight, the resulting contribution, and where the planner placed the item.
  Both endpoints existed but nothing in the UI called them.
- **Frontend contract tests** (`tests/test_frontend.py`): every selector
  resolves, every endpoint the client calls exists, and no asset is loaded from
  a remote host.
- **Security tests** (`tests/test_security.py`) and a gap-endpoint test.
- **Packaging and tooling**: `pyproject.toml` (ruff, pytest, coverage),
  `requirements-dev.txt`, pinned runtime dependencies with upper bounds.
- **CI** (`.github/workflows/ci.yml`): lint, catalog validation, and tests on
  Python 3.10–3.12 plus Windows; the Docker image is built and booted, and its
  `/api/health` checked.
- **Dockerfile** — non-root, state on a volume, with a healthcheck.
- `LICENSE` (MIT), `CONTRIBUTING.md`, and this changelog.

### Changed

- `PATCH /profile` now rejects out-of-range `weekly_hours` with 422 instead of
  silently clamping it.
- The Anthropic SDK moved out of the default install: no provider needs a
  package, so `pip install -r requirements.txt` installs three dependencies.
  `pip install "anthropic>=0.40,<1.0"` enables the SDK path.
- Unhandled exceptions return a plain JSON 500 and are logged server-side.

### Fixed

- README test counts and file listings had drifted from the code.

## [1.0.0]

Initial application: conversational goal capture, learner profiling,
prerequisite-ordered path generation, explainable recommendations, progress and
feedback adaptation, dashboard, skill graph, weekly plan, achievements, and
Markdown export.
