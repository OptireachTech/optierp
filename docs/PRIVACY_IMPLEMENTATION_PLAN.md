# Privacy Implementation Plan

> Execution plan for [DATA_PRIVACY_ARCHITECTURE.md](DATA_PRIVACY_ARCHITECTURE.md). Turns the
> approved architecture into shipped controls and a standing engineering practice — every future
> module, API, integration, document workflow, AI capability and database change is expected to
> pass through the same gates.

**Scope boundary.** This plan builds and enforces the *engineering* controls that support a
DPDP-compliant posture — isolation, classification, access control, audit, retention, AI
governance. It is not itself legal compliance. Consent/notice content, the Data Protection
Officer decision, breach-notification runbooks and grievance-process ownership are legal/
organisational decisions, marked **\*** throughout rather than assumed.

## 1. The missing foundation: CI

> **Status: shipped 2026-09-15.** `.github/workflows/ci.yml` now runs on every PR/push into
> `main`/`develop`: provisions Postgres + Redis, applies the Alembic migration chain (catches a
> broken migration before merge), runs `ruff check .`, then the full `pytest` suite — unit *and*
> integration, not just the DB-free unit tests, since ~50 integration test files self-skip without
> a real Postgres and would otherwise let CI report green while quietly skipping most of the suite.

Every guard test below only enforces anything if something runs it on every pull request and
blocks the merge on failure. That something didn't exist: there was no `.github/workflows/`
directory and no pre-commit config anywhere in the repo. `ruff` and `pytest` both ran — only when
a developer remembered to run them locally. Everything else in this plan assumes CI now exists.

## 2. Execution workstreams

The architecture's seven phases, expanded into a definition of done and an automated guard, plus
one new phase this exercise surfaced. Effort is sized relatively (S/M/L) — calendar mapping
depends on team capacity, which this plan doesn't guess at.

| Phase | Deliverable | Definition of done | Automated guard | Effort |
|---|---|---|---|---|
| Foundation ✅ | CI pipeline | every PR runs lint + full test suite; merge blocked on red | `.github/workflows/ci.yml` | S |
| 0 ✅ | Isolation & session integrity | all 22 gap tables carry a policy + `FORCE RLS` (148 tables total now forced); refresh tokens revocable; `/auth/login`+`/auth/refresh` rate-limited | `test_rls_coverage.py` | M |
| 1 | Classification & field-level RBAC | every field touching known-sensitive data carries a tier; `can_read_pii`/`can_read_restricted` enforced; audit snapshot redacted | `test_sensitivity_coverage.py`, `test_audit_redaction.py` | M |
| 2 | Document storage | `Document` model generalized off `SecretarialFile`; bytes in object storage; view/download/export are separate permissions | manual review — no automated guard for storage-migration correctness | L |
| 3 | OCR/AI pipeline on stored documents | upload → store → extract → review → evidence-linked; Tier-3 document types refuse at the gateway, not a flag | `test_ai_call_sites.py` | M |
| 4 | Service identity, credentials, AI gateway | background jobs use a scoped system-actor role; vendor keys in a secret manager; `ai_gateway.py` is the only outbound AI call site | `test_ai_call_sites.py` | L |
| 5 | CA/CS delegation, generalized | `Engagement` (scope: secretarial/accounting/both) replaces the secretarial-only model with no loss of ladder guarantees | reuses existing engagement test suite, extended | M |
| 6 | Envelope encryption, Tier 2/3 | per-tenant KMS-wrapped keys live; blind-index search works for exact-match fields; crypto-shred verified end-to-end | integration test — key rotation + shred against a real KMS sandbox | L |
| **7 — new** | Data principal rights | a verified requester can retrieve, correct, or request erasure of their own data footprint (§4) | manual review — identity-verification policy is a prerequisite, not a test | L |

### Foundation + Phase 0 — completion report (2026-09-15)

**Changed:**

