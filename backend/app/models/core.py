"""Module 01 — Core / Setup ORM models.

Migrated from: erpnext/setup (Company, UOM, Currency Exchange, ...) and the
Frappe core DocTypes (User, Role, Workflow, Naming Series, System Settings).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CompanyScopedMixin, DocumentMixin


class Company(Base, DocumentMixin):
    """Legal entity / subsidiary with its own Chart of Accounts.

    Source: erpnext/setup/doctype/company. The company is also the tenant
    isolation unit (Section 4.1) — every scoped table FKs to it.
    """

    __tablename__ = "companies"

    company_name: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    abbr: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2))
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_id: Mapped[str | None] = mapped_column(String(80))  # GSTIN
    pan: Mapped[str | None] = mapped_column(String(10))  # Income-tax PAN
    tan: Mapped[str | None] = mapped_column(String(10))  # Tax Deduction Account Number
    domain: Mapped[str | None] = mapped_column(String(140))
    is_group: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    parent_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id")
    )
    date_of_establishment: Mapped[date | None] = mapped_column(Date)
    chart_of_accounts_template: Mapped[str | None] = mapped_column(String(80))

    # Default accounts — resolved after the COA is seeded; FKs added with
    # use_alter because companies <-> accounts reference each other.
    default_receivable_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_recv_account")
    )
    default_payable_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_pay_account")
    )
    default_cash_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_cash_account")
    )
    default_bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_bank_account")
    )
    default_income_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_income_account")
    )
    default_expense_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_expense_account")
    )
    round_off_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_roundoff_account")
    )
    write_off_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_writeoff_account")
    )
    exchange_gain_loss_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_exch_account")
    )
    default_cost_center_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cost_centers.id", use_alter=True, name="fk_company_cost_center"),
    )

    # Module 03 — perpetual inventory accounts (resolved from the COA template)
    enable_perpetual_inventory: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    default_inventory_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_inventory_account")
    )
    stock_received_but_not_billed_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_srbnb_account")
    )
    stock_adjustment_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", use_alter=True, name="fk_company_stock_adj_account")
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class User(Base, DocumentMixin):
    """Source: frappe/core/doctype/user. Users are global; company access
    is granted through role assignments (user_role.company_id)."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(140), unique=True, nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(140), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(140))
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    language: Mapped[str] = mapped_column(String(10), default="en", server_default=text("'en'"))
    time_zone: Mapped[str | None] = mapped_column(String(60))
    default_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", use_alter=True, name="fk_user_default_company")
    )
    last_login: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # user_roles also carries owner/modified_by FKs to users — name the join column
    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", foreign_keys="UserRole.user_id"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() if self.last_name else self.first_name


class Role(Base, DocumentMixin):
    """Source: frappe/core/doctype/role."""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class UserRole(Base, DocumentMixin):
    """Role assignment; company_id NULL = global assignment (e.g. System Manager)."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", "company_id", name="uq_user_role_company"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(100), ForeignKey("roles.name", onupdate="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )

    user: Mapped[User] = relationship(back_populates="roles", foreign_keys=[user_id])


class RefreshToken(Base):
    """One row per issued refresh token — lets a stolen-but-unused token be revoked
    and a replayed, already-rotated token be detected (Section 4.1 / Privacy Phase 0).

    Not ``CompanyScopedMixin``: a refresh token authenticates a user, not a
    company (the active company is chosen per access-token mint, after login).
    No ``DocumentMixin`` either — docstatus/owner don't apply to a session
    artifact, same reasoning as ``AuditLog``.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    creation: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # "logout" | "rotated" | "reuse_detected" — why this row stopped being valid.
    revoked_reason: Mapped[str | None] = mapped_column(String(30))
    # The jti of the token this one was rotated into, when revoked_reason == "rotated".
    replaced_by_jti: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class RolePermission(Base, DocumentMixin):
    """DocType-level permissions, matching ERPNext semantics (Section 4.2).

    company_id NULL = applies to all companies.
    """

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role", "doctype", "company_id", name="uq_role_doctype_company"),
        Index("ix_role_permissions_doctype", "doctype"),
    )

    role: Mapped[str] = mapped_column(
        String(100), ForeignKey("roles.name", onupdate="CASCADE"), nullable=False
    )
    doctype: Mapped[str] = mapped_column(String(100), nullable=False)
    can_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_write: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_create: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_submit: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_cancel: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_amend: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_print: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_email: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    can_report: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    if_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE")
    )


