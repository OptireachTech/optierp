"""Integration test — Module 01 end-to-end flow against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL is set (use the owner role so create_all works):

    TEST_DATABASE_URL=postgresql+asyncpg://erp_owner:pw@localhost:5432/erp_test pytest tests/integration

Covers: schema creation, seeding, login, company creation (COA + cost
centers + fiscal year + default accounts), naming series atomicity and a
permission-denied path.
"""

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@pytest_asyncio.fixture()
async def client():
    from app.core.database import async_session_factory, engine
    from app.core.security import hash_password
    from app.main import app
    from app.models.core import Currency, Role, User, UserRole

    from tests.integration.conftest import ensure_schema

    # Shared with the `ctx` fixture in conftest.py: builds the schema once
    # per session and TRUNCATEs on later calls, rather than this file
    # independently dropping/recreating `public` on every test — see
    # docs/CI_TEST_PERFORMANCE_PLAN.md Phase 1 and ensure_schema's docstring
    # for why these two fixtures can't each do their own thing here.
    async with engine.begin() as conn:
        await ensure_schema(conn)

    async with async_session_factory() as db:
        db.add(Currency(code="USD", currency_name="US Dollar", symbol="$"))
        db.add(Currency(code="INR", currency_name="Indian Rupee", symbol="₹"))
        db.add(Role(name="System Manager", is_system=True))
        db.add(Role(name="Accounts User", is_system=True))
        admin = User(
            email="admin@test.io", first_name="Admin", hashed_password=hash_password("Passw0rd!xyz")
        )
        db.add(admin)
        await db.flush()
        db.add(UserRole(user_id=admin.id, role="System Manager", company_id=None))
        limited = User(
            email="limited@test.io", first_name="Limited", hashed_password=hash_password("Passw0rd!xyz")
        )
        db.add(limited)
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "Passw0rd!xyz"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_company_creation_seeds_coa_and_defaults(client: AsyncClient):
    headers = await _login(client, "admin@test.io")

    resp = await client.post(
        "/api/v1/companies",
        json={
            "company_name": "Acme India Pvt Ltd",
            "abbr": "ACME",
            "default_currency": "INR",
            "country_code": "IN",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    company = resp.json()

    # India template selected automatically and defaults resolved
    assert company["chart_of_accounts_template"] == "in_standard"
    assert company["default_receivable_account_id"] is not None
    assert company["default_payable_account_id"] is not None
    assert company["default_cost_center_id"] is not None

    # COA tree exists and is queryable through the API
    resp = await client.get(f"/api/v1/companies/{company['id']}/chart-of-accounts", headers=headers)
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) > 50
    roots = [a for a in accounts if a["parent_account_id"] is None]
    # in_standard mirrors erpnext's India chart: 4 roots, equity accounts
    # nest under "Source of Funds (Liabilities)" (no root-level Equity)
    assert {a["root_type"] for a in roots} == {"Asset", "Liability", "Income", "Expense"}

    # Indian fiscal year (April-March) auto-created
    from app.core.database import async_session_factory
    from app.models.accounts import FiscalYear

    async with async_session_factory() as db:
        fy = (await db.execute(select(FiscalYear))).scalars().one()
        assert fy.year_start_date.month == 4
        assert fy.year_end_date.month == 3

    # Audit trail recorded the INSERT
    async with async_session_factory() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(text("audit_logs")).where(text("doctype = 'Company'"))
            )
        ).scalar_one()
        assert count >= 1


@pytest.mark.asyncio
async def test_naming_series_increments_atomically(client: AsyncClient):
    from app.core.database import async_session_factory
    from app.core.naming import get_next_name

    company_id = uuid.uuid4()  # series are independent of company existence at this layer
    async with async_session_factory() as db:
        # naming_series.company_id FK requires a real company; create one minimal row
        from app.models.core import Company

        company = Company(id=company_id, company_name=f"NS Co {company_id.hex[:6]}",
                          abbr=company_id.hex[:6].upper(), default_currency="USD")
        db.add(company)
        await db.flush()
        first = await get_next_name(db, "SINV-.YYYY.-", company_id)
        second = await get_next_name(db, "SINV-.YYYY.-", company_id)
        await db.commit()

    assert first.endswith("00001")
    assert second.endswith("00002")
    assert first[:-5] == second[:-5]


@pytest.mark.asyncio
async def test_permission_denied_without_role(client: AsyncClient):
    headers = await _login(client, "limited@test.io")
    resp = await client.post(
        "/api/v1/companies",
        json={"company_name": "Nope Inc", "abbr": "NOPE", "default_currency": "USD"},
        headers=headers,
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "ERR_PERMISSION_DENIED"
    assert "detail" in body
