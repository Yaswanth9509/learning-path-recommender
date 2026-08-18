# Contributing

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt
python run.py                 # http://127.0.0.1:8000
```

Python 3.10 or newer. No API key is needed — without one the assistant runs on
the deterministic rule engine, which is the path the whole test suite exercises.

## Before every commit

```bash
python -m ruff check .        # lint (CI runs the same command)
python -m pytest --cov        # 284 tests, offline, ~2s
```

CI runs both on Python 3.10–3.12 plus a Windows job, builds the Docker image,
and boots it to check `/api/health`. Coverage below 80% fails the build.

## Where things live

| Layer | Files | What to remember |
|---|---|---|
| Catalog data | `backend/data/*.json` | Validated at import; run the tests after any edit |
| Engines | `catalog, profiling, gap, recommender, pathgen, dashboard, insights` | Pure functions over the catalog — no HTTP, no DB |
| Assistant | `assistant.py`, `llm.py` | Every LLM path needs a deterministic fallback |
| HTTP | `main.py`, `security.py`, `db.py` | Routes stay thin; logic belongs in an engine |
| Frontend | `frontend/*` | Vanilla JS, no build step, no external requests |

## The invariants that must not break

1. **Prerequisites are never violated.** `assert_prerequisites_respected` walks
   a path in learner order and fails if an item appears before the skills it
   requires. Any change to `pathgen.py` must keep this true for all 9 goals × 3
   levels.
2. **Generation is deterministic.** Identical input, identical path. Break ties
   with explicit sort keys, never with set or dict ordering.
3. **The app works with no LLM.** Anything an LLM does, the rule engine must
   also do. Tests run with `DISABLE_LLM=1` and never touch the network.
4. **The catalog is always consistent.** New skills need a course that teaches
   them; new prerequisites must not create a cycle. The validator enforces
   both — extend `test_catalog.py` when you add a new class of constraint.
5. **The frontend calls nothing remote.** `test_frontend.py` fails on any
   external URL, on a selector that no element defines, and on a call to an
   endpoint the API does not serve.

## Adding to the catalog

Edit `backend/data/{skills,courses,goals}.json`, then run the tests. Typical
failures and what they mean:

- *"skill X is taught by no item"* — add a course that teaches it, or the path
  generator could produce an unfillable gap.
- *"cycle detected"* — two skills list each other, directly or transitively.
- *"item requires a skill that depends on what it teaches"* — the item cannot
  be scheduled anywhere; split it.

## Style

Ruff enforces the rules in `pyproject.toml`. Beyond that, match what is already
there: comments explain *why*, docstrings describe behaviour rather than
restating signatures, and tests are named as the sentence they prove.
