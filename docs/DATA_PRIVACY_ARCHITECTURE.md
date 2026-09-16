# Data Privacy & Security Architecture

> Approved design. Execution phases, CI enforcement gates and the DPDP alignment map live in
> [PRIVACY_IMPLEMENTATION_PLAN.md](PRIVACY_IMPLEMENTATION_PLAN.md). Plain-language explainer and
> the published artifacts (with diagrams) are linked at the bottom of this file.

## 1. The model

Don't encrypt uniformly — classify data, then control who (and what process) can see it.
Encryption is the last mile, not the primary mechanism. This is an accounting/compliance system:
amounts, dates, account links and `docstatus` are the working set of every trial balance, GST
return and stock ledger. Encrypting those breaks the double-entry triggers, the report engine and
every indexed aggregate for a threat ciphertext-at-rest already covers. Match control to threat
instead:

| Tier | What | Protection |
|---|---|---|
| 0 — Baseline | everything | disk/backup encryption, TLS in transit |
| 1 — Operational & financial | GL entries, invoices, stock ledger, amounts, dates, account links | RLS + RBAC; kept plaintext and indexed — reports and reconciliation depend on it |
| 2 — Identifying | email, phone, address, PAN, bank account/IBAN | field-level RBAC now; app-layer encryption + blind-index search later (Phase 6) |
| 3 — Restricted | KYC blobs, DIN, DOB, identity documents | encrypted, RBAC-gated, every read audited |

Roughly 30 columns land in Tier 2/3, not thousands. Reports, GST returns and the double-entry
engine are untouched.

Envelope encryption (when Phase 6 lands): `KMS root → per-tenant KEK → per-tenant DEK → AES-256-GCM
per field`, AAD-bound to `tenant_id ‖ table ‖ column ‖ row_id`. Application layer, not `pgcrypto` —
the key must never transit the SQL connection or land in `pg_stat_statements`/WAL. Per-tenant DEKs
give per-tenant crypto-shredding and a clean seat for customer-held keys (BYOK) later without a
rewrite — only the indirection (`tenant KEK`) needs to point at a customer's own KMS instead of
ours.

## 2. Prerequisite — isolation integrity

