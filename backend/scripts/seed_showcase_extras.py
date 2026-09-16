"""Fill the demo gaps: the doctypes no existing seeder touches.

``seed_demo`` + the per-module demo seeders already cover accounting, stock,
buying, selling, manufacturing basics, assets, CM planning, GST/TDS returns and
income tax. This script adds the screens that were still empty on a fresh demo:

    Request for Quotation → Supplier Quotation   (buying)
    Production Plan → Work Orders                (manufacturing planning)
    Job Cards (started + completed)              (shop floor)
    Quality Inspections (accepted + rejected)    (quality)
    Subcontract Job (sent + received)            (outsourced manufacturing)
    Service Credits with usage                   (stock / prepaid services)
    Reorder levels                               (stock reorder report)
    Share Types / Shareholders / Share Transfers (accounts — capital)
    Subscription Plans + Subscriptions           (accounts — recurring billing)
    Payment Requests                             (accounts — collections)

Run standalone, or let ``scripts.seed_showcase`` run it as one of its steps:

    python -m scripts.seed_showcase_extras

Every section is idempotent (keyed on a name or remark it writes) and isolated:
a section that fails is reported and the rest still run, because a half-built
demo is far more useful than none. Prerequisite: ``scripts.seed_demo`` has built
the company, items, warehouses and parties this builds on top of.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--database-url",
        default=(
            os.environ.get("SEED_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or "postgresql+asyncpg://erp_owner:milin@localhost:5432/erp"
        ),
    )
    p.add_argument("--company-name", default=os.environ.get("DEMO_COMPANY", "Mango Appliances Demo"))
    return p.parse_args()


ARGS = _parse_args()
os.environ["DATABASE_URL"] = ARGS.database_url
os.environ.setdefault("MIGRATIONS_DATABASE_URL", ARGS.database_url)
os.environ.setdefault("SECRET_KEY", "seed-showcase-extras-not-used-0123456789")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
# a default Windows console is cp1252 and cannot print the rupee/dash glyphs
# the service layer emits — never let that be what fails a seed
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.core.database import async_session_factory, set_company_context  # noqa: E402
from app.core.security import CurrentUser  # noqa: E402
from app.models.accounts import Account, PaymentRequest, SalesInvoice  # noqa: E402
from app.models.accounts.share import ShareTransfer, ShareType, Shareholder  # noqa: E402
from app.models.accounts.subscription import Subscription, SubscriptionPlan  # noqa: E402
from app.models.buying import RequestForQuotation, Supplier  # noqa: E402
from app.models.core import Company, User  # noqa: E402
from app.models.manufacturing import (  # noqa: E402
    BOM,
    JobCard,
    ProductionPlan,
    SubcontractJob,
    WorkOrder,
)
from app.models.quality import QualityInspection  # noqa: E402
from app.models.selling import Customer  # noqa: E402
from app.models.stock import Item, ServiceCredit, StockLedgerEntry, Warehouse  # noqa: E402
from app.schemas.accounts.masters import PaymentRequestCreate  # noqa: E402
from app.schemas.accounts.share import ShareTransferCreate  # noqa: E402
from app.schemas.accounts.subscription import (  # noqa: E402
    SubscriptionCreate,
    SubscriptionPlanDetailIn,
)
from app.schemas.buying import (  # noqa: E402
    RFQCreate,
    RFQItemIn,
    SupplierQuotationCreate,
    SupplierQuotationItemIn,
)
from app.schemas.manufacturing import (  # noqa: E402
    JobCardCompleteIn,
    ProductionPlanCreate,
    ProductionPlanItemIn,
    SubcontractJobCreate,
    SubcontractJobReceiveIn,
    SubcontractJobSendIn,
)
from app.schemas.quality import QualityInspectionCreate  # noqa: E402
from app.schemas.stock import ServiceCreditCreate, ServiceCreditUsageIn  # noqa: E402
from app.services import job_card as jc_service  # noqa: E402
from app.services import payment_request as pr_service  # noqa: E402
from app.services import production_plan as pp_service  # noqa: E402
from app.services import quality_inspection as qi_service  # noqa: E402
from app.services import rfq as rfq_service  # noqa: E402
from app.services import service_credit as sc_service  # noqa: E402
from app.services import share_transfer as share_service  # noqa: E402
from app.services import subcontract_job as sub_service  # noqa: E402
from app.services import subscription as sub_billing  # noqa: E402
from app.services import work_order as wo_service  # noqa: E402

TODAY = date.today()


def _d(value: float | int | str) -> Decimal:
    return Decimal(str(value))


class Ctx:
    """Everything the sections build on, resolved once."""

    def __init__(self, company: Company, actor: CurrentUser) -> None:
        self.company = company
        self.actor = actor


async def _context(db: AsyncSession) -> Ctx:
    company = await db.scalar(select(Company).where(Company.company_name == ARGS.company_name))
    if company is None:
        company = await db.scalar(select(Company).order_by(Company.creation.asc()))
    if company is None:
        raise RuntimeError("No company found — run scripts.seed_demo first.")
    await set_company_context(db, company.id)

    user = await db.scalar(select(User).where(User.email == "admin@example.com"))
    if user is None:
        user = await db.scalar(select(User).order_by(User.creation.asc()))
    if user is None:
        raise RuntimeError("No user found — run scripts.seed first.")

    actor = CurrentUser(
        {
            "sub": str(user.id),
            "email": user.email,
            "company_id": str(company.id),
            "roles": ["System Manager"],
        }
    )
    return Ctx(company, actor)


async def _stock_warehouses(db: AsyncSession, company_id: uuid.UUID) -> list[Warehouse]:
    return list(
        (
            await db.execute(
                select(Warehouse)
                .where(
                    Warehouse.company_id == company_id,
                    Warehouse.is_group.is_(False),
                    Warehouse.disabled.is_(False),
                )
                .order_by(Warehouse.warehouse_name)
            )
        )
        .scalars()
        .all()
    )


def _pick_warehouse(warehouses: list[Warehouse], *preferred: str) -> Warehouse:
    """First warehouse whose name matches one of ``preferred``, else the first.

    Ordering warehouses alphabetically and taking [0] silently picks "Finished
    Goods" as a raw-material source, so choose by name and mean it.
    """
    by_name = {w.warehouse_name.casefold(): w for w in warehouses}
    for name in preferred:
        match = by_name.get(name.casefold())
        if match is not None:
            return match
    return warehouses[0]


async def _warehouse_holding(
    db: AsyncSession, company_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> Warehouse | None:
    """The warehouse holding most of ``item_ids`` — where the stock actually is."""
    if not item_ids:
        return None
    row = (
        await db.execute(
            select(StockLedgerEntry.warehouse_id, func.sum(StockLedgerEntry.actual_qty).label("qty"))
            .where(
                StockLedgerEntry.company_id == company_id,
                StockLedgerEntry.item_id.in_(item_ids),
            )
            .group_by(StockLedgerEntry.warehouse_id)
            .having(func.sum(StockLedgerEntry.actual_qty) > 0)
            .order_by(func.sum(StockLedgerEntry.actual_qty).desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return await db.scalar(select(Warehouse).where(Warehouse.id == row.warehouse_id))


async def _ensure_item(
    db: AsyncSession, ctx: Ctx, item_code: str, item_name: str, **fields
) -> Item:
    """Look up an item by (company, code) and create it only if missing.

    Sections share service/subscription item codes, and a section may re-run
    after a partially failed pass, so creating blind trips ``uq_item_code``.
    """
    item = await db.scalar(
        select(Item).where(Item.company_id == ctx.company.id, Item.item_code == item_code)
    )
    if item is not None:
        return item
    item = Item(
        id=uuid.uuid4(),
        company_id=ctx.company.id,
        item_code=item_code,
        item_name=item_name,
        stock_uom=fields.pop("stock_uom", "Nos"),
        **fields,
    )
    db.add(item)
    await db.flush()
    return item


# --- buying: RFQ → Supplier Quotation --------------------------------------------------


async def seed_rfq_cycle(db: AsyncSession, ctx: Ctx) -> str:
    """One submitted RFQ to three suppliers, with two of them quoting back.

    Gives the RFQ list, the comparison view and the Supplier Quotation list real
    rows — including two different rates for the same item, which is the whole
    point of the screen.
    """
    remarks = "Showcase: annual compressor rate contract"
    if await db.scalar(
        select(RequestForQuotation).where(
            RequestForQuotation.company_id == ctx.company.id,
            RequestForQuotation.remarks == remarks,
        )
    ):
        return "already seeded"

    suppliers = list(
        (
            await db.execute(
                select(Supplier)
                .where(Supplier.company_id == ctx.company.id, Supplier.disabled.is_(False))
                .order_by(Supplier.supplier_name)
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    items = list(
        (
            await db.execute(
                select(Item)
                .where(
                    Item.company_id == ctx.company.id,
                    Item.is_purchase_item.is_(True),
                    Item.is_stock_item.is_(True),
                )
                .order_by(Item.item_code)
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(suppliers) < 2 or not items:
        return "skipped — needs 2 suppliers and an item"

    warehouses = await _stock_warehouses(db, ctx.company.id)
    wh_id = warehouses[0].id if warehouses else None

    rfq = await rfq_service.create_rfq(
        db,
        RFQCreate(
            posting_date=TODAY - timedelta(days=21),
            schedule_date=TODAY - timedelta(days=7),
            message_for_supplier=(
                "Please quote your best landed rate for the items below, inclusive of "
                "freight to our Mumbai stores. Rates to hold for 12 months."
            ),
            remarks=remarks,
            items=[
                RFQItemIn(
                    item_id=item.id,
                    qty=_d(qty),
                    warehouse_id=wh_id,
                    schedule_date=TODAY - timedelta(days=7),
                )
                for item, qty in zip(items, (150, 90))
            ],
            supplier_ids=[s.id for s in suppliers],
        ),
        ctx.actor,
    )
    await rfq_service.submit_rfq(db, rfq.id, ctx.actor)
    await db.commit()

    # Two suppliers respond at different rates — one gets submitted (the winner),
    # the other stays draft so the list shows both docstatuses.
    rfq = await rfq_service.get_rfq(db, rfq.id, ctx.company.id)
    base_rates = [_d(1850), _d(4200)]
    quoted = 0
    for supplier, markup in zip(suppliers[:2], (Decimal("1.00"), Decimal("1.08"))):
        sq = await rfq_service.create_supplier_quotation(
            db,
            SupplierQuotationCreate(
                supplier_id=supplier.id,
                posting_date=TODAY - timedelta(days=14),
                valid_till=TODAY + timedelta(days=180),
                rfq_id=rfq.id,
                remarks=f"Rate contract response — {supplier.supplier_name}",
                items=[
                    SupplierQuotationItemIn(
                        item_id=item.id,
                        qty=_d(qty),
                        rate=(rate * markup).quantize(Decimal("0.01")),
                        warehouse_id=wh_id,
                    )
                    for item, qty, rate in zip(items, (150, 90), base_rates)
                ],
            ),
            ctx.actor,
        )
        if quoted == 0:  # the cheaper quote is the accepted one
            await rfq_service.submit_supplier_quotation(db, sq.id, ctx.actor)
        quoted += 1
    await db.commit()
    return f"RFQ {rfq.name} to {len(suppliers)} suppliers, {quoted} quotations"


# --- manufacturing: Production Plan → Work Orders --------------------------------------


async def seed_production_plan(db: AsyncSession, ctx: Ctx) -> str:
    """A submitted Production Plan with exploded raw materials and Work Orders.

    Uses whatever active BOMs the manufacturing seed produced, so this keeps
    working as that data changes.
    """
    remarks = "Showcase: Q3 finished-goods build plan"
    if await db.scalar(
        select(ProductionPlan).where(
            ProductionPlan.company_id == ctx.company.id, ProductionPlan.remarks == remarks
        )
    ):
        return "already seeded"

    boms = list(
        (
            await db.execute(
                select(BOM)
                .where(
                    BOM.company_id == ctx.company.id,
                    BOM.is_active.is_(True),
                    BOM.docstatus == 1,
                )
                .order_by(BOM.creation)
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not boms:
        return "skipped — no submitted BOM"

    warehouses = await _stock_warehouses(db, ctx.company.id)
    if not warehouses:
        return "skipped — no warehouse"
    fg_wh = warehouses[0]
    src_wh = warehouses[1] if len(warehouses) > 1 else warehouses[0]

    plan = await pp_service.create_production_plan(
        db,
        ProductionPlanCreate(
            posting_date=TODAY - timedelta(days=5),
            from_date=TODAY - timedelta(days=5),
            to_date=TODAY + timedelta(days=45),
            get_items_from="Manual",
            fg_warehouse_id=fg_wh.id,
            source_warehouse_id=src_wh.id,
            remarks=remarks,
            items=[
                ProductionPlanItemIn(
                    item_id=bom.production_item_id,
                    bom_id=bom.id,
                    planned_qty=_d(qty),
                    warehouse_id=fg_wh.id,
                    planned_start_date=TODAY + timedelta(days=offset),
                    description="Quarterly build against forecast demand",
                )
                for bom, qty, offset in zip(boms, (40, 25), (3, 12))
            ],
        ),
        ctx.actor,
    )
    # explode to leaf raws and net off stock, so the MR proposal rows are real
    await pp_service.get_raw_materials(db, plan.id, ctx.actor, only_shortfall=True)
    await pp_service.submit_production_plan(db, plan.id, ctx.actor)
    await db.commit()

    result = await pp_service.create_work_orders_from_plan(db, plan.id, ctx.actor)
    await db.commit()

    # Submit them: a submitted Work Order is what the shop-floor screens read,
    # and submitting also raises one Job Card per routing operation — which is
    # what the job_cards section then drives.
    submitted = 0
    for wo_id in result.created_ids:
        try:
            await wo_service.submit_work_order(db, wo_id, ctx.actor)
            submitted += 1
        except Exception:  # noqa: BLE001 — e.g. no material available; leave it draft
            await db.rollback()
    return (
        f"Plan {plan.name}: {len(boms)} FG rows -> {result.count} work orders "
        f"({submitted} submitted)"
    )


# --- shop floor: Job Cards -------------------------------------------------------------


async def seed_job_cards(db: AsyncSession, ctx: Ctx) -> str:
    """Drive the auto-raised Job Cards into a spread of statuses.

    Submitting a Work Order already creates one Job Card per routing operation,
    so this does not create any — it moves them, leaving some Completed, one
    with a running clock (In Process) and the rest Open.
    """
    if await db.scalar(
        select(func.count())
        .select_from(JobCard)
        .where(JobCard.company_id == ctx.company.id, JobCard.total_completed_qty > 0)
    ):
        return "already seeded"

    cards = list(
        (
            await db.execute(
                select(JobCard)
                .options(selectinload(JobCard.time_logs))
                .where(JobCard.company_id == ctx.company.id, JobCard.status == "Open")
                .order_by(JobCard.creation)
            )
        )
        .scalars()
        .all()
    )
    if not cards:
        return "skipped - no open job cards (work orders have no routing operations)"

    completed = running = 0
    for pos, jc in enumerate(cards):
        wo = await db.scalar(select(WorkOrder).where(WorkOrder.id == jc.work_order_id))
        if pos == 0 or pos % 3 == 0:
            await jc_service.start_job_card(db, jc.id, ctx.actor)
            await jc_service.complete_job_card(
                db,
                jc.id,
                JobCardCompleteIn(completed_qty=wo.qty if wo else jc.for_quantity),
                ctx.actor,
            )
            completed += 1
        elif pos % 3 == 1:
            # left running, so the shop floor shows work in progress right now
            await jc_service.start_job_card(db, jc.id, ctx.actor)
            running += 1
    await db.commit()
    return (
        f"{len(cards)} job cards: {completed} completed, {running} in process, "
        f"{len(cards) - completed - running} open"
    )


# --- quality ---------------------------------------------------------------------------


async def seed_quality_inspections(db: AsyncSession, ctx: Ctx) -> str:
    """One accepted and one rejected inspection, plus a draft awaiting a decision."""
    if await db.scalar(
        select(func.count())
        .select_from(QualityInspection)
        .where(QualityInspection.company_id == ctx.company.id)
    ):
        return "already seeded"

    work_orders = list(
        (
            await db.execute(
                select(WorkOrder)
                .where(WorkOrder.company_id == ctx.company.id, WorkOrder.docstatus == 1)
                .order_by(WorkOrder.creation)
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    if not work_orders:
        return "skipped — no submitted work order"

    plan = [
        ("Sample of 5 checked against drawing rev C — all dimensions within tolerance.", True),
        ("Surface finish below spec on 2 units; batch held for rework.", False),
        ("Routine in-process check — awaiting metrology sign-off.", None),
    ]
    made = 0
    for wo, (remark, accept) in zip(work_orders, plan):
        qi = await qi_service.create_quality_inspection(
            db,
            QualityInspectionCreate(
                reference_type="Work Order",
                reference_id=wo.id,
                item_id=wo.production_item_id,
                qty=wo.qty,
                inspection_date=TODAY - timedelta(days=2),
                remarks=remark,
            ),
            ctx.actor,
        )
        if accept is not None:
            await qi_service.submit_quality_inspection(db, qi.id, ctx.actor, accept=accept)
        made += 1
    await db.commit()
    return f"{made} quality inspections (accepted / rejected / draft)"


# --- outsourced manufacturing ----------------------------------------------------------


async def seed_subcontract_job(db: AsyncSession, ctx: Ctx) -> str:
    """A subcontract job with materials sent out and finished goods received back."""
    if await db.scalar(
        select(func.count())
        .select_from(SubcontractJob)
        .where(SubcontractJob.company_id == ctx.company.id)
    ):
        return "already seeded"

    bom = await db.scalar(
        select(BOM)
        .options(selectinload(BOM.items))
        .where(BOM.company_id == ctx.company.id, BOM.is_active.is_(True), BOM.docstatus == 1)
        .order_by(BOM.creation)
    )
    supplier = await db.scalar(
        select(Supplier)
        .where(Supplier.company_id == ctx.company.id, Supplier.disabled.is_(False))
        .order_by(Supplier.supplier_name)
    )
    if bom is None or supplier is None:
        return "skipped — needs a submitted BOM and a supplier"

    warehouses = await _stock_warehouses(db, ctx.company.id)
    if not warehouses:
        return "skipped — no warehouse"
    # Send from wherever the components actually sit, not from whichever
    # warehouse sorts first — otherwise the transfer fails on zero stock.
    component_ids = [row.item_id for row in bom.items]
    source = await _warehouse_holding(db, ctx.company.id, component_ids) or _pick_warehouse(
        warehouses, "Main Store", "Stores"
    )
    fg = _pick_warehouse(warehouses, "Finished Goods", "Main Store")

    # The supplier's premises is its own warehouse — that is what makes the
    # sent-but-not-consumed stock visible in the ledger.
    supplier_wh = await db.scalar(
        select(Warehouse).where(
            Warehouse.company_id == ctx.company.id,
            Warehouse.warehouse_name == "Subcontractor - Precision Works",
        )
    )
    if supplier_wh is None:
        supplier_wh = Warehouse(
            id=uuid.uuid4(),
            company_id=ctx.company.id,
            warehouse_name="Subcontractor - Precision Works",
            warehouse_type="Subcontractor",
            is_group=False,
            parent_warehouse_id=source.parent_warehouse_id,
        )
        db.add(supplier_wh)
        await db.flush()

    service_expense = await db.scalar(
        select(Account).where(
            Account.company_id == ctx.company.id,
            Account.is_group.is_(False),
            Account.account_name.ilike("%Cost of Goods Sold%"),
        )
    )

    job = await sub_service.create_subcontract_job(
        db,
        SubcontractJobCreate(
            bom_id=bom.id,
            supplier_id=supplier.id,
            qty=_d(10),
            posting_date=TODAY - timedelta(days=12),
            supplier_warehouse_id=supplier_wh.id,
            source_warehouse_id=source.id,
            fg_warehouse_id=fg.id,
            service_cost=_d(4500),
            service_cost_account_id=service_expense.id if service_expense else None,
            remarks="Showcase: machining outsourced to Precision Works",
        ),
        ctx.actor,
    )
    await sub_service.submit_subcontract_job(db, job.id, ctx.actor)
    await db.commit()

    # Send all 10, receive 6 back — leaves the job "Partially Received", which is
    # the state worth showing.
    await sub_service.send_to_subcontractor(
        db, job.id, SubcontractJobSendIn(qty=_d(10), posting_date=TODAY - timedelta(days=10)), ctx.actor
    )
    await db.commit()
    await sub_service.receive_from_subcontractor(
        db,
        job.id,
        SubcontractJobReceiveIn(
            qty=_d(6),
            posting_date=TODAY - timedelta(days=3),
            service_cost_account_id=service_expense.id if service_expense else None,
        ),
        ctx.actor,
    )
    await db.commit()
    return f"Subcontract job {job.name}: 10 sent, 6 received"


# --- stock: service credits + reorder levels -------------------------------------------


async def seed_service_credits(db: AsyncSession, ctx: Ctx) -> str:
    """Prepaid service blocks (AMC hours, calibration visits) with usage drawn down."""
    if await db.scalar(
        select(func.count())
        .select_from(ServiceCredit)
        .where(ServiceCredit.company_id == ctx.company.id)
    ):
        return "already seeded"

    supplier = await db.scalar(
        select(Supplier)
        .where(Supplier.company_id == ctx.company.id, Supplier.disabled.is_(False))
        .order_by(Supplier.supplier_name)
    )
    prepaid = await db.scalar(
        select(Account).where(
            Account.company_id == ctx.company.id,
            Account.is_group.is_(False),
            Account.account_name.ilike("%Prepaid%"),
        )
    )
    expense = await db.scalar(
        select(Account).where(
            Account.company_id == ctx.company.id,
            Account.is_group.is_(False),
            Account.account_name.ilike("%Cost of Goods Sold%"),
        )
    )

    made = 0
    for code, name, qty, rate, used in [
        ("SVC-AMC-HRS", "AMC Engineer Hours", 200, 850, 74),
        ("SVC-CALIB", "Instrument Calibration Visits", 24, 3200, 9),
    ]:
        item = await _ensure_item(
            db,
            ctx,
            code,
            name,
            is_stock_item=False,  # a service block, not inventory
            is_sales_item=False,
            is_purchase_item=True,
            description=f"Prepaid {name.lower()} drawn down as consumed",
        )

        credit = await sc_service.create_service_credit(
            db,
            ServiceCreditCreate(
                item_id=item.id,
                supplier_id=supplier.id if supplier else None,
                purchase_date=TODAY - timedelta(days=90),
                purchased_qty=_d(qty),
                rate=_d(rate),
                valid_upto=TODAY + timedelta(days=275),
                remarks=f"Annual prepaid block — {name}",
                prepaid_account_id=prepaid.id if prepaid else None,
                expense_account_id=expense.id if expense else None,
            ),
            ctx.actor,
        )
        # draw it down in a few visits so the balance is a real number
        drawn = Decimal("0")
        for offset, slice_qty in [(70, used * 0.5), (35, used * 0.3), (8, used * 0.2)]:
            chunk = _d(round(slice_qty, 2))
            if chunk <= 0 or drawn + chunk > _d(qty):
                continue
            await sc_service.add_usage(
                db,
                credit.id,
                ServiceCreditUsageIn(
                    usage_date=TODAY - timedelta(days=offset),
                    qty=chunk,
                    remarks="Preventive maintenance visit",
                ),
                ctx.actor,
            )
            drawn += chunk
        made += 1
    await db.commit()
    return f"{made} service credits with usage history"


async def seed_reorder_levels(db: AsyncSession, ctx: Ctx) -> str:
    """Set reorder level/qty on stock items so the reorder report has suggestions."""
    items = list(
        (
            await db.execute(
                select(Item)
                .where(
                    Item.company_id == ctx.company.id,
                    Item.is_stock_item.is_(True),
                    Item.reorder_level == 0,
                )
                .order_by(Item.item_code)
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return "already seeded"

    warehouses = await _stock_warehouses(db, ctx.company.id)
    for pos, item in enumerate(items):
        # deliberately mixed: some levels sit above current stock so the reorder
        # report actually proposes something
        item.reorder_level = _d([25, 50, 100, 150][pos % 4])
        item.reorder_qty = _d([50, 100, 200, 300][pos % 4])
        if item.default_warehouse_id is None and warehouses:
            item.default_warehouse_id = warehouses[0].id
    await db.commit()
    return f"reorder levels set on {len(items)} items"


# --- accounts: share capital -----------------------------------------------------------


async def seed_share_capital(db: AsyncSession, ctx: Ctx) -> str:
    """Share types, shareholders and a transfer history that adds up to a cap table."""
    if await db.scalar(
        select(func.count()).select_from(ShareTransfer).where(ShareTransfer.company_id == ctx.company.id)
    ):
        return "already seeded"

    types: dict[str, ShareType] = {}
    for name, par in [("Equity", 10), ("Preference", 100)]:
        st = await db.scalar(
            select(ShareType).where(
                ShareType.company_id == ctx.company.id, ShareType.share_type_name == name
            )
        )
        if st is None:
            st = ShareType(
                id=uuid.uuid4(),
                company_id=ctx.company.id,
                share_type_name=name,
                currency=ctx.company.default_currency,
                par_value=_d(par),
            )
            db.add(st)
            await db.flush()
        types[name] = st

    holders: dict[str, Shareholder] = {}
    for pos, name in enumerate(
        ["Milin Kanu (Founder)", "Sneha Jha (Co-founder)", "Kalpataru Ventures LLP", "ESOP Trust"]
    ):
        sh = await db.scalar(
            select(Shareholder).where(
                Shareholder.company_id == ctx.company.id, Shareholder.shareholder_name == name
            )
        )
        if sh is None:
            sh = Shareholder(
                id=uuid.uuid4(),
                company_id=ctx.company.id,
                shareholder_name=name,
                folio_no=f"FOLIO-{pos + 1:04d}",
            )
            db.add(sh)
            await db.flush()
        holders[name] = sh
    await db.commit()

    # founding issue → investor round → a secondary transfer → ESOP pool
    script: list[tuple[str, str, str | None, str | None, int, int, int, str]] = [
        ("Issue", "Equity", None, "Milin Kanu (Founder)", 600_000, 10, 900, "Founder subscription at par"),
        ("Issue", "Equity", None, "Sneha Jha (Co-founder)", 400_000, 10, 900, "Co-founder subscription at par"),
        ("Issue", "Preference", None, "Kalpataru Ventures LLP", 50_000, 240, 420, "Series A preference round"),
        ("Issue", "Equity", None, "ESOP Trust", 90_000, 10, 300, "ESOP pool carve-out"),
        ("Transfer", "Equity", "Milin Kanu (Founder)", "Kalpataru Ventures LLP", 40_000, 260, 120,
         "Secondary sale as part of Series A"),
    ]
    made = 0
    for kind, share_type, frm, to, qty, rate, days_ago, remark in script:
        transfer = await share_service.create_share_transfer(
            db,
            ShareTransferCreate(
                transfer_type=kind,
                share_type_id=types[share_type].id,
                from_shareholder_id=holders[frm].id if frm else None,
                to_shareholder_id=holders[to].id if to else None,
                no_of_shares=qty,
                rate=_d(rate),
                transfer_date=TODAY - timedelta(days=days_ago),
                remarks=remark,
            ),
            ctx.actor,
        )
        await share_service.submit_share_transfer(db, transfer.id, ctx.actor)
        made += 1
    await db.commit()
    return f"{len(types)} share types, {len(holders)} shareholders, {made} transfers"


# --- accounts: recurring billing -------------------------------------------------------


async def seed_subscriptions(db: AsyncSession, ctx: Ctx) -> str:
    """Subscription plans + live subscriptions, with the first invoices generated."""
    if await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.company_id == ctx.company.id)
    ):
        return "already seeded"

    customers = list(
        (
            await db.execute(
                select(Customer)
                .where(Customer.company_id == ctx.company.id, Customer.disabled.is_(False))
                .order_by(Customer.customer_name)
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    if not customers:
        return "skipped — no customer"

    plans: dict[str, SubscriptionPlan] = {}
    for code, plan_name, price, interval, count in [
        ("SVC-AMC-GOLD", "AMC Gold — Monthly", 12_500, "Month", 1),
        ("SVC-CLOUD", "Cloud Monitoring — Quarterly", 27_000, "Month", 3),
        ("SVC-SUPPORT", "Priority Support — Annual", 96_000, "Year", 1),
    ]:
        item = await _ensure_item(
            db,
            ctx,
            code,
            plan_name.split(" — ")[0],
            is_stock_item=False,
            is_sales_item=True,
            is_purchase_item=False,
            description=f"Recurring service — {plan_name}",
        )

        plan = await db.scalar(
            select(SubscriptionPlan).where(
                SubscriptionPlan.company_id == ctx.company.id,
                SubscriptionPlan.plan_name == plan_name,
            )
        )
        if plan is None:
            plan = SubscriptionPlan(
                id=uuid.uuid4(),
                company_id=ctx.company.id,
                plan_name=plan_name,
                item_id=item.id,
                price=_d(price),
                billing_interval=interval,
                interval_count=count,
                currency=ctx.company.default_currency,
            )
            db.add(plan)
            await db.flush()
        plans[plan_name] = plan
    await db.commit()

    made = 0
    plan_list = list(plans.values())
    for pos, customer in enumerate(customers):
        picked = [plan_list[pos % len(plan_list)]]
        if pos == 0 and len(plan_list) > 1:  # one customer on two plans
            picked.append(plan_list[1])
        sub = await sub_billing.create_subscription(
            db,
            SubscriptionCreate(
                customer_id=customer.id,
                start_date=TODAY - timedelta(days=120),
                days_until_due=15,
                generate_at="Beginning",
                next_invoice_date=TODAY - timedelta(days=120),
                plans=[SubscriptionPlanDetailIn(plan_id=p.id, qty=_d(1)) for p in picked],
            ),
            ctx.actor,
        )
        await db.commit()
        # catch the subscription up to today so it has billing history, not just
        # a start date — bounded so a stale demo cannot spin.
        for _ in range(6):
            try:
                invoice = await sub_billing.generate_due_invoice(db, sub.id, ctx.actor)
            except Exception:  # noqa: BLE001 — nothing due, or invoice preconditions unmet
                break
            if invoice is None:
                break
            await db.commit()
        made += 1
    return f"{len(plans)} plans, {made} subscriptions with generated invoices"


# --- accounts: collections -------------------------------------------------------------


async def seed_payment_requests(db: AsyncSession, ctx: Ctx) -> str:
    """Payment requests against the oldest unpaid invoices."""
    if await db.scalar(
        select(func.count()).select_from(PaymentRequest).where(PaymentRequest.company_id == ctx.company.id)
    ):
        return "already seeded"

    invoices = list(
        (
            await db.execute(
                select(SalesInvoice)
                .where(
                    SalesInvoice.company_id == ctx.company.id,
                    SalesInvoice.docstatus == 1,
                    SalesInvoice.outstanding_amount > 0,
                )
                .order_by(SalesInvoice.posting_date)
                .limit(4)
            )
        )
        .scalars()
        .all()
    )
    if not invoices:
        return "skipped — no unpaid sales invoice"

    statuses = ["Requested", "Requested", "Paid", "Cancelled"]
    made = 0
    for invoice, status in zip(invoices, statuses):
        pr = await pr_service.create_payment_request(
            db,
            PaymentRequestCreate(
                customer_id=invoice.customer_id,
                reference_invoice_id=invoice.id,
                posting_date=invoice.posting_date,
                due_date=invoice.due_date or (invoice.posting_date + timedelta(days=30)),
                amount=invoice.outstanding_amount,
                currency=invoice.currency,
                message=f"Payment request against invoice {invoice.name}. "
                "Use the secure link below or transfer to our HDFC current account.",
                payment_url=f"https://pay.optireach.local/r/{invoice.name.lower()}",
            ),
            ctx.actor,
        )
        if status != "Requested":
            await pr_service.set_status(db, pr.id, status, ctx.actor)
        made += 1
    await db.commit()
    return f"{made} payment requests"


SECTIONS = [
    ("rfq", seed_rfq_cycle),
    ("production_plan", seed_production_plan),
    ("job_cards", seed_job_cards),
    ("quality", seed_quality_inspections),
    ("subcontracting", seed_subcontract_job),
    ("service_credits", seed_service_credits),
    ("reorder", seed_reorder_levels),
    ("share_capital", seed_share_capital),
    ("subscriptions", seed_subscriptions),
    ("payment_requests", seed_payment_requests),
]


async def main() -> None:
    failures = 0
    for name, fn in SECTIONS:
        # one session per section: a rollback in one must not poison the next
        async with async_session_factory() as db:
            try:
                ctx = await _context(db)
                message = await fn(db, ctx)
                print(f"  · {name:<17} {message}")
            except Exception as exc:  # noqa: BLE001 — a bad section must not stop the rest
                await db.rollback()
                failures += 1
                print(f"  · {name:<17} FAILED: {type(exc).__name__}: {exc}")
    if failures:
        print(f"Showcase extras: {len(SECTIONS) - failures}/{len(SECTIONS)} sections seeded.")
        sys.exit(1)
    print(f"Showcase extras: all {len(SECTIONS)} sections seeded.")


if __name__ == "__main__":
    asyncio.run(main())
