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