> **Status: closed by migration `0103_rls_coverage` (Phase 0, 2026-09-15).** Left as a record of
> what the gap was and how it was verified — see [Implementation order](#10-implementation-order).

None of the access-control design matters if the tenant boundary under it leaks.

- `app.company_id` is set as a transaction-local Postgres GUC per request and re-armed on every
  new transaction (`backend/app/core/database.py`); RLS policies read it. Design is sound.
- **Coverage was not.** A precise scan (every `CompanyScopedMixin` model cross-referenced against
  every migration's `ENABLE ROW LEVEL SECURITY` statement — the same check `test_rls_coverage.py`
  now runs on every PR) found 148 company-scoped tables, 126 with a `company_isolation` policy and
  **22 with none**, relying on application-layer `WHERE company_id = …` alone — including
  `bank_transactions`, `shareholders`, `share_transfers`, `assets` and `subscriptions`.
  (First pass at this scan mis-flagged 25 more as gaps — see the correction note below.)
- `FORCE ROW LEVEL SECURITY` was **never set, anywhere**. Any session running as `erp_owner`
  (migrations, a script, a pooler misconfiguration) bypassed RLS silently, on *every* table —
  not just the 22.

Fixed: `company_isolation` policy on the 22 gap tables, `FORCE ROW LEVEL SECURITY` on all 148, and
`backend/tests/unit/test_rls_coverage.py` — an introspection-only CI guard, same idiom as
`test_descriptor_drift.py` — failing the build when a `CompanyScopedMixin` model gains no matching
migration policy from here on.

**Correction (caught by CI, not by review):** the migration's first version put 47 tables in its
gap list. Deploying it failed in CI with `DuplicateObjectError: policy "company_isolation" for
table "cm_cost_rates" already exists` — 25 of the 47 already had a policy. Several migrations
(mostly the tax and contribution-margin modules) grant RLS through a locally-defined
`_rls(table)` or `_enable_rls(table)` helper function instead of inlining `op.execute` — a third
idiom the original scan didn't recognise, alongside the literal and loop forms it did. Re-scanning
with that idiom added found the true count: 22, not 47. `test_rls_coverage.py` now recognises all
three call shapes, so this exact class of false positive fails the guard test before a migration
is even written, rather than surfacing as a runtime `DuplicateObjectError` in CI.

Known residual gap (not closed by this migration): the integration test suite builds its schema
via `Base.metadata.create_all` (`tests/integration/conftest.py`), which does not run Alembic's raw
`CREATE POLICY` SQL — so no integration test today actually exercises RLS enforcement end-to-end
against a live tenant boundary. `test_rls_coverage.py` proves every table is *configured* correctly;
it cannot prove a second tenant is actually blocked at query time. Closing that gap means either
teaching the integration fixture to apply migrations instead of `create_all`, or a dedicated
RLS-enforcement integration test connecting as `erp_app` — tracked as follow-up work, not yet
scheduled to a phase.

## 3. Data & document classification

One taxonomy for both database columns and uploaded files.

Document sensitivity is set once at upload, from a small controlled vocabulary — not inferred by
a classifier:

- **General business document** — Tier 1 by default.
- **Financial evidence** — invoice/receipt/bank-statement scans linked via
  `reference_doctype`/`reference_id`; Tier 1–2 depending on content (bank statements are Tier 2).
- **Confidential** — board minutes, resolutions, related-party detail; Tier 2.
- **Restricted/KYC** — identity proofs, DSC material, director KYC; always Tier 3.

`SecretarialFile` (`backend/app/models/secretarial/masters.py`) already carries a `category`
column and a `storage_backend` seam meant for exactly this — generalize it into a shared
`Document`/`Attachment` model rather than replacing it.

## 4. Access control & delegation

### Field-level sensitivity

Current RBAC (`role_permissions`: role × doctype × action, plus `if_owner` —
`backend/app/core/permissions.py`) is doctype-level only: a role with `read` on Customer gets every
column, PAN included. Fix: a `sensitivity` tag (`standard` / `pii` / `restricted`) on each field,
plus two new flags on `role_permissions` — `can_read_pii`, `can_read_restricted`. Masking happens
once, at the single choke point both API responses and audit snapshots already pass through —
`serialize_document()` in `backend/app/services/audit.py`.

### Identity minimization — adopt the intent, not the literal mechanism

Operating on `Client ID → Invoice → Amount → Tax` without always exposing identity is right as a
*viewing* principle, wrong as a *storage* principle. Swapping real customer/contact FKs for derived
tokens in transactional tables breaks GST invoices (must show the legal buyer name and GSTIN) and
the audit trail, for no gain field-masking doesn't already give. The FK stays real; what changes is
who can dereference it down to name/PAN/email/address. A reconciliation job, an AI categorization
service, or a junior bookkeeper's dashboard should be written to operate on `customer_id`,
`account_id`, amount and tax — never pulling name/PAN/email/address into its working set. Where
that data crosses to an *external* AI provider, this becomes an enforced gateway (§6), not a
discipline.

### CA/CS collaboration — generalize the engagement ladder, don't rebuild it

`SecretarialEngagement` (`backend/app/models/secretarial/engagement.py`) already solves this well:
a bilateral, entity-scoped, revocable grant with a graduated financial-access ladder.

```
none → derived_only (+ Facts) → reports_read (+ Reports) → ledger_read (+ Ledger detail)
                                                              ↳ + Banking / KYC docs — opt-in, off by default
```

Invariants worth keeping exactly as-is: only the client-side tenant may grant/revoke; a projected
role is always company-scoped (never global — would leak into every other tenant that user
touches); no rung grants write, enforced structurally (the seeded financial roles carry `can_read`
and nothing else).

Gap: this ladder exists **only for the secretarial module**. A CA firm doing bookkeeping today gets
ordinary, all-or-nothing `user_roles` — no ladder, no `include_banking`-style guardrail. Fix: widen
`SecretarialEngagement` into a module-agnostic `Engagement` with a `scope` field (`secretarial` /
`accounting` / `both`), reusing the ladder, role-projection and revoke logic as-is rather than
building a parallel `AccountingEngagement`.

## 5. Document storage & the OCR/AI pipeline

`Document → OCR/AI → Transaction → Accounting entries`, with the original always retrievable as
evidence from the resulting ERP record. Nothing is auto-posted — extraction fills a review grid,
exactly as `backend/app/services/ocr.py` already does today.

- **Object storage, not database blobs.** Generalize `SecretarialFile`'s `storage_backend` seam
  from `db` to real object storage: private bucket, no public ACLs, SSE-KMS baseline; access only
  through short-lived backend-minted presigned URLs, never a durable bucket URL to the frontend.
  Tier-3 documents get an additional app-layer envelope-encryption pass before upload.
- **View / download / export as three separate permissions** — a permission check, not new
  infrastructure. Export deserves the tightest limit: it's the realistic bulk-exfiltration path,
  not single-record viewing.
- **OCR/AI egress control, per tenant.** `ocr.py` already gets two things right: nothing persists
  from a model response without human review, and `ocr_log_raw_response` defaults off because "the
  reply contains the document's contents." Move OCR/AI enablement from a global setting to
  per-tenant (pattern: `gst_settings.py`), and require a separate, explicit consent flag before any
  Tier-3 document reaches an external vendor.

## 6. Machine identity, credentials & vendors

### Service identity

Background jobs mostly respect tenancy (`secretarial_reminders.py` calls `set_company_context()`
per tenant like a request would). The depreciation job doesn't: `process_depreciation()`
(`backend/app/jobs/assets.py::_resolve_actor_user`) picks a real employee — the company owner, or
any user with a role in the company, or any System Manager — and builds a `CurrentUser` in-process
with `roles: ["System Manager"]` (the platform superuser role) to act as them. Two problems: a bug
or compromise in that job carries superuser privilege into every tenant it touches that night, and
the audit trail permanently misattributes the posting to a person who never acted. Fix: a
dedicated, narrowly-scoped system-actor identity per job family (e.g. `System Job Runner:
Depreciation`), using the existing `users`/`roles`/`user_roles` machinery — no new auth path.

### Credential lifecycle

> **Status:** refresh-token row closed by migration `0104_refresh_tokens` + `app/services/
> auth_sessions.py` (Phase 0, 2026-09-15). JWT signing-key rotation and the vendor-key secret
> manager remain open — scheduled with Phase 4 (service identity), below.

| Credential | Today | Gap | Fix |
|---|---|---|---|
| Access token (JWT) | 15 min | none — short life is the mitigation | keep as-is |
| Refresh token (JWT, httpOnly cookie) | ~~7 days; rotated on use, old token never invalidated~~ **fixed**: every issued token is row-tracked by `jti`; `/auth/refresh` revokes the presented token and mints a replacement; replaying an already-rotated token revokes every session for that user; `/auth/logout` revokes server-side, not just the cookie | — | shipped |
| JWT signing key (`secret_key`) | static, never rotated | rotating it today logs out every user on every tenant at once, so nobody does it | verify against a short list of active keys during a rotation window |
| Vendor API keys (OCR, SMTP, GSP, storage) | plain env vars, no schedule | a leaked key has no expiry, no revocation trail | one secret manager for all of them (same one as Phase 6 field encryption), rotation runbook (e.g. 90 days, or immediately on suspected compromise) |

### Rate limiting & abuse protection

> **Status:** `/auth/login` and `/auth/refresh` closed by `app/core/rate_limit.py` (Phase 0,
> 2026-09-15) — Redis-backed, fails open on a Redis outage (availability over strict enforcement,
> matching this app's existing posture toward Redis). The other four surfaces below are unstarted;
> each belongs to the phase that ships the surface it protects (document storage → Phase 2, OCR
> pipeline → Phase 3), not to Phase 0.

Nothing beyond `/auth/*` and `ocr.py` handling the *vendor's* own 429s exists today. Redis is
already provisioned (websocket pub/sub, now also rate limiting) — reuse it rather than adding
infrastructure.

| Surface | Why | Shape |
|---|---|---|
| `/auth/login`, `/auth/refresh` | credential stuffing, currently unlimited | ~~per (email, IP) attempt cap with backoff~~ **shipped**: per-IP fixed-window cap (10/60s, configurable) |
| Document `download` | pulls a tenant's files one at a time under any single-request check | per-user request-rate cap |
| Document/report `export` (bulk) | the real exfiltration path — a permission check alone doesn't bound volume | tight cap + alert on threshold |
| `/registry/{doctype}` | generic list/search via `ilike` across a whole tenant | moderate per-user cap |
| OCR/AI extract | every call sends a document externally — cost *and* egress amplifier | per-tenant cap, tied to the OCR opt-in |

### AI data governance — minimum necessary, enforced by code

Binding rule: external AI providers get only the minimum data a task needs; customer, vendor,
employee and personal identifiers stay inside OptiReach by default and are replaced with internal
aliases before anything leaves; identity is resolved back only inside OptiReach; PAN, Aadhaar-type
identifiers, bank details, contact info, addresses, KYC documents and credentials never reach an
external provider without a named, approved feature governed by its own policy.

`backend/app/services/ocr.py` is, today, the **only** place in the backend that makes an outbound
AI call — the right moment to build the mandatory choke point before a second one exists.

- **Structured calls** (future: reconciliation matching, categorization, a copilot) go through an
  **allow-list**, not a block-list: only fields explicitly marked safe for export (Tier 1) cross by
  real value. A Tier 2/3 field is aliased or dropped by default — a new sensitive column added
  later is unreachable unless deliberately allow-listed.
- **Aliasing** reuses the per-tenant key material planned for Phase 6 rather than a second
  tokenization system: a deterministic HMAC of `(tenant_id, entity_type, real_id)` under the
  tenant's key gives a stable, meaningless-outside-OptiReach alias — stable enough for a matching
  model to recognize repeat entities without ever learning who they are. Resolution back to the
  real record happens only inside OptiReach, after the response returns.
- **Document/image calls** (OCR) can't be aliased this way — the identifier is pixels on a scanned
  page, which is the extraction target itself. This is the directive's own named-exception carve-
  out; Tier-3/KYC document types are **hard-blocked at the gateway**, not merely defaulted off.
- **Vendor approval is a gate**, checked before a call is allowed to leave — confirmed no-training
  terms, strictest available retention, a DPA where offered. Fails closed if the vendor isn't
  approved.

### Vendor register

| Vendor | Sends | Must confirm |
|---|---|---|
| OCR/AI vision API (OpenAI-compatible, `gpt-4o-mini` default) | one document image/PDF + static prompt — no customer master data, no other tenant's documents | written zero-retention/no-training terms per account; enable any explicit no-training flag |
| SMTP (Mailhog in dev; production provider open) | invoice/statement content, recipient name & email, tokenised director portal links | signed DPA, bounded log retention |
| GSP/IRP/NIC (pluggable per tenant — e-invoicing, e-way bill, GSTR filing) | invoice/e-way-bill payloads — mandatory regulatory flow | credentials in a secure store (already the stated intent in `backend/app/services/gsp.py`'s docstring, not yet implemented) |
| Object storage (new) | uploaded document bytes | contractual deletion guarantee, stated data-residency region |

## 7. Auditability, retention & deletion

- `serialize_document()` currently writes every column verbatim into `audit_logs.data_before/after`
  — an unencrypted, permanent, append-only second copy of every PII field. Redact Tier 2/3 fields
  in that snapshot (store a change marker + hash, not the value).
- **Reads are not audited today** — only creates/updates/deletes. Add a lightweight read-audit
  event for Tier-3 access (doctype, id, user, action=`READ`, no payload).
- Statutory retention (GL, invoices, filings) and DPDP-style minimization pull in different
  directions — resolve by data class, not a blanket policy:

| Data class | Retention | Deletion |
|---|---|---|
| Financial/transactional records | statutory period, non-negotiable | none while the legal-obligation basis applies |
| Evidence linked to a live transaction | as long as the transaction is within its statutory window | same as above |
| Standalone PII with no statutory record | tenant-configurable | real delete, or key rotation/crypto-shred once Tier 2/3 fields carry per-tenant keys |
| General business documents | tenant-configurable | ordinary delete |

## 8. Evaluating the original team proposals

| Proposal | Verdict | Reasoning |
|---|---|---|
| Multi-tenant + RBAC | Adopt | correct foundation, already built — ship §2's coverage fix first |
| Access by tenant/company/role/user/module/action | Adopt | already the shape of `role_permissions`; add sensitivity as one more dimension |
| Minimize unnecessary PII exposure | Adopt | the Tier 2/3 field-masking mechanism |
| Use internal identifiers instead of real identity | Adopt the intent, not the storage mechanism | see §4 |
| Stronger protection for KYC/highly sensitive data | Adopt | Tier 3: encryption, restricted RBAC, mandatory read-audit |
| Avoid encrypting all financial/operational data | Adopt | the single most important call to get right |
| Apply protection selectively by sensitivity | Adopt | the whole tiering model |
| Businesses control access for employees & external professionals | Adopt, generalize | widen the existing CS engagement ladder rather than build a parallel system |
| Classify documents by sensitivity | Adopt | small vocabulary set at upload, not a per-file classifier |
| Secure object storage with controlled access | Adopt | private bucket, SSE-KMS, presigned URLs only |
| Separate view/download/export controls | Adopt | cheap, targets the actual exfiltration risk |
| Maintain access and security audit trails | Adopt, extend | write-side leaks plaintext today; read-side doesn't exist yet |
| Machine/service identity for OCR, AI, workers, internal services | Adopt | the depreciation job's borrowed superuser identity is a live gap |
| Credential lifecycle — issuance, rotation, expiry, revocation | Adopt | access tokens are right; everything longer-lived has no revocation path |
| Rate limiting / abuse protection | Adopt | nothing exists today; Redis is already in the stack |
| Third-party/vendor security | Adopt | four real vendor surfaces exist or are landing; formalize as a gate, not a record |

Not on the original list, but load-bearing: the isolation-coverage gap in §2. No amount of
role/sensitivity design matters if dozens of tenant tables have no database-level guarantee behind
the application filter. (Closed — see §2.)

## 9. Where this lands in the code

| File / model | Change |
|---|---|
| `migrations/versions/0103_rls_coverage.py`, `0104_refresh_tokens.py` | ✅ shipped (Phase 0) — `company_isolation` policy + `FORCE ROW LEVEL SECURITY` on the 22 gap tables; `FORCE` added to the 133 tables that already had a policy (126 `CompanyScopedMixin`, plus 7 non-tenant-scoped tables — `secretarial_engagements` and six reference tables — that already carried their own); new `refresh_tokens` table |
| `app/registry/base.py` — `FieldSpec` | add `sensitivity: str` (no default — see implementation plan §3) |
| `app/models/core.py` — `RolePermission` | add `can_read_pii`, `can_read_restricted` booleans |
| `app/services/audit.py` | add `redact_for_audit()` beside `serialize_document()`; add `log_read()` for Tier-3 access |
| `app/models/secretarial/masters.py` — `SecretarialFile` | generalize into a shared `Document` model; wire `storage_backend="s3"` |
| `app/services/ocr.py` | accept a `document_id` sourced from object storage, alongside the existing raw-upload path |
| `app/models/secretarial/engagement.py` | widen `SecretarialEngagement` to a module-agnostic `Engagement` with a `scope` field |
| `app/core/config.py` | move OCR enablement to per-company settings; move vendor credentials to a secret manager |
| new — `app/services/documents.py` | presigned URL issuance; view/download/export permission checks |
| `app/jobs/assets.py::_resolve_actor_user` | replace the borrowed human/System-Manager identity with a scoped system-actor user |
| `app/core/security.py` | refresh-token `jti` tracking, invalidate-on-rotate, reuse detection |
| new — rate-limit middleware | Redis-backed, tiered by endpoint class |
| new — `app/services/ai_gateway.py` | the mandatory choke point for outbound AI calls — allow-list/alias for structured payloads, named-exception + Tier-3 hard-block for document calls, approved-vendor check; `ocr.py` becomes its first caller |

## 10. Implementation order

| Phase | Focus |
|---|---|
| 0 ✅ | Isolation & session integrity — RLS gap, `FORCE RLS`, refresh-token revocation, auth rate limits. Shipped 2026-09-15, see `docs/PRIVACY_IMPLEMENTATION_PLAN.md` §2 for the full report. |
| 1 | Classification & field-level RBAC |
| 2 | Document storage — object store, view/download/export split |
| 3 | OCR/AI pipeline wired to stored documents |
| 4 | Service identity, credential hygiene, AI gateway |
| 5 | CA/CS delegation generalized across modules |
| 6 | Envelope encryption for Tier 2/3 — deliberately last; Phases 0–5 already remove most of the risk |

See [PRIVACY_IMPLEMENTATION_PLAN.md](PRIVACY_IMPLEMENTATION_PLAN.md) for the CI foundation, the
enforcement guard tests, the data-principal-rights workstream (Phase 7), and the DPDP alignment
map.

---

Published artifacts (with diagrams, for sharing outside the repo):
- [Full architecture, with diagrams](https://claude.ai/code/artifact/739708fe-c916-4225-9a3a-7fa544eb81a1)
- [Plain-language one-pager](https://claude.ai/code/artifact/9e0b843b-deab-426d-a4a6-cb634fd8a85f)
- [Implementation plan, with diagrams](https://claude.ai/code/artifact/cb925aa8-4cb2-4b6a-a7fa-54e7a864cb89)
