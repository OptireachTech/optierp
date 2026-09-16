"""Privacy Phase 0 — close the row-level-security coverage gap.

``CompanyScopedMixin``'s own docstring (``app/models/base.py``) promises every
tenant-owned table gets a ``company_isolation`` RLS policy in its migration.
That promise had drifted: 47 of 148 ``company_id``-bearing tables were never
given one, including ``bank_transactions``, ``shareholders``, ``share_transfers``,
``assets`` and the entire tax computation/filing table set (see
``docs/DATA_PRIVACY_ARCHITECTURE.md`` and ``docs/PRIVACY_IMPLEMENTATION_PLAN.md``
Phase 0). This migration:

  1. Enables RLS + the standard ``company_isolation`` policy on the 47 gap
     tables (``GAP_TABLES``), exactly matching the policy every other scoped
     table already carries (see e.g. migration 0001_core_setup).
  2. Adds ``FORCE ROW LEVEL SECURITY`` to *every* scoped table — the 47 above
     plus the 104 that already had ``ENABLE`` but never ``FORCE``
     (``ALREADY_ENABLED_TABLES``). Without FORCE, RLS does not apply to a
     table's owner; ``erp_owner`` (the Alembic/migration role — see
     ``infra/init-db.sql``) is exactly that owner, so a stray ad-hoc query run
     as ``erp_owner`` outside the app (a psql session, a maintenance script)
     was reading and writing across every tenant with no isolation at all.
     The application role ``erp_app`` is a non-owner and was already isolated
     by plain ``ENABLE``; FORCE closes the owner-bypass gap specifically.

Operational note for future migrations: FORCE RLS means any ``op.execute``
that INSERTs/UPDATEs a scoped table's rows from *this point forward* runs as
``erp_owner`` with no ``app.company_id`` GUC set, so the ``company_isolation``
USING clause (which doubles as the INSERT/UPDATE check) evaluates against
NULL and admits zero rows. A migration needing to backfill a scoped table
must ``SELECT set_config('app.company_id', '<uuid>', true)`` first, per
tenant. (No existing migration does this after the current head, so nothing
here needs changing retroactively — this is guidance for what comes next.)

Revision ID: 0103_rls_coverage
Revises: 0102_data_migration_rename
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0103_rls_coverage"
down_revision: Union[str, None] = "0102_data_migration_rename"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that already carry ENABLE ROW LEVEL SECURITY + company_isolation from
# an earlier migration (renamed tables listed under their current name — RLS
# policies follow a table across ALTER TABLE ... RENAME, per 0102's docstring)
# and only need FORCE added here.
ALREADY_ENABLED_TABLES: tuple[str, ...] = (
    "accounts", "addresses", "bank_accounts", "banks", "batches", "bins", "blanket_orders", "budgets",
    "campaigns", "contacts", "cost_centers", "coupon_codes", "customer_groups", "customers",
    "delivery_notes", "fiscal_years", "gl_entries", "item_alternatives", "item_groups", "item_prices",
    "items", "journal_entries", "letter_heads", "material_requests", "migration_imported_documents",
    "migration_imports", "migration_mappings", "migration_source_profiles", "modes_of_payment",
    "monthly_distributions", "naming_series", "payment_entries", "payment_terms",
    "payment_terms_templates", "period_closing_vouchers", "price_lists", "pricing_rules",
    "product_bundles", "promotional_schemes", "purchase_invoices", "purchase_orders", "purchase_receipts",
    "quotations", "requests_for_quotation", "sales_invoices", "sales_orders", "sales_partners",
    "sales_persons", "secretarial_agenda_items", "secretarial_appointments", "secretarial_attendance",
    "secretarial_auditors", "secretarial_beneficial_owners", "secretarial_capital_events",
    "secretarial_charges", "secretarial_circulars", "secretarial_circulation_recipients",
    "secretarial_circulations", "secretarial_committee_members", "secretarial_committees",
    "secretarial_compliance_items", "secretarial_compliance_reminders", "secretarial_compliance_rules",
    "secretarial_consent_responses", "secretarial_content_packs", "secretarial_ctcs",
    "secretarial_distinctive_seq", "secretarial_documents", "secretarial_dscs", "secretarial_engagements",
    "secretarial_entities", "secretarial_files", "secretarial_filings", "secretarial_financial_facts",
    "secretarial_group_links", "secretarial_meetings", "secretarial_members",
    "secretarial_minutes_book_seq", "secretarial_persons", "secretarial_portal_events",
    "secretarial_portal_tokens", "secretarial_practice_clients", "secretarial_related_parties",
    "secretarial_s186_entries", "secretarial_s186_limits", "secretarial_settings",
    "secretarial_share_certificates", "secretarial_share_transfer_details", "secretarial_status_history",
    "serial_nos", "service_credits", "shipping_rules", "stock_entries", "stock_ledger_entries",
    "stock_reconciliations", "supplier_groups", "supplier_quotations", "suppliers", "tax_categories",
    "tax_templates", "terms_templates", "territories", "utm_sources", "warehouses",
)

# Tables using CompanyScopedMixin that never got a company_isolation policy at all.
GAP_TABLES: tuple[str, ...] = (
    "advance_gst_adjustments", "asset_categories", "asset_maintenances", "asset_movements", "assets",
    "bank_transactions", "boms", "cm_cost_rates", "cm_cost_structures", "cm_plan_variance_runs",
    "cm_plans", "dunning_types", "email_logs", "item_tax_templates", "job_cards", "locations",
    "mat_credit_ledger", "operations", "payment_requests", "production_plans", "routings",
    "share_transfers", "share_types", "shareholders", "subcontract_jobs", "subscription_plans",
    "subscriptions", "tax_26as_recon_runs", "tax_challans", "tax_compliance_reminders",
    "tax_computation_adjustment_lines", "tax_computation_income_lines", "tax_computation_results",
    "tax_computation_runs", "tax_computations", "tax_credit_entries", "tax_depreciation_movements",
    "tax_depreciation_registers", "tax_filings", "tax_loss_carry_forward_ledger",
    "tax_loss_setoff_entries", "tax_policy_overrides", "tax_regime_elections", "tax_registrations",
    "tax_withholding_categories", "work_orders", "workstations",
)


def upgrade() -> None:
    # NULLIF guards the cast: an unset/empty GUC yields NULL -> no rows visible.
    for table in GAP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON {table} "
            f"USING (company_id = NULLIF(current_setting('app.company_id', true), '')::uuid)"
        )

    for table in GAP_TABLES + ALREADY_ENABLED_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in GAP_TABLES + ALREADY_ENABLED_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    for table in GAP_TABLES:
        op.execute(f"DROP POLICY IF EXISTS company_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