class UserPermission(Base, DocumentMixin):
    """Row-level user restrictions (mirrors frappe User Permission):
    restricts a user to specific linked values, e.g. only Territory = 'North'."""

    __tablename__ = "user_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctype: Mapped[str] = mapped_column(String(100), nullable=False)
    for_value: Mapped[str] = mapped_column(String(140), nullable=False)
    applicable_for: Mapped[str | None] = mapped_column(String(100))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE")
    )


class Currency(Base, DocumentMixin):
    """Source: frappe/geo currency master (global, not company-scoped)."""

    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    currency_name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(10))
    fraction: Mapped[str | None] = mapped_column(String(40))
    fraction_units: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    smallest_currency_fraction_value: Mapped[float | None] = mapped_column(Numeric(21, 9))
    number_format: Mapped[str | None] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class HsnCode(Base, DocumentMixin):
    """Global HSN/SAC → GST-rate reference master (India compliance).

    Seeded once from the official HSN rate schedule (``data/seeds/hsn_codes.json``);
    not company-scoped — the government's code list is the same for every tenant.
    Powers the "type an item name → auto-fetch HSN + GST rate" lookup on the Item
    form (``services.hsn_lookup``). A single 8-digit code can carry several rows
    (e.g. *fresh/chilled* Nil vs *frozen* 12%), so uniqueness is on code+description
    and the caller picks the specific commodity line.
    """

    __tablename__ = "hsn_codes"
    __table_args__ = (
        UniqueConstraint("hsn_code", "description", name="uq_hsn_code_description"),
        Index("ix_hsn_codes_hsn_code", "hsn_code"),
    )

    hsn_code: Mapped[str] = mapped_column(String(8), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    # Buckets the Item's gst_treatment (Taxable / Nil-Rated); Nil for the 0% slab.
    gst_treatment: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'Taxable'")
    )
    chapter: Mapped[int | None] = mapped_column(Integer)
    schedule: Mapped[str | None] = mapped_column(String(6))  # Roman numeral (I..V)


class CurrencyExchange(Base, DocumentMixin):
    """Source: erpnext/setup/doctype/currency_exchange."""

    __tablename__ = "currency_exchanges"
    __table_args__ = (
        UniqueConstraint(
            "from_currency", "to_currency", "date", "for_buying", "for_selling", name="uq_currency_exchange"
        ),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[float] = mapped_column(Numeric(21, 9), nullable=False)
    for_buying: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    for_selling: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Country(Base, DocumentMixin):
    """Global country master."""

    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    country_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    time_zone: Mapped[str | None] = mapped_column(String(60))


class UOM(Base, DocumentMixin):
    """Source: erpnext/setup/doctype/uom."""

    __tablename__ = "uoms"

    uom_name: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    must_be_whole_number: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class UOMConversion(Base, DocumentMixin):
    """Source: erpnext/setup/doctype/uom_conversion_factor."""

    __tablename__ = "uom_conversions"
    __table_args__ = (UniqueConstraint("from_uom", "to_uom", name="uq_uom_conversion"),)

    category: Mapped[str | None] = mapped_column(String(80))
    from_uom: Mapped[str] = mapped_column(String(140), ForeignKey("uoms.uom_name"), nullable=False)
    to_uom: Mapped[str] = mapped_column(String(140), ForeignKey("uoms.uom_name"), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(21, 9), nullable=False)


class NamingSeries(Base, DocumentMixin, CompanyScopedMixin):
    """Atomic per-company document-name counters (see app.core.naming)."""

    __tablename__ = "naming_series"
    __table_args__ = (UniqueConstraint("series_prefix", "company_id", name="uq_naming_series_prefix"),)

    series_prefix: Mapped[str] = mapped_column(String(80), nullable=False)
    pattern: Mapped[str] = mapped_column(String(80), nullable=False)
    current_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))