- `.github/workflows/ci.yml` — new. Postgres + Redis services, migration apply, `ruff check`, full `pytest`.
- `backend/migrations/versions/0103_rls_coverage.py` — new. `company_isolation` policy on the 22
  gap tables (re-scanned down from an original estimate of 59, then a first-pass migration of 47 —
  see the correction note below and `docs/DATA_PRIVACY_ARCHITECTURE.md` §2); `FORCE ROW LEVEL
  SECURITY` on all 148.
- `backend/migrations/versions/0104_refresh_tokens.py` — new. `refresh_tokens` table.
- `backend/app/models/core.py` — new `RefreshToken` model.
- `backend/app/services/auth_sessions.py` — new. Issue/rotate/revoke refresh tokens; reuse detection.
- `backend/app/core/security.py` — `create_refresh_token` now embeds and returns a `jti`.
- `backend/app/core/rate_limit.py` — new. Redis-backed fixed-window limiter, fails open.
- `backend/app/core/config.py` — `auth_rate_limit_enabled/attempts/window_seconds`.
- `backend/app/core/exceptions.py` — new `RateLimitedError` (429, `ERR_RATE_LIMITED`).
- `backend/app/api/v1/auth.py` — `login`/`refresh`/`logout`/`switch-company` wired to the above;
  rate limiting on `login`+`refresh`; `switch-company` now rotates the refresh token instead of
  minting a second concurrently-valid one.
