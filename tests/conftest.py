"""Shared test fixtures.

The LLM is disabled for every test so results are deterministic and no test ever
touches the network or spends tokens. The rule engine is exercised instead —
which is exactly the path that must work when no API key is configured. The
database is pinned to SQLite for the same reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DISABLE_LLM"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

# Empty, not absent: `config.load_dotenv` fills in anything the environment does
# not already define, so popping this would let a real `DATABASE_URL` from .env
# reach the tests and send every query to a Postgres server over the network.
os.environ["DATABASE_URL"] = ""

# The suite fires far more requests per minute than a human ever would, so the
# limiter is off by default here. `tests/test_security.py` turns it back on
# with a tiny budget for the tests that are about the limiter itself.
os.environ["RATE_LIMIT_ENABLED"] = "0"
os.environ.pop("API_KEY", None)

from backend import security  # noqa: E402
from backend.catalog import get_catalog  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.main import app, db_dep  # noqa: E402
from backend.models import LearnerProfile  # noqa: E402
from backend.profiling import derive  # noqa: E402


@pytest.fixture(scope="session")
def catalog():
    return get_catalog()


@pytest.fixture(autouse=True)
def gates_from_env():
    """Every test starts from the environment's gate settings, unconfigured."""
    security.configure()
    yield
    security.configure()


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    app.dependency_overrides[db_dep] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def make_profile(catalog):
    def _make(**kwargs):
        kwargs.setdefault("learner_id", "test-learner")
        profile = LearnerProfile(**kwargs)
        return profile, derive(catalog, profile)

    return _make
