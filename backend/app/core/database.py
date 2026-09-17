"""Async SQLAlchemy engine + session factory.

Multi-tenancy: the API connects as a non-owner PostgreSQL role so the
row-level-security policies created in the migrations apply. For every
request the active company id (from the JWT) is injected into the
transaction-local GUC ``app.company_id`` which the RLS policies read:

    CREATE POLICY company_isolation ON <table>
      USING (company_id = current_setting('app.company_id', true)::uuid);

Services additionally filter by company_id explicitly (defense in depth,
Section 2.1 rule 5).
"""

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

# See Settings.db_search_path — test-only, routes this process's connections at a
# pytest-xdist worker's own schema (docs/CI_TEST_PERFORMANCE_PLAN.md Phase 2). "public"
# stays a fallback, not just the worker's own schema: an extension like ltree installs
# its types/functions into whichever schema existed first across every worker/test run
# in this database, and IF NOT EXISTS makes re-creating it into a worker's own schema a
# no-op once it exists anywhere — so an unqualified reference to its type only resolves
# if "public" is still reachable via search_path.
_connect_args = (
    {"server_settings": {"search_path": f"{_settings.db_search_path},public"}}
    if _settings.db_search_path
    else {}
)

engine = create_async_engine(
    _settings.database_url,
    echo=_settings.db_echo,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Plain session — used by auth endpoints that run before a company context exists."""
    async with async_session_factory() as session:
        yield session


def _arm_company_context(session: AsyncSession) -> None:
    """Re-apply the tenant GUC at the start of every transaction of this session.

    ``set_config(..., is_local := true)`` only lives until the next COMMIT, so a
    service that commits mid-request would otherwise continue in a fresh
    transaction with no tenant context — and RLS would hide every row it just
    wrote. The ``after_begin`` hook re-arms the GUC for each new transaction.
    """
    if session.info.get("company_context_armed"):
        return
    session.info["company_context_armed"] = True

    @event.listens_for(session.sync_session, "after_begin")
    def _set_guc(sync_session, transaction, connection) -> None:
        cid = sync_session.info.get("company_id", "")
        # cid is str(uuid.UUID) or "" — no injection surface
        connection.exec_driver_sql(f"SELECT set_config('app.company_id', '{cid}', true)")


async def set_company_context(session: AsyncSession, company_id: uuid.UUID | None) -> None:
    """Set the transaction-local tenant GUC consumed by RLS policies.

    The value is transaction-scoped (pooled connections never leak a tenant
    id) and re-armed on every new transaction via ``_arm_company_context``.
    """
    session.info["company_id"] = str(company_id) if company_id else ""
    _arm_company_context(session)
    await session.execute(
        text("SELECT set_config('app.company_id', :cid, true)"),
        {"cid": str(company_id) if company_id else ""},
    )