- `backend/tests/unit/test_rls_coverage.py` — new guard test (introspection-only, no DB).
- `backend/tests/integration/test_refresh_token_reuse.py`, `test_login_rate_limit.py` — new.
- `backend/tests/conftest.py` — `AUTH_RATE_LIMIT_ENABLED=false` test default (avoids the shared
  `ctx` fixture's logins tripping the limiter once Redis is live in CI).

**Tests run:** full unit suite (`pytest tests/unit`, no DB required) — 455 passed, including the 3
new RLS-coverage assertions. `ruff check` — clean. The 4 new integration tests self-skip in this
environment (no local Postgres/Redis) and need the CI run above, or a local
`docker compose up -d postgres redis`, to actually execute — see the architecture doc's residual-gap
note in §2 for what they do and don't prove.

**Correction, found by the first real CI run:** the PR opened for this work failed migration
`0103` with `DuplicateObjectError: policy "company_isolation" for table "cm_cost_rates" already
exists`. Root cause: the coverage scan (and the guard test built on it) only recognised two of the
three ways this codebase grants RLS — a literal `op.execute` and the `for table in (...):` loop —
and missed a third: several migrations (the tax module and contribution-margin planning,
specifically) define their own `_rls(table)` or `_enable_rls(table)` helper function and call it
per table instead of inlining `op.execute`. 25 of the original 47 "gap" tables already had a
policy through one of those helpers; the migration tried to create a second `company_isolation`
policy on each and Postgres correctly refused. Fix: `test_rls_coverage.py` now detects any
file-local single-argument function whose body issues an ENABLE/FORCE statement, and treats a
literal-argument call to it the same as a direct statement. Re-scanning with that fixed found the
real gap: 22 tables, not 47. `0103_rls_coverage.py` was rewritten with the corrected lists — see
its own docstring for the full account. This is exactly the failure mode CI exists to catch before
merge, not after a manual review; it did.

**Second correction, same CI run:** the fix above hit a *different* failure on the next run —
`UndefinedTableError: relation "tax_adjustment_provisions" does not exist`. Four tables had been
RLS'd via `_rls()` in migration `0084_tax_adjustment_engine.py` and later dropped outright by
`0092_drop_legacy_itr.py` (a clean-break tax-module rewrite); the scan accumulates every mention
across all of migration history with no notion of "then it was deleted", so it kept treating them
as covered and the migration tried to `FORCE ROW LEVEL SECURITY` on tables Postgres no longer had.
Fix: `_scan_migrations()` now intersects its result against `Base.metadata.tables` — the ORM's
live table set — before returning, so a dropped table can never reappear as "already covered".
The real already-covered count dropped from 133 to 129; the 22-table gap itself was unaffected.

**Known limitation carried forward, not fixed here:** the integration test fixture builds its schema
via `Base.metadata.create_all`, which doesn't run Alembic's `CREATE POLICY` SQL — so RLS enforcement
itself still has no live-database test proving a second tenant is actually blocked. `test_rls_coverage.py`
proves configuration, not runtime enforcement. Flagged, not scheduled to a phase yet.

## 3. The enforcement layer

Not a project that ends — a standing practice every future change is measured against, in order
of how early each mechanism catches a problem.

### 3.1 Fail at registration time, not at test time

The strongest guarantee is one a developer can't skip by forgetting to run a test.
`FieldSpec.sensitivity` (`app/registry/base.py`) should carry **no default** — registering a
descriptor without deciding a field's tier becomes a Python-level error at import time, the same
moment `test_descriptor_drift.py` already catches a descriptor that has drifted from its model.
That existing test — its own docstring calls it "the single most important safeguard for the
engine" — is the pattern to extend, not replace.

### 3.2 Automated guards on every pull request

| Guard | What it checks | Lives in |
|---|---|---|
| `test_rls_coverage.py` | every model using `CompanyScopedMixin` has a matching `company_isolation` policy and `FORCE ROW LEVEL SECURITY` across the migrations — introspection only, no database, same idiom as the existing descriptor-drift guard | `tests/unit` |
| `test_sensitivity_coverage.py` | every bespoke Pydantic schema field matching a known-sensitive name (email, phone, pan, bank, iban, din, dob, kyc, address…) carries an explicit tier — the safety net for fields outside the metadata engine that §3.1 can't reach | `tests/unit` |
| `test_audit_redaction.py` | a fixture row with a Tier-2/3 field set, run through `serialize_document`/`redact_for_audit`, never surfaces the raw value | `tests/unit` |
| `test_ai_call_sites.py` | static scan — the only module importing `httpx` to call an external AI-shaped host is `app/services/ai_gateway.py`; a new file making its own outbound AI call fails the build | `tests/unit` |
| `test_login_rate_limit.py`, `test_refresh_token_reuse.py` | `/auth/login` throttles after N attempts; replaying a spent refresh-token `jti` is rejected and kills the session | `tests/integration` |

### 3.3 A human gate for what a test can't judge

Coverage and redaction are checkable mechanically; whether a *new* integration, AI feature, or
export capability is a good idea is not. The team already runs a `/security-review` pass over
pending changes — a `/privacy-review` pass (or an extension of that skill) should run the same
way, triggered whenever a PR:

- adds a table or column holding personal or business-identifying data
- calls a new external service, or adds a new named exception through the AI gateway
- adds a new document-view, download or export surface
- changes what a CA/CS engagement rung can see

**Pipeline:** PR opens → automated guards (§3.2) run in CI → if the PR trips one of the four
triggers above, a Data Protection Design Review is required before merge; otherwise it merges once
guards are green → a quarterly audit (§5) reviews vendor terms, access grants and coverage drift,
opening follow-up PRs as needed.

### 3.4 The five questions every change answers

1. **Does this touch personal or business-sensitive data?** Classify it before merge — §3.1 forces
   this for engine-served fields.
2. **Does this add a company-scoped table?** The RLS policy and `FORCE ROW LEVEL SECURITY` ship in
   the same migration, not a follow-up.
3. **Does this call an external service?** AI calls go through the gateway; anything else joins the
   approved vendor register before it's wired in.
4. **Does this expose a way to view, download, or export data?** Rate limiting and audit logging
   apply — export gets the tightest limit of the three.
5. **Does this touch a Tier-2/3 field or a KYC document?** Confirm the RBAC flag exists and reads
   are logged, not just writes.

## 4. Data principal rights — access, correction, erasure

The one capability the architecture didn't plan for, and the part of "DPDP by design" that's a
product workflow, not a database control.

**Who's actually responsible for what.** For data a tenant business enters about its own
customers, vendors or employees, that tenant is the data fiduciary — OptiReach provides the tool,
it doesn't stand between the tenant and their customer. A central OptiReach support queue actioning
every tenant's customer requests would be both operationally unworkable and the wrong party
answering. The exception is OptiReach's own platform-level data (login accounts, platform admin
records), where OptiReach itself is the fiduciary. The design follows from that split: this ships
as **tenant-facing self-service tooling**, not a central request queue.

