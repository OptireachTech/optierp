"""RLS-coverage guard — every tenant-owned table must be isolated.

``CompanyScopedMixin`` (``app/models/base.py``) promises that a table using it
gets a ``company_isolation`` row-level-security policy, with
``FORCE ROW LEVEL SECURITY`` so the policy also binds the migration-owner role
(Section 4.1; see ``docs/DATA_PRIVACY_ARCHITECTURE.md``). That promise is only
as good as this test: RLS policies are raw SQL inside Alembic migrations, not
part of SQLAlchemy's metadata, so nothing about a missing policy shows up from
introspecting the ORM alone — a model can gain ``CompanyScopedMixin`` and the
migration author can simply forget the policy, exactly what happened to the 22
tables closed by migration 0103_rls_coverage.

Runs without a database: it statically scans every migration file for the
``ENABLE``/``FORCE ROW LEVEL SECURITY`` statements — literal calls, the
``for table in (...): op.execute(f"...")`` loop form, and a locally-defined
one-table-argument helper (several migrations write their own ``_rls(table)``
or ``_enable_rls(table)`` instead of inlining ``op.execute`` — the exact
mismatch between this scanner's first version and that third idiom is what
let migration 0103 try to re-create policies that already existed via one of
these helpers and fail in CI; see its commit history) — and compares the
covered set against every model that mixes in ``CompanyScopedMixin``. Same
introspection-only idiom as ``test_descriptor_drift.py``.
"""

import ast
from pathlib import Path

from app.models.base import Base, CompanyScopedMixin

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"

# ALTER TABLE ... RENAME TO carries RLS policies (and FORCE) across the rename
# (see 0102_data_migration_rename's docstring) — map old names appearing in
# older migrations' ENABLE/FORCE statements to the table's current name so the
# scan below doesn't report a table as uncovered just because it was renamed
# after its RLS policy was created. Add an entry here if a future migration
# renames another scoped table.
RENAMED_TABLES = {
    "tally_imports": "migration_imports",
    "tally_mappings": "migration_mappings",
    "tally_imported_documents": "migration_imported_documents",
}


def _scoped_tables() -> set[str]:
    return {
        mapper.class_.__table__.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, CompanyScopedMixin)
    }


def _sql_literal(node: ast.AST) -> str | None:
    """Best-effort flatten of a plain string or f-string node to its literal text,
    with ``{expr}`` placeholders left as ``{}`` — enough to regex out a fixed SQL verb."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _names_for(iter_node: ast.expr, consts: dict[str, list[str]]) -> list[str] | None:
    """Resolve a for-loop's iterable to a literal list of table-name strings, when possible."""
    if isinstance(iter_node, (ast.Tuple, ast.List)):
        return [e.value for e in iter_node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if isinstance(iter_node, ast.Name):
        return consts.get(iter_node.id)
    if isinstance(iter_node, ast.BinOp) and isinstance(iter_node.op, ast.Add):
        # e.g. `for table in GAP_TABLES + ALREADY_ENABLED_TABLES:`
        left = _names_for(iter_node.left, consts)
        right = _names_for(iter_node.right, consts)
        if left is not None and right is not None:
            return left + right
    return None


def _canonicalise(tables: set[str]) -> set[str]:
    return {RENAMED_TABLES.get(t, t) for t in tables}


def _local_rls_helpers(tree: ast.Module, src: str) -> dict[str, set[str]]:
    """Find file-local ``def helper(table): op.execute(f"...ENABLE/FORCE...")`` functions.

    Returns {function_name: {"ENABLE", "FORCE"} subset} for every single-argument
    function whose body issues an ``op.execute`` naming ROW LEVEL SECURITY using
    that argument — the ``_rls``/``_enable_rls`` idiom several migrations use
    instead of inlining ``op.execute`` directly.
    """
    helpers: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and len(node.args.args) == 1):
            continue
        body_src = ast.get_source_segment(src, node) or ""
        kinds = set()
        if "ENABLE ROW LEVEL SECURITY" in body_src:
            kinds.add("ENABLE")
        if "FORCE ROW LEVEL SECURITY" in body_src:
            kinds.add("FORCE")
        if kinds:
            helpers[node.name] = kinds
    return helpers


