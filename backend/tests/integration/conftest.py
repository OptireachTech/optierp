"""Shared fixtures for Module 02 integration tests (PostgreSQL required).

Skipped unless TEST_DATABASE_URL is set; each test gets a clean schema state,
a seeded company (India COA) and an authenticated superuser client.

Performance note (see docs/CI_TEST_PERFORMANCE_PLAN.md Phases 1-2): the schema
itself — tables, extensions, GL triggers — is built exactly once per pytest
session (once per worker under `pytest -n auto`), not once per test. ~230
tests across this package share this `ctx` fixture; rebuilding 241 tables via
DROP SCHEMA CASCADE + create_all on every single one of them was the dominant
cost in CI. Between tests we TRUNCATE instead of rebuilding — see the comment
on `_reset_data` for why TRUNCATE and not DELETE. Under xdist, each worker
gets its own schema (`_SCHEMA` below) instead of sharing `public`, so parallel
workers can't collide on the same tables.
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

TEST_DB = os.environ.get("TEST_DATABASE_URL")

PW = "Passw0rd!xyz"

# docs/CI_TEST_PERFORMANCE_PLAN.md Phase 2: under plain `pytest` this is "public",
# unchanged from before xdist existed. Under `pytest -n auto`, tests/conftest.py has
# already pointed this worker's `app.core.database.engine` at a private search_path of
# the same name (DB_SEARCH_PATH), so every unqualified table `_build_schema`/create_all
# creates lands here instead of colliding with another worker's "public". The
# `statutory` schema stays shared/unparameterized on purpose — it's a read-only
# catalogue (see app/models/statutory.py's docstring); nothing in this suite writes to
# it, so concurrent workers truncating an already-empty shared table is a no-op race,
# not a correctness hazard.
_SCHEMA = f"test_{os.environ['PYTEST_XDIST_WORKER']}" if os.environ.get("PYTEST_XDIST_WORKER") else "public"

# Set once the schema (tables/extensions/triggers) has been built in this process.
# A plain module-level flag is correctly scoped even under xdist: each worker is its
# own OS process with its own module state, so "built once in this process" already
# means "built once per worker" — no change needed here for that, only for _SCHEMA
# above (the name each worker builds).
_schema_ready = False


async def _build_schema(conn: AsyncConnection) -> None:
    """Create the schema from scratch. Runs once per test session, not per test."""
    # drop_all can't order DROPs across FK cycles (unnamed use_alter
    # constraints) — recreating the schema wholesale is cycle-proof
    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    await conn.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS ltree"))
    # The statutory catalogue lives in its own schema (migration 0085) and
    # create_all only creates tables, never the schema that holds them.
    await conn.execute(text("CREATE SCHEMA IF NOT EXISTS statutory"))

    from app.models.base import Base

    await conn.run_sync(Base.metadata.create_all)
    # the GL triggers live in migration 0002; create_all doesn't know them
    await conn.execute(text(
        """
        CREATE OR REPLACE FUNCTION fn_gl_entry_balance_check() RETURNS trigger AS $$
        DECLARE diff NUMERIC;
        BEGIN
          SELECT COALESCE(SUM(debit) - SUM(credit), 0) INTO diff
            FROM gl_entries
           WHERE voucher_type = NEW.voucher_type AND voucher_id = NEW.voucher_id;
          IF ABS(diff) > 0.005 THEN
            RAISE EXCEPTION 'GL voucher % is out of balance by %', NEW.voucher_no, diff;
          END IF;
          RETURN NULL;
        END $$ LANGUAGE plpgsql
        """
    ))
    await conn.execute(text(
        "CREATE CONSTRAINT TRIGGER trg_gl_entry_balance_check AFTER INSERT ON gl_entries "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_gl_entry_balance_check()"
    ))
    await conn.execute(text(
        """
        CREATE OR REPLACE FUNCTION fn_gl_entry_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'gl_entries is append-only: % not allowed', TG_OP;
        END $$ LANGUAGE plpgsql
        """
    ))
    await conn.execute(text(
        "CREATE TRIGGER trg_gl_entry_immutable BEFORE UPDATE OR DELETE ON gl_entries "
        "FOR EACH ROW EXECUTE FUNCTION fn_gl_entry_immutable()"
    ))


async def _reset_data(conn: AsyncConnection) -> None:
    """Wipe all rows between tests, leaving tables/extensions/triggers in place.

    TRUNCATE, not DELETE: gl_entries has a BEFORE UPDATE OR DELETE trigger
    (fn_gl_entry_immutable, by design — it's an append-only ledger) that
    raises on any DELETE. TRUNCATE doesn't fire row-level triggers in
    Postgres, so it's not just faster than a per-table DELETE here, DELETE
    would actively fail. RESTART IDENTITY reproduces the "fresh sequences"
    behaviour tests got for free from the old drop/recreate.
    """
    from app.models.base import Base

    tables = ", ".join(
        f'"{t.schema}"."{t.name}"' if t.schema else f'"{t.name}"'
        for t in Base.metadata.sorted_tables
    )
    await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


async def ensure_schema(conn: AsyncConnection) -> None:
    """Build the schema once per session; TRUNCATE (fast) on every later call.

    Single shared decision point for "build vs. reset", used by every fixture
    in this package that needs a fresh-looking schema — not just ``ctx``
    below, but also test_module01_flow.py's own ``client`` fixture. That file
    used to do its own DROP-SCHEMA-CASCADE-then-create_all, independently of
    ``ctx`` and missing the GL triggers / statutory schema entirely — safe
    only because the old ``ctx`` unconditionally rebuilt everything on every
    call, silently repairing whatever the other fixture had just torn down.
    Once ``ctx`` stopped doing that (this file, Phase 1), that cross-file
    repair disappeared, so both fixtures now go through this one function
    instead of duplicating (and drifting from) the build logic.
    """
    global _schema_ready
    if not _schema_ready:
        await _build_schema(conn)
        _schema_ready = True
    else:
        await _reset_data(conn)


@pytest_asyncio.fixture()
async def ctx():
    """Schema + seeded company/admin; returns (client, company, headers)."""
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")

    from app.core.database import async_session_factory, engine
    from app.core.security import hash_password
    from app.main import app
    from app.models.core import Currency, Role, User, UserRole

    async with engine.begin() as conn:
        await ensure_schema(conn)

    async with async_session_factory() as db:
        db.add(Currency(code="INR", currency_name="Indian Rupee", symbol="₹"))
        db.add(Role(name="System Manager", is_system=True))
        admin = User(email="admin@test.io", first_name="Admin", hashed_password=hash_password(PW))
        db.add(admin)
        await db.flush()
        db.add(UserRole(user_id=admin.id, role="System Manager", company_id=None))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/auth/login", json={"email": "admin@test.io", "password": PW})
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        resp = await client.post(
            "/api/v1/companies",
            json={"company_name": "Acme India", "abbr": "ACME", "default_currency": "INR",
                  "country_code": "IN"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        company = resp.json()

        # re-login so the JWT carries the new company context
        resp = await client.post(
            "/api/v1/auth/switch-company", json={"company_id": company["id"]}, headers=headers
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        yield client, company, headers
    await engine.dispose()


async def coa_account(client: AsyncClient, company: dict, headers: dict, name: str) -> dict:
    """Find an account by name in the company's chart of accounts."""
    resp = await client.get(f"/api/v1/companies/{company['id']}/chart-of-accounts", headers=headers)
    return next(a for a in resp.json() if a["account_name"] == name)