- **Look up a data footprint.** A tenant admin searches by name, email, phone or PAN (via the
  Tier-2 blind index from Phase 6) and gets back every record referencing that person — Customer,
  Contact, SecretarialPerson, uploaded documents referencing them — tagged by tier.
- **Access.** Export a plain report of what's held about that person.
- **Correction.** Routes to the normal edit permission on each underlying record — no new write
  path, just a guided entry point.
- **Erasure.** Runs against the retention rules already designed (architecture §7): a record tied
  to a live statutory obligation is reported back with that basis stated, not silently refused; a
  standalone record with no statutory tie is actually deleted, or crypto-shredded if it's Tier 2/3
  material under a per-tenant key.
- **Identity verification of the requester\*** — a policy decision for legal/product to set before
  this ships, not an engineering default. The tool supports whatever verification step is decided,
  rather than assuming one.

## 5. Ongoing governance & ownership

| Owns | Primary owner type | Cadence |
|---|---|---|
| Guard tests passing, classification coverage, RLS coverage | engineering — a named privacy/data-protection lead | continuous (CI) + quarterly report |
| Vendor register — no-training terms, DPA status, retention | engineering lead + procurement/legal sign-off | quarterly re-confirmation |
| Active CA/CS engagements and their access rung | engineering lead, flagged to account owners | quarterly — flag anything at `ledger_read`/banking that looks stale |
| Named AI-gateway exceptions (document OCR and any future ones) | engineering lead — logged in an exceptions register | quarterly — still needed, still approved? |
| Consent/notice content, DPO designation\*, breach runbook\*, grievance process\* | legal/compliance | set once, reviewed on any DPDP rule change |

**Exceptions register** — the one new artifact this introduces beyond code: a short, living log,
one line per named exception (`document OCR sends Tier-1/2 images to Vendor X, approved by
[name] on [date]`), so every deliberate deviation from default-deny is visible and reviewable
instead of buried in a code comment nobody revisits.

## 6. DPDP alignment map

What the engineering controls support, and what still needs a legal/organisational decision —
kept as separate columns on purpose.

| Principle | Engineering control | Status | Still needs legal/org work\* |
|---|---|---|---|
| Purpose limitation & data minimisation | tiered classification; AI gateway allow-list; internal services operate on identifiers, not identity | designed — Phases 1, 4 | confirm documented processing purpose per feature |
| Security safeguards | RLS, field-level RBAC, envelope encryption (Tier 2/3), credential lifecycle, rate limiting | designed — Phases 0, 1, 4, 6 | — |
| Storage limitation / retention | tier-specific retention; statutory-record carve-out; crypto-shred path | designed — architecture §7 | tenant-configurable retention periods need sign-off per data class |
| Data principal rights — access, correction, erasure | tenant-facing lookup, export, correction routing, tiered erasure | planned — §4 above | requester identity-verification policy |
| Notice & consent | — | not an engineering control | notice content and consent capture — product/legal workstream |
| Grievance redressal | audit trail supports investigating any complaint | partially supported | a named contact and process |
| Breach notification | audit trail + mandatory Tier-3 read logging gives the forensic basis | partially supported | notification timelines and runbook |
| Data Protection Officer / significant-fiduciary obligations | — | N/A | whether these apply, and who holds the role, is a legal/org determination |
| Cross-border transfer | object-storage vendor's stated data-residency region | partially addressed — vendor register | confirm any restricted-transfer scenarios |

---

Published artifact (with diagrams): [Privacy Implementation Plan](https://claude.ai/code/artifact/cb925aa8-4cb2-4b6a-a7fa-54e7a864cb89)