def _scan_migrations() -> tuple[set[str], set[str]]:
    """Return (tables with ENABLE ROW LEVEL SECURITY, tables with FORCE ROW LEVEL SECURITY)."""
    import re

    enabled: set[str] = set()
    forced: set[str] = set()
    statement_re = re.compile(r"ALTER TABLE (\w+) (ENABLE|FORCE) ROW LEVEL SECURITY")

    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        # Module-level tuple/list-of-strings constants, e.g. RLS_TABLES = (...) or
        # GAP_TABLES: tuple[str, ...] = (...) — both plain and annotated assignments.
        consts: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.value, (ast.Tuple, ast.List)):
                targets, value = [node.target], node.value
            if value is None:
                continue
            vals = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if vals:
                for target in targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = vals

        helpers = _local_rls_helpers(tree, src)

        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                names = _names_for(node.iter, consts)
                if not names:
                    continue
                for sub in ast.walk(node):
                    if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "execute" and sub.args):
                        continue
                    text = _sql_literal(sub.args[0])
                    if text and "ENABLE ROW LEVEL SECURITY" in text and "{" in text:
                        enabled.update(names)
                    if text and "FORCE ROW LEVEL SECURITY" in text and "{" in text:
                        forced.update(names)

            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "execute" and node.args:
                text = _sql_literal(node.args[0])
                m = statement_re.match(text) if text else None
                if m:
                    table, kind = m.group(1), m.group(2)
                    (enabled if kind == "ENABLE" else forced).add(table)

            # A call to one of this file's local RLS helpers with a literal table name.
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in helpers and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                table = node.args[0].value
                for kind in helpers[node.func.id]:
                    (enabled if kind == "ENABLE" else forced).add(table)

    enabled, forced = _canonicalise(enabled), _canonicalise(forced)

    # A table can be granted RLS in one migration and dropped in a later one
    # (e.g. 0084_tax_adjustment_engine's tables, dropped by
    # 0092_drop_legacy_itr) — the scan above sees every mention across all of
    # history and has no notion of "then it was deleted". Filter against the
    # ORM's current table set (after canonicalising renames, or a renamed
    # table's old name would get filtered out here instead of mapped) — the
    # only reliable source of "still exists". Otherwise a migration written
    # from this scanner's output can try to FORCE ROW LEVEL SECURITY on a
    # table Postgres no longer has, exactly what happened with migration
    # 0103's first fix — see its commit history.
    existing_tables = set(Base.metadata.tables.keys())
    return enabled & existing_tables, forced & existing_tables


def test_scanner_finds_known_coverage():
    """Sanity check on the scanner itself — if this drops to zero, the AST scan broke,
    not the migrations; don't let the coverage tests below pass vacuously."""
    enabled, forced = _scan_migrations()
    assert len(enabled) > 100, f"expected >100 tables with ENABLE ROW LEVEL SECURITY, found {len(enabled)}"
    assert len(forced) > 100, f"expected >100 tables with FORCE ROW LEVEL SECURITY, found {len(forced)}"


def test_every_scoped_table_has_rls_enabled():
    enabled, _ = _scan_migrations()
    missing = _scoped_tables() - enabled
    assert not missing, (
        f"{len(missing)} CompanyScopedMixin table(s) have no company_isolation RLS policy: "
        f"{sorted(missing)}. Add ENABLE ROW LEVEL SECURITY + CREATE POLICY company_isolation "
        f"in a migration (see 0103_rls_coverage.py for the pattern)."
    )


def test_every_scoped_table_forces_rls():
    _, forced = _scan_migrations()
    missing = _scoped_tables() - forced
    assert not missing, (
        f"{len(missing)} CompanyScopedMixin table(s) never get FORCE ROW LEVEL SECURITY: "
        f"{sorted(missing)}. Without FORCE, RLS does not bind the table owner (erp_owner) — "
        f"add ALTER TABLE ... FORCE ROW LEVEL SECURITY in a migration."
    )
