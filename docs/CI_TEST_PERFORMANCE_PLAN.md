# CI Test Performance Plan

> Diagnosis and remediation plan for slow CI (`.github/workflows/ci.yml`). Companion to
> [PRIVACY_IMPLEMENTATION_PLAN.md](PRIVACY_IMPLEMENTATION_PLAN.md) § 1, which put CI in place —
> this plan is about keeping it fast enough that people don't route around it.

## Diagnosis

CI runs one serial job: install deps, `alembic upgrade head`, `ruff check .`, then a single
`pytest` invocation covering the whole backend suite — 667 tests across 77 files (413 unit,
254 integration). No parallelization (`pytest-xdist` isn't installed).

The dominant cost isn't the test count, it's the fixture 229 of the 254 integration tests share.
`backend/tests/integration/conftest.py`'s `ctx` fixture is function-scoped, so **every one of
those 229 tests**, before it runs anything, pays:

1. `DROP SCHEMA public CASCADE` + `CREATE SCHEMA public`
2. `Base.metadata.create_all` — rebuilds all 241 tables from the ORM models
3. Recreates two Postgres trigger functions (GL balance check, GL immutability)
4. Seeds a currency, a role, and an admin user
5. Three HTTP round trips through the app: `/auth/login`, `/companies` create, `/auth/switch-company`

That's a full-schema rebuild repeated 229 times, serially, in one process. This is the highest-leverage
fix available and the one this plan starts with.

## Phases

### Phase 1 — session-scoped schema, per-test truncate (in progress)

Build the schema once per test-session; between tests, wipe data instead of rebuilding structure.

- Guard the destructive DDL (drop/create schema, `create_all`, trigger creation) behind an
  in-process "already built" flag, so it runs once for the whole `pytest` invocation.
- On every subsequent `ctx` call, replace the rebuild with one
  `TRUNCATE TABLE <all tables> RESTART IDENTITY CASCADE` statement instead.
- **Why `TRUNCATE`, not `DELETE FROM` per table:** `gl_entries` has a `BEFORE UPDATE OR DELETE`
  trigger (`fn_gl_entry_immutable`) that raises on any `DELETE`, by design (append-only ledger).
  `TRUNCATE` doesn't fire row-level triggers in Postgres, so it's not just faster than `DELETE`
  here — `DELETE` would actively break this schema. `RESTART IDENTITY` reproduces the
  "fresh sequence" behavior tests already rely on from the old drop/recreate.
- Trigger functions and extensions are DDL, not data — they survive `TRUNCATE` untouched, so they
  only need to be (re)created in the one-time branch.
- Per-test `engine.dispose()` stays as-is: it isn't about schema state, it's about not leaking
  asyncpg connections across the per-test event loop pytest-asyncio creates for each async test
  (`asyncio_mode = "auto"`, no `asyncio_default_fixture_loop_scope` override). Removing it would
  risk "attached to a different loop" failures for an unrelated reason — out of scope here.
- **Risk / non-goal:** this changes only how the fixture prepares state, not what any test
  asserts. If a test was implicitly relying on a *fully empty* database in some way `TRUNCATE`
  doesn't reproduce (e.g. leftover sequence state on a table not covered by `Base.metadata`), it
  will surface as a failure the first time the full suite runs post-change — expected, not a sign
  the approach is wrong.
- **Cross-file hazard found and fixed while implementing this:** `test_module01_flow.py` had its
  own `client` fixture doing an independent `DROP SCHEMA public CASCADE` + `create_all` — a
  second, incomplete copy of the same logic (no GL triggers, no `statutory` schema). This was
  invisible before Phase 1 because the old `ctx` unconditionally rebuilt everything on *every*
  call, so any `ctx`-based test running after `test_module01_flow.py`'s silently repaired what it
  had torn down. Once `ctx` stopped rebuilding unconditionally, that repair disappeared, which
  would have made suite-wide test order matter (GL triggers missing, or a hard failure truncating
  `statutory.*` tables that no longer exist, depending on which file ran first). Fixed by
  extracting the build-vs-reset decision into one shared `ensure_schema()` in
  `backend/tests/integration/conftest.py` and pointing both fixtures at it — `test_module01_flow.py`
  no longer duplicates schema-build logic at all.

**Status: verified against a live Postgres (2026-09-17).** Implemented in
`backend/tests/integration/conftest.py` and `backend/tests/integration/test_module01_flow.py` — see
Verification below for the run that confirmed it.

### Phase 2 — parallelize with `pytest-xdist` (attempted 2026-09-17, blocked — not shipped)

Schema-per-worker isolation is implemented; the CI step is **not** wired to `-n auto` (still plain
`pytest tests/integration`) because a live run under real concurrency found a second, unrelated
problem this plan didn't anticipate. Recorded here so the next attempt doesn't rediscover it from
scratch.

**Implemented (the isolation plumbing itself — this part works):**

- `pytest-xdist>=3.6` added to `[project.optional-dependencies].dev`.
- `app/core/config.py` — new `Settings.db_search_path: str | None`, test-only, never set outside
  a test run.
- `app/core/database.py` — when set, `engine`'s `connect_args` carries
  `{"server_settings": {"search_path": f"{db_search_path},public"}}` — **`public` has to stay in
  the path, not just the worker's own schema**: `ltree` (or any extension) installs into whichever
  schema existed first across every worker/run in this database, and `CREATE EXTENSION IF NOT
  EXISTS` is a database-wide no-op once it exists anywhere, so an unqualified reference to its type
  only resolves if `public` is still reachable. Found by running a single worker for real — without
  the `,public` fallback, schema build fails with `UndefinedObjectError: type "ltree" does not
  exist`, misleadingly suggesting the extension itself is broken.
- `backend/tests/conftest.py` — reads `PYTEST_XDIST_WORKER` (set by pytest-xdist in each worker's
  own process, before this module — and so before `app.core.database` — is ever imported) and sets
  `DB_SEARCH_PATH=test_{worker}` when present. Unset under plain `pytest` (no `-n`): behaviour is
  byte-for-byte unchanged from before this phase — reconfirmed with three full serial runs after
  landing this code (731 passed / 3 skipped / 0 failed each time).
- `backend/tests/integration/conftest.py` — `_SCHEMA` resolves to `test_{worker}` under xdist,
  `"public"` otherwise; `_build_schema`'s `DROP`/`CREATE SCHEMA` target that name explicitly.
- `statutory` schema deliberately stays shared, unparameterized: read-only catalogue (see
  `app/models/statutory.py`'s docstring), nothing writes to it, so a no-op TRUNCATE race is
  harmless — parameterizing it would need every `schema="statutory"` model to become dynamic for
  no isolation benefit.

**Blocked on (found during live verification, not fixed):**

1. **`app.core.database.engine` is a naive module-level singleton, and that's incompatible with
   real xdist concurrency independent of the schema work above.** Reproduced with
   `pytest tests/integration/test_refresh_token_reuse.py -n 2 -q`:
   `RuntimeError: Task ... got Future ... attached to a different loop`, immediately followed by
   `RuntimeError: Event loop is closed` when the pool tries to close the connection. **Confirmed
   independent of the schema-isolation change** — reproduces identically with `DB_SEARCH_PATH=""`
   (i.e. `connect_args` forced empty, the exact pre-Phase-2 code path). Root cause hypothesis, not
   yet confirmed: `pool_pre_ping=True`'s ping does async I/O against a pooled asyncpg connection
   that was opened under a *different* test's pytest-asyncio event loop (function-scoped, no
   `asyncio_default_fixture_loop_scope` override — see `_build_schema`'s docstring) — plausible
   under any multi-test run sharing one process, but only actually observed once xdist entered the
   picture. Candidate fixes, **none attempted yet**: `pool_pre_ping=False` for the test engine,
   `NullPool` (no cross-test connection reuse at all) for tests, or giving pytest-asyncio a
   session-scoped loop instead of the default per-test one. Whichever is chosen needs the same
   "run it for real against Postgres" discipline this whole exercise has been built on — don't ship
   a fix for this without reproducing the failure first and watching it disappear.
2. **A `DuplicateObjectError` on `trg_gl_entry_balance_check`** surfaced once (1) above was worked
   around enough to get further — `_build_schema` appeared to run more than once against the same
   worker schema within a single process. Not root-caused; could be a symptom of (1) (a fixture
   retrying after the loop error) rather than an independent third bug — re-diagnose *after* (1) is
   actually fixed, not in parallel with it.

**Do not re-enable `pytest tests/integration -n auto` in CI until both are resolved and reverified
live** — the isolation plumbing above being correct is necessary but not sufficient.

### Phase 3 — split CI into fast/slow jobs (implemented 2026-09-17)

`.github/workflows/ci.yml` is now two jobs, both required for merge:

- `unit-tests` — no services; installs deps, `ruff check .`, `pytest tests/unit`. Reports back in
  roughly the time `pip install -e ".[dev]"` takes, not after a Postgres/Redis container boot.
- `integration-tests` — the previous Postgres+Redis setup, `alembic upgrade head`, then
  `pytest tests/integration` — **plain, not `-n auto`**: Phase 2 above isn't safe to ship yet, so
  this job doesn't run any slower than before the split; the win here is purely `unit-tests`
  failing fast, independent of whatever Phase 2 ends up needing.

Ruff and the Alembic migration check moved to `unit-tests`/stayed with `integration-tests`
respectively — migrations need the `erp` database so that check stays in `integration-tests`.
This phase shipped independently of Phase 2 and doesn't depend on it landing.

### Phase 4 — baseline measurement (implemented 2026-09-17)

- **After** (Phase 1 landed, single process, no `-n`): `pytest -q --durations=25` — **731 passed, 3
  skipped, 0 failed in 784.56s (13:04)**. The slowest 25 entries are almost entirely fixture
  `setup` time (2.9s-4.2s per test, one outlier at 14.6s likely paying first-connection/JIT
  warmup), not test-body `call` time — confirms the diagnosis: per-test cost is now fixture setup
  (three HTTP round-trips + a TRUNCATE), not schema rebuilding, exactly what Phase 1 was supposed
  to leave as the remaining cost, and exactly what Phase 2's parallelization *would* target next,
  once it's actually working — see Phase 2's "Blocked on" list above.
- **Before** (pre-Phase-1 behaviour: full `DROP SCHEMA CASCADE` + `create_all` + trigger recreation
  on every one of ~230 shared-fixture tests) was not re-measured by reverting working code — the
  qualitative diagnosis at the top of this document (full schema rebuild x229) already establishes
  the effect size directionally, and reverting a verified fix just to produce a second number
  wasn't judged worth the risk of leaving the repo in a half-reverted state mid-measurement.
- A Phase-2 (`-n 2`) duration comparison was not captured: the run errored (see Phase 2's "Blocked
  on" list) before producing a meaningful wall-clock number to compare against the 784.56s above.

## Verification

Not run against a live Postgres in this environment: Docker Desktop's daemon isn't reachable
here, and something else already owns local port 5432 (unconfirmed whether it's disposable) —
running this fixture's `DROP SCHEMA public CASCADE` against an unverified database is not a risk
worth taking to save a manual check.

What was checked without a database:

- `ruff check` passes on the changed file.
- The module imports cleanly (no import-order regressions from moving `Base.metadata` access
  into helper functions).
- Importing `app.main` and walking `Base.metadata.sorted_tables` resolves all **241 tables**
  (matches the pre-change count) across the `public` and `statutory` schemas, and the generated
  `TRUNCATE` statement text builds correctly (~5.4 KB, one `statutory`-schema table set, well
  under any Postgres statement-size concern).
- SQLAlchemy emits a pre-existing `SAWarning` about an unresolvable FK cycle between `boms` and
  `items` when sorting tables — present before this change too (it's the same cycle the original
  code's "drop_all can't order DROPs across FK cycles" comment refers to). Harmless for
  `TRUNCATE ... CASCADE`, which doesn't depend on table order for correctness.

**Live run (2026-09-17):** `docker compose up -d postgres redis`, fresh `pip install -e ".[dev]"`
inside a one-off `docker compose run` container (mirrors CI's own install step exactly), then
`alembic upgrade head && pytest -q`. Ran **three separate times** against the same session (each a
full teardown/re-launch of the container, so each paid the one-time schema build fresh): **731
passed, 3 skipped, 0 failures** every time, no test-order or leftover-sequence-state flakiness
across runs — the two things Phase 1's own "Risk / non-goal" note above flagged as the way this
could have gone wrong. The `test_module01_flow.py` cross-file hazard fix (shared `ensure_schema()`)
held up too: both that file's `client` fixture and `ctx`-based tests ran in the same session without
either one silently repairing what the other tore down. **Phase 1 is solid** on this evidence.

**Phase 2 live run (2026-09-17) — this is where it stopped being solid.** Isolated each change with
its own targeted run rather than only testing the end state, which is what actually caught both
bugs below (a single "does -n 2 pass" run would have reported one opaque failure and left both
causes conflated):

1. `pytest tests/integration/test_refresh_token_reuse.py -n 2 -q` → `RuntimeError: ... attached to
   a different loop`, immediately followed by `RuntimeError: Event loop is closed`, before any
   schema DDL even ran.
2. Same command with `-e DB_SEARCH_PATH=""` (search_path plumbing forced off, i.e. the exact
   pre-Phase-2 connection setup) → **identical error** — proves (1) has nothing to do with the
   schema-isolation change; it's pre-existing, xdist-only fragility in `app.core.database.engine`.
3. `pytest tests/integration/test_refresh_token_reuse.py -n 1 -q` (before the `,public` search_path
   fallback fix) → different error, `UndefinedObjectError: type "ltree" does not exist` — a single
   worker got further than two concurrent ones, confirmed the schema-isolation change had its own,
   separate, real bug, and pinned down what it was.
4. Same command after adding the `,public` fallback → past the `ltree` error, onto
   `DuplicateObjectError: trigger "trg_gl_entry_balance_check" ... already exists` — not yet
   root-caused; see Phase 2's "Blocked on" list.

Net result: `.github/workflows/ci.yml`'s `integration-tests` job runs plain `pytest
tests/integration`, not `-n auto` — reverted immediately on finding (1), before it could reach a
real CI run. The schema-isolation code (`db_search_path`, `connect_args`, `_SCHEMA`) stays in the
tree — inert under plain `pytest` (confirmed by the three Phase-1 reruns above, all after this code
landed), and a documented starting point for whoever picks Phase 2 back up.
