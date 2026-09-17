"""Populate Secretarial so a human can click through Phases 0–4.

``seed_secretarial_demo`` only creates logins and turns the module flag on. Without
this step every register, meeting and calendar is empty, and unpublished draft rules
make Generate look broken.

Idempotent. Safe to re-run.

    python -m scripts.seed_secretarial_scenario
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEED_DATABASE_URL")
        or os.environ.get("MIGRATIONS_DATABASE_URL")
        or os.environ.get("DATABASE_URL"),
    )
    return parser.parse_args()


ARGS = _parse_args()
if ARGS.database_url:
    os.environ["DATABASE_URL"] = ARGS.database_url

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory, set_company_context  # noqa: E402
from app.core.security import CurrentUser  # noqa: E402
from app.models.core import Company, User  # noqa: E402
from app.models.secretarial import (  # noqa: E402
    SecretarialAppointment,
    SecretarialAuditor,
    SecretarialBeneficialOwner,
    SecretarialCharge,
    SecretarialCircular,
    SecretarialCommittee,
    SecretarialComplianceRule,
    SecretarialContentPack,
    SecretarialDocument,
    SecretarialDsc,
    SecretarialEngagement,
    SecretarialEntity,
    SecretarialFiling,
    SecretarialGroupLink,
    SecretarialMeeting,
    SecretarialMember,
    SecretarialPerson,
)
from app.schemas.secretarial import (  # noqa: E402
    AppointmentCreate,
    AuditorCreate,
    BeneficialOwnerCreate,
    ChargeCreate,
    CommitteeCreate,
    DscCreate,
    EngagementCreate,
    EntityCreate,
    EntityUpdate,
    GenerateCalendarIn,
    GroupLinkCreate,
    MemberCreate,
    PersonCreate,
)
from app.services.secretarial import circular as circular_service  # noqa: E402
from app.services.secretarial import compliance as compliance_service  # noqa: E402
from app.services.secretarial import documents as document_service  # noqa: E402
from app.services.secretarial import engagement as engagement_service  # noqa: E402
from app.services.secretarial import entity as entity_service  # noqa: E402
from app.services.secretarial import filings as filing_service  # noqa: E402
from app.services.secretarial import financial_facts as facts_service  # noqa: E402
from app.services.secretarial import meeting as meeting_service  # noqa: E402
from app.services.secretarial import persons as person_service  # noqa: E402
from app.services.secretarial import registers as register_service  # noqa: E402
from app.services.secretarial import roster as roster_service  # noqa: E402

FY = "2025-26"
REVIEWER = "Demo Reviewer (placeholder — NOT a real sign-off)"
REVIEWER_CRED = "ACS 00000"


def _actor(user: User, company_id) -> CurrentUser:
    return CurrentUser(
        {
            "sub": str(user.id),
            "email": user.email,
            "company_id": str(company_id),
            "roles": ["System Manager", "Company Secretary"],
        }
    )


async def _company(db, name: str) -> Company:
    row = await db.scalar(select(Company).where(Company.company_name == name))
    if row is None:
        raise SystemExit(f"Company '{name}' not found — run scripts.seed_showcase first")
    return row


async def _user(db, email: str) -> User:
    row = await db.scalar(select(User).where(User.email == email))
    if row is None:
        raise SystemExit(f"User '{email}' not found — run scripts.seed_secretarial_demo first")
    return row


async def _publish_catalogues(db) -> None:
    """Sign off the seeded draft rules and packs so calendars and PDFs actually run."""
    now = datetime.now(UTC)
    today = date.today()
    published = 0
    for model in (SecretarialComplianceRule, SecretarialContentPack):
        rows = list(
            (
                await db.execute(
                    select(model).where(
                        model.company_id.is_(None), model.review_status != "published"
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.review_status = "published"
            row.reviewer_name = REVIEWER
            row.reviewer_credential = REVIEWER_CRED
            row.reviewed_on = today
            row.published_at = now
            if getattr(row, "effective_from", None) is None:
                row.effective_from = date(2014, 4, 1)
            published += 1
    await db.commit()
    print(f"  published {published} statutory artefacts (placeholder reviewer)")


async def _person(db, actor, *, din: str | None, full_name: str, **extra) -> SecretarialPerson:
    q = select(SecretarialPerson).where(SecretarialPerson.company_id == actor.company_id)
    q = q.where(SecretarialPerson.din == din) if din else q.where(SecretarialPerson.full_name == full_name)
    existing = await db.scalar(q)
    if existing:
        return existing
    return await person_service.create_person(
        db,
        PersonCreate(full_name=full_name, din=din, nationality="Indian", kyc_status="verified", **extra),
        actor,
    )


async def _appoint(db, actor, entity_id, person, role_type: str, designation: str, **flags) -> None:
    exists = await db.scalar(
        select(SecretarialAppointment.id).where(
            SecretarialAppointment.entity_id == entity_id,
            SecretarialAppointment.person_id == person.id,
            SecretarialAppointment.role_type == role_type,
            SecretarialAppointment.ceased_on.is_(None),
        )
    )
    if exists:
        return
    await person_service.create_appointment(
        db,
        AppointmentCreate(
            entity_id=entity_id,
            person_id=person.id,
            role_type=role_type,
            designation=designation,
            appointed_on=date(2021, 4, 1),
            **flags,
        ),
        actor,
    )


async def _seed_business(db, mango: Company, admin: User, firm: Company, cs: User) -> SecretarialEntity:
    actor = _actor(admin, mango.id)
    await set_company_context(db, mango.id)
    await entity_service.ensure_bootstrap(db, mango.id, actor)

    entity = await db.scalar(
        select(SecretarialEntity).where(
            SecretarialEntity.company_id == mango.id,
            SecretarialEntity.linked_company_id == mango.id,
        )
    )
    if entity is None:
        raise SystemExit("Business entity was not bootstrapped")

    await entity_service.update_entity(
        db,
        entity.id,
        EntityUpdate(
            cin="U29308MH2019PTC325441",
            email="secretarial@mangoappliances.com",
            phone="+91 22 4000 1200",
            registered_office={
                "line1": "12, Andheri Industrial Estate",
                "city": "Mumbai",
                "state": "Maharashtra",
                "pincode": "400053",
            },
        ),
        actor,
    )
    entity = await db.get(SecretarialEntity, entity.id)

    priya = await _person(
        db, actor, din="07123456", full_name="Priya Sharma",
        email="priya.sharma@mangoappliances.com", occupation="Managing Director",
        pan="AABPS1234C",
    )
    rahul = await _person(
        db, actor, din="08234567", full_name="Rahul Mehta",
        email="rahul.mehta@mangoappliances.com", occupation="Director",
        pan="AABPM5678D",
    )
    anita = await _person(
        db, actor, din="09345678", full_name="Anita Desai",
        email="anita.desai@mangoappliances.com", occupation="Company Secretary",
        pan="AABPD9012E",
    )
    await _appoint(db, actor, entity.id, priya, "director", "Managing Director", is_signing=True, is_chairperson=True)
    await _appoint(db, actor, entity.id, rahul, "director", "Non-Executive Director", is_signing=True)
    await _appoint(db, actor, entity.id, anita, "secretary", "Company Secretary", is_signing=True)
    print("  directors: Priya Sharma, Rahul Mehta; CS: Anita Desai")

    if not await db.scalar(select(SecretarialMember.id).where(SecretarialMember.folio_no == "M-001")):
        await register_service.create_row(
            db, SecretarialMember,
            MemberCreate(
                entity_id=entity.id, member_name="Priya Sharma", folio_no="M-001",
                person_id=priya.id, pan="AABPS1234C", shares_held=Decimal("60000"),
                share_class="Equity", joined_on=date(2019, 6, 12), holding_as_on=date(2026, 3, 31),
                is_beneficial_owner=True,
            ),
            actor,
        )
    if not await db.scalar(select(SecretarialMember.id).where(SecretarialMember.folio_no == "M-002")):
        await register_service.create_row(
            db, SecretarialMember,
            MemberCreate(
                entity_id=entity.id, member_name="Rahul Mehta", folio_no="M-002",
                person_id=rahul.id, pan="AABPM5678D", shares_held=Decimal("40000"),
                share_class="Equity", joined_on=date(2019, 6, 12), holding_as_on=date(2026, 3, 31),
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialCommittee.id).where(
            SecretarialCommittee.entity_id == entity.id,
            SecretarialCommittee.committee_name == "Audit Committee",
        )
    ):
        await register_service.create_committee(
            db,
            CommitteeCreate(
                entity_id=entity.id, committee_name="Audit Committee",
                committee_type="audit", constituted_on=date(2020, 4, 1), quorum=2,
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialGroupLink.id).where(
            SecretarialGroupLink.entity_id == entity.id,
            SecretarialGroupLink.related_entity_name == "Mango Holdings Pvt Ltd",
        )
    ):
        await register_service.create_row(
            db, SecretarialGroupLink,
            GroupLinkCreate(
                entity_id=entity.id, related_entity_name="Mango Holdings Pvt Ltd",
                related_cin="U67120MH2015PTC100001", relation="holding",
                shareholding_pct=Decimal("60"), valid_from=date(2019, 6, 12),
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialAuditor.id).where(
            SecretarialAuditor.entity_id == entity.id,
            SecretarialAuditor.firm_name == "Shah & Shah LLP",
        )
    ):
        await register_service.create_row(
            db, SecretarialAuditor,
            AuditorCreate(
                entity_id=entity.id, firm_name="Shah & Shah LLP",
                registration_no="123456W", auditor_type="statutory",
                appointed_on=date(2022, 9, 30), term_from_fy="2022-23", term_to_fy="2026-27",
                email="audit@shahandshah.in",
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialCharge.id).where(
            SecretarialCharge.entity_id == entity.id,
            SecretarialCharge.holder_name == "HDFC Bank Ltd",
        )
    ):
        await register_service.create_row(
            db, SecretarialCharge,
            ChargeCreate(
                entity_id=entity.id, holder_name="HDFC Bank Ltd",
                charge_type="hypothecation", amount_secured=Decimal("25000000"),
                property_description="Plant and machinery at Andheri works",
                created_on=date(2023, 8, 15), status="open", srn="T12345678",
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialDsc.id).where(
            SecretarialDsc.company_id == mango.id, SecretarialDsc.serial_no == "DSC-PRIYA-2025"
        )
    ):
        await register_service.create_row(
            db, SecretarialDsc,
            DscCreate(
                entity_id=entity.id, person_id=priya.id, holder_name="Priya Sharma",
                serial_no="DSC-PRIYA-2025", issuing_authority="eMudhra",
                issued_on=date(2025, 1, 10), expires_on=date(2027, 1, 9), status="active",
                custodian="Anita Desai, CS",
            ),
            actor,
        )

    if not await db.scalar(
        select(SecretarialBeneficialOwner.id).where(
            SecretarialBeneficialOwner.entity_id == entity.id,
            SecretarialBeneficialOwner.person_name == "Priya Sharma",
        )
    ):
        await register_service.create_row(
            db, SecretarialBeneficialOwner,
            BeneficialOwnerCreate(
                entity_id=entity.id, person_id=priya.id, person_name="Priya Sharma",
                classification="sbo", holding_pct=Decimal("60"),
                nature_of_interest="Controlling shareholder", valid_from=date(2019, 6, 12),
                declared_on=date(2023, 9, 30),
            ),
            actor,
        )

    await register_service.sync_related_parties(db, entity.id, actor)
    print("  registers: members, committee, group, auditor, charge, DSC, BO, related parties")

    cal = await compliance_service.generate_calendar(
        db, GenerateCalendarIn(entity_id=entity.id, fy=FY), actor
    )
    print(
        f"  calendar {FY}: created={cal.items_created} refreshed={cal.items_refreshed} "
        f"n/a={cal.items_skipped_not_applicable} unpublished_ignored={cal.unpublished_rules_ignored}"
    )

    if not await db.scalar(
        select(SecretarialMeeting.id).where(
            SecretarialMeeting.entity_id == entity.id,
            SecretarialMeeting.title == "Board meeting — Q2 FY 2025-26",
        )
    ):
        meeting = await meeting_service.create_meeting(
            db, actor,
            entity_id=entity.id, meeting_type="board",
            scheduled_at=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
            venue="Registered office, Andheri",
            title="Board meeting — Q2 FY 2025-26",
            mode="physical", chairperson_id=priya.id, fy=FY,
        )
        await meeting_service.set_agenda(
            db, meeting.id,
            [
                {"title": "Leave of absence", "body": "Note absences, if any."},
                {
                    "title": "Appointment of additional director",
                    "body": "Consider appointing an additional director under s.161.",
                    "pack_code": "director-appointment",
                },
                {"title": "Any other business", "body": "With the permission of the Chair."},
            ],
            actor,
        )
        print("  meeting: Board meeting — Q2 FY 2025-26 (draft, with agenda)")

    if not await db.scalar(
        select(SecretarialCircular.id).where(
            SecretarialCircular.entity_id == entity.id,
            SecretarialCircular.title == "Engagement of interior consultant",
        )
    ):
        await circular_service.create_circular(
            db, actor,
            entity_id=entity.id,
            title="Engagement of interior consultant",
            resolution_text=(
                "RESOLVED THAT the Company engage Kala Studio, Mumbai, to design the "
                "registered-office reception at a fee not exceeding Rs. 4,00,000."
            ),
            description="Ordinary commercial contract — not a Rule 5 matter.",
        )
    if not await db.scalar(
        select(SecretarialCircular.id).where(
            SecretarialCircular.entity_id == entity.id,
            SecretarialCircular.title == "Approval of financial statements (blocked demo)",
        )
    ):
        blocked = await circular_service.create_circular(
            db, actor,
            entity_id=entity.id,
            title="Approval of financial statements (blocked demo)",
            resolution_text=(
                "RESOLVED THAT the audited financial statements and the Board's Report "
                "for the year ended 31 March 2026 be and are hereby approved."
            ),
        )
        print(
            f"  circulars: one eligible draft; one Rule-5 blocked "
            f"(eligible={blocked.eligibility_result.get('eligible') if blocked.eligibility_result else '?'})"
        )

    if not await db.scalar(
        select(SecretarialDocument.id).where(
            SecretarialDocument.entity_id == entity.id,
            SecretarialDocument.pack_code == "director-appointment",
        )
    ):
        try:
            await document_service.generate(
                db, actor,
                entity_id=entity.id, pack_code="director-appointment", fragment="resolution",
                form_data={
                    "appointee_name": "Rahul Mehta",
                    "appointee_din": "08234567",
                    "appointment_date": "01 April 2021",
                    "designation": "Non-Executive Director",
                },
                title="Board resolution — appointment of Rahul Mehta",
                document_date=date(2021, 4, 1),
            )
            print("  document: director-appointment resolution (PDF-ready)")
        except Exception as exc:  # noqa: BLE001
            print(f"  document skipped: {exc}")

    if not await db.scalar(
        select(SecretarialFiling.id).where(
            SecretarialFiling.entity_id == entity.id,
            SecretarialFiling.form_code == "DIR-12",
        )
    ):
        await filing_service.record(
            db, actor,
            entity_id=entity.id, form_code="DIR-12", fy=FY,
            srn="F12345678", filed_on=date(2021, 4, 20), status="filed",
            filing_fee=Decimal("600"), filed_by="Anita Desai",
            notes="Appointment of Rahul Mehta as director.",
        )
        print("  filing: DIR-12 filed (SRN F12345678)")

    live = await db.scalar(
        select(SecretarialEngagement.id).where(
            SecretarialEngagement.entity_id == entity.id,
            SecretarialEngagement.firm_company_id == firm.id,
            SecretarialEngagement.status.in_(("pending", "active")),
        )
    )
    if live is None:
        engagement = await engagement_service.create_engagement(
            db,
            EngagementCreate(
                entity_id=entity.id, firm_company_id=firm.id,
                financial_access="ledger_read", grant_to_user_ids=[cs.id],
                notes="Demo grant so the CS practice login can switch into this company.",
            ),
            actor,
        )
        await engagement_service.activate_engagement(db, engagement.id, actor)
        print("  engagement: Mango → OptiReach Demo (active, ledger_read)")

    await set_company_context(db, firm.id)
    await roster_service.refresh(db, firm.id)
    await set_company_context(db, mango.id)

    return entity


async def _seed_practice(db, firm: Company, admin: User) -> None:
    actor = _actor(admin, firm.id)
    await set_company_context(db, firm.id)

    async def managed(name: str, payload: EntityCreate) -> SecretarialEntity:
        existing = await db.scalar(
            select(SecretarialEntity).where(
                SecretarialEntity.company_id == firm.id,
                SecretarialEntity.entity_name == name,
            )
        )
        if existing:
            return existing
        return await entity_service.create_entity(db, payload, actor)

    textiles = await managed(
        "Sunrise Textiles Pvt Ltd",
        EntityCreate(
            entity_name="Sunrise Textiles Pvt Ltd", kind="company", entity_class="private",
            cin="U17120GJ2018PTC200002", email="cs@sunrisetextiles.in",
            incorporated_on=date(2018, 2, 14),
            registered_office={"city": "Surat", "state": "Gujarat", "pincode": "395003"},
        ),
    )
    llp = await managed(
        "Mehta Advisory LLP",
        EntityCreate(
            entity_name="Mehta Advisory LLP", kind="llp", entity_class="llp",
            llpin="AAB-1234", email="partners@mehtaadvisory.in",
            incorporated_on=date(2020, 11, 1),
            registered_office={"city": "Pune", "state": "Maharashtra", "pincode": "411001"},
        ),
    )

    partner = await _person(
        db, actor, din="06451234", full_name="Suresh Mehta",
        email="suresh@mehtaadvisory.in", occupation="Designated Partner",
    )
    await _appoint(db, actor, llp.id, partner, "designated_partner", "Designated Partner", is_signing=True)

    director = await _person(
        db, actor, din="05551234", full_name="Kavita Shah",
        email="kavita@sunrisetextiles.in", occupation="Director",
    )
    await _appoint(db, actor, textiles.id, director, "director", "Director", is_signing=True, is_chairperson=True)

    facts = await db.scalar(
        select(SecretarialEntity.id).where(SecretarialEntity.id == textiles.id)
    )
    if facts:
        await facts_service.set_manual_facts(
            db, textiles.id, FY,
            {
                "turnover": 8_500_000,
                "net_profit": 1_200_000,
                "net_worth": 6_000_000,
                "paid_up_capital": 1_000_000,
                "borrowings": 2_000_000,
                "notes": "Typed in — Sunrise is a managed client, books are not in this tenant.",
            },
            actor,
        )

    cal = await compliance_service.generate_calendar(db, GenerateCalendarIn(fy=FY), actor)
    await roster_service.refresh(db, firm.id)
    print(
        f"  practice clients: Sunrise Textiles (company) + Mehta Advisory (LLP); "
        f"calendar created={cal.items_created} n/a={cal.items_skipped_not_applicable}"
    )


async def main() -> None:
    async with async_session_factory() as db:
        mango = await _company(db, "Mango Appliances Demo")
        firm = await _company(db, "OptiReach Demo Pvt Ltd")
        admin = await _user(db, os.environ.get("ADMIN_EMAIL", "admin@example.com").lower())
        cs = await _user(db, "cs@optireachsecretarial.com")
        demo = await db.scalar(select(User).where(User.email == "demo@optireach.in"))
        practice_user = demo or admin

        print("Publishing statutory catalogues (placeholder sign-off)…")
        await _publish_catalogues(db)

        print("Mango Appliances Demo (business):")
        await _seed_business(db, mango, admin, firm, cs)

        print("OptiReach Demo Pvt Ltd (practice):")
        await _seed_practice(db, firm, practice_user)

    print(
        "\nReady to click through. Logins:\n"
        "  owner@mangoappliances.com / Demo!Pass123   — business shell, full registers\n"
        "  cs@optireachsecretarial.com / Demo!Pass123 — practice shell, two managed clients\n"
        "  admin@example.com / ChangeMe!123           — Mango, System Manager\n"
        "  demo@optireach.in / Demo@12345             — OptiReach Demo tenant\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
