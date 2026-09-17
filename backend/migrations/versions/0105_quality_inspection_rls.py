"""Privacy Phase 0 — close a second RLS coverage gap: quality_inspections.

``0103_rls_coverage`` closed 22 gap tables but missed a 23rd. Root cause:
``QualityInspection`` (``app/models/quality.py``, added by migration
``0075_quality_inspection``) uses ``CompanyScopedMixin`` but was never imported
by ``app/models/__init__.py`` — the module ``migrations/env.py`` and the guard
tests (``tests/unit/test_rls_coverage.py``, ``test_descriptor_drift.py``) rely
on to populate ``Base.metadata``/``Base.registry.mappers`` completely. With
that module never imported, ``quality_inspections`` silently never appeared in
``_scoped_tables()``, so the original gap-closure pass never saw it as needing
a policy either — a real, live cross-tenant gap the whole time, not a false
alarm; only a run that happens to import the full app (an integration test, or
CI) ever exercises the guard test completely enough to catch it, which is
exactly how this was found — running the full suite for real, not trusting a
unit-only green.

Fixed in two places: ``app/models/__init__.py`` now imports ``quality`` (closes
the blind spot for every future run of the guard tests, not just this one
table), and this migration grants ``quality_inspections`` the same
``company_isolation`` policy + ``FORCE ROW LEVEL SECURITY`` every other scoped
table carries — exactly the ``0103_rls_coverage`` pattern, for one table.

Revision ID: 0105_quality_inspection_rls
Revises: 0104_refresh_tokens
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0105_quality_inspection_rls"
down_revision: Union[str, None] = "0104_refresh_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE quality_inspections ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON quality_inspections "
        "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::uuid)"
    )
    op.execute("ALTER TABLE quality_inspections FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE quality_inspections NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS company_isolation ON quality_inspections")
    op.execute("ALTER TABLE quality_inspections DISABLE ROW LEVEL SECURITY")
