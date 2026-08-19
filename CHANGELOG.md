# Changelog

All notable changes to this project are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.4.0] — 2026-08-19

Accounts that survive a restart, a catalogue that is not only for developers,
and a pass over the interface for clarity rather than motion.

### Added

- **Accounts, sessions and per-user isolation.** Register, sign in, or use the
  app as a guest; every learner, path and message is scoped to one account and
  invisible to any other. A guest can turn into a real account in place,
  keeping everything they have already done.
- **Several paths per learner.** Up to three goals run side by side with one
  active at a time, switchable from the Path tab and visible on the dashboard.
- **The assistant asks before it assumes.** Experience level and weekly hours
  are requested once, up front, because they decide where a path starts and how
  long it runs. Say "you decide" and it chooses, and says which numbers it
  chose. Previously it silently assumed beginner and eight hours a week.
- **Postgres support via `DATABASE_URL`.** The same SQL runs on either engine;
  with the variable unset the app is stdlib `sqlite3` exactly as before. A
  deployment needs it: a free instance has no persistent disk, so a SQLite file
  and every account in it are erased whenever the instance sleeps. Includes a
  one-shot retry without `channel_binding` for hosts that refuse it, and
  reconnection when a serverless database suspends an idle connection.
- **Five domains outside software** — Business & Product, Marketing, Design,
  Finance & Accounting, and Mathematics — bringing the catalogue to 85 skills,
  139 items and 14 goals. Mathematics is a *subject* rather than a career,
  which the catalogue previously had no example of.
- **Empty tabs now offer a way forward.** "Set a goal in the Chat tab first"
  was a dead end on five of the seven tabs and the first thing a visitor saw.
  Each one now builds a worked example in one click, or jumps to the chat.
- **`storage` in `/api/health`**, so a deployment can be checked rather than
  assumed. `sqlite` on a disk-less host means state is lost on every sleep.

### Fixed

- **The dashboard overflowed into itself.** `.badge` was two components at
  once: the one-line status pills in the header, which set
  `white-space: nowrap`, and the multi-line achievement tiles. The tiles
  inherited the nowrap and their descriptions ran through the card beside them
  and off the panel, so the whole page scrolled sideways at narrow widths.
  The tiles now carry their own class.
- **`Rename`, `New learner` and `Save my work` did nothing.** All three were in
  the markup, styled, and bound to no handler at all — `Save my work` had a
  complete `upgradeGuest()` implementation that nothing ever called. A test now
  fails if any button in the page has no handler.
- **A blank pill floated at the bottom of every page.** The toast hid itself
  with `translateY(120%)`, and 120% of an empty ~18px box does not clear a
  1.5rem inset.
- **Four database round trips ran one after another on every page load.** The
  path, the path list and the conversation are independent; only the profile
  has to come first. Invisible against a local file, about six seconds against
  a database in another region.

### Changed

- **A type scale with actual steps in it.** Headings ran .95rem to 1.05rem
  against 15px body text, so nothing led the eye and every card carried the
  same weight. Cards are now separated by their border rather than by eight
  identical shadows, and each tab states what it is.
- **The header is grouped**, into the engine, the learner, and the account,
  instead of eight controls in one undifferentiated row.
- **`render.yaml` deploys to `ohio`**, matching the region of the database it
  is pointed at. Serving from Singapore against a `us-east-2` database put a
  cross-planet round trip on every query.

## [1.3.0] — 2026-08-18

Deployment readiness, and a conversation layer that survives real users.

### Added

- **Typo and shorthand tolerance.** Messages are normalised (shorthand expanded,
  punctuation stripped) and goal keywords matched fuzzily, so
  "i wnat to be data analist" builds the right path. Clean spelling still scores
  higher than a fuzzy hit.
- **Small talk that is actually useful.** Greetings, "help", "what is this",
  "i don't know what i want", thanks, and frustration each get a specific answer
  instead of the goal-clarification question.
- **Goal switching from a plain statement.** Naming a different goal switches to
  it; *asking* about one ("should I be a data analyst?") does not.
- **Skill claims mid-conversation.** "I already know SQL" credits the skill and
  rebuilds the path, rather than telling the learner to go and edit a form.
- **An answer for "is this too much for me"**, grounded in real hours and the two
  levers that change the plan.
- `render.yaml` — a one-click Render blueprint with the gates tightened for a
  public demo.
- `tests/test_conversation.py` — 32 tests, all against the rule engine.

### Changed

- **The default Gemini model is now `gemini-flash-lite-latest`.** The flagship
  model allows roughly twenty free requests a day, which a public demo exhausts
  immediately; the lite tier is far larger, and the `-latest` alias cannot be
  retired out from under the app the way a pinned id can.
- **The container honours `$PORT`.** It hardcoded 8000, so every hosting
  platform would have started it and then failed the health check.
- Replies no longer read "Priya, Start with …" — the opener lowercases after a
  name.

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