class SystemSetting(Base, DocumentMixin):
    """Key/value settings; company_id NULL = instance-wide default.

    Print Settings and Global Defaults are stored here as JSONB values
    rather than as separate one-row tables (assumption — see module notes).
    """

    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_system_setting"),)

    key: Mapped[str] = mapped_column(String(140), nullable=False)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE")
    )


class LetterHead(Base, DocumentMixin, CompanyScopedMixin):
    """Source: frappe/printing/doctype/letter_head."""

    __tablename__ = "letter_heads"
    __table_args__ = (UniqueConstraint("company_id", "letter_head_name", name="uq_letter_head"),)

    letter_head_name: Mapped[str] = mapped_column(String(140), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)  # header HTML
    footer: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Workflow(Base, DocumentMixin):
    """Workflow definition per doctype (Section 4.3)."""

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    doctype: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    initial_state: Mapped[str] = mapped_column(String(100), nullable=False)
    send_email_alert: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    states: Mapped[list["WorkflowState"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowState.idx"
    )
    transitions: Mapped[list["WorkflowTransition"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowTransition.idx"
    )


class WorkflowState(Base, DocumentMixin):
    __tablename__ = "workflow_states"
    __table_args__ = (UniqueConstraint("workflow_id", "state", name="uq_workflow_state"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    state_docstatus: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    allow_edit_role: Mapped[str | None] = mapped_column(String(100))
    next_action_role: Mapped[str | None] = mapped_column(String(100))
    idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    workflow: Mapped[Workflow] = relationship(back_populates="states")

    # The engine reads ``docstatus`` of the *target document*; this property
    # exposes the state's intended docstatus without clashing with the
    # DocumentMixin.docstatus column of this row itself.
    @property
    def target_docstatus(self) -> int:
        return self.state_docstatus


class WorkflowTransition(Base, DocumentMixin):
    __tablename__ = "workflow_transitions"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    to_state: Mapped[str] = mapped_column(String(100), nullable=False)
    allowed_role: Mapped[str] = mapped_column(String(100), nullable=False)
    condition: Mapped[str | None] = mapped_column(Text)  # reserved: expression-based gating
    idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    workflow: Mapped[Workflow] = relationship(back_populates="transitions")


class WorkflowActionLog(Base, DocumentMixin):
    """Append-only log of applied workflow actions (workflow_action_master)."""

    __tablename__ = "workflow_action_logs"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    doctype: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    from_state: Mapped[str] = mapped_column(String(100), nullable=False)
    to_state: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE")
    )


class AuditLog(Base):
    """Audit trail (Section 4.7) — populated by the service layer so the
    acting user and request context are captured. Append-only."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_doc", "doctype", "document_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    creation: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    doctype: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # INSERT/UPDATE/DELETE/SUBMIT/CANCEL
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Set when the actor was working under a delegated engagement (Module 13): the
    # firm they acted *from*, as distinct from company_id — the tenant whose data
    # was touched. Three facts, not two. See docs/SECRETARIAL_GAP_AND_PLAN.md §2.2.
    acting_company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    data_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    data_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)


class NotificationTemplate(Base, DocumentMixin):
    """Jinja2 notification templates (Section 4.4)."""

    __tablename__ = "notification_templates"

    name: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_html: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    channel: Mapped[str] = mapped_column(String(20), default="email", server_default=text("'email'"))


class EmailLog(Base, DocumentMixin, CompanyScopedMixin):
    """Send audit trail — one row per outbound email (sent or failed).

    Written by ``services.email.send_document_email`` so a user can see what was
    emailed, to whom, when, and whether it succeeded. Company-scoped; reads filter
    by ``company_id`` explicitly (mirrors ``bank_transactions`` — no RLS policy).
    """

    __tablename__ = "email_logs"
    __table_args__ = (Index("ix_email_logs_reference", "reference_doctype", "reference_id"),)

    to_addresses: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    reference_doctype: Mapped[str | None] = mapped_column(String(100))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # Sent | Failed
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
