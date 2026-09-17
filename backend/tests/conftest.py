"""Test configuration.

Environment defaults are set BEFORE app modules import settings, so the
suite runs without a .env file. Integration tests need a real PostgreSQL —
point TEST_DATABASE_URL at one (they self-skip otherwise):

    TEST_DATABASE_URL=postgresql+asyncpg://erp_owner:erp_owner_pw@localhost:5432/erp_test
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789")
_test_db = os.environ.get("TEST_DATABASE_URL")
os.environ.setdefault(
    "DATABASE_URL", _test_db or "postgresql+asyncpg://erp_app:erp@localhost:5432/erp"
)
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("REFRESH_COOKIE_SECURE", "false")
# Off by default: a real Redis in CI would otherwise throttle the many
# unrelated /auth/login calls the ctx fixture makes across the whole
# integration suite. tests/integration/test_login_rate_limit.py turns it
# back on for itself only.
os.environ.setdefault("AUTH_RATE_LIMIT_ENABLED", "false")

# docs/CI_TEST_PERFORMANCE_PLAN.md Phase 2: under `pytest -n auto`, pytest-xdist sets
# PYTEST_XDIST_WORKER (e.g. "gw0") in each worker's own process before this module is
# ever imported. Give each worker's connections a private schema via DB_SEARCH_PATH
# (app/core/database.py reads it into `search_path`) so concurrent workers' schema
# builds/truncates (tests/integration/conftest.py) can't collide on a shared `public`.
# Unset (plain `pytest`, no -n) -> DB_SEARCH_PATH stays unset -> unchanged, "public".
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER")
if _xdist_worker:
    os.environ.setdefault("DB_SEARCH_PATH", f"test_{_xdist_worker}")
