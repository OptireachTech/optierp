"""Import every ORM model module so the mapper registry and ``Base.metadata``
are complete whenever ``app.models`` is imported.

This mirrors what ``migrations/env.py`` does for Alembic autogenerate, and is
required by the metadata engine (app.registry) and the descriptor-drift test,
which both rely on all models being registered.
"""

from app.models import (  # noqa: F401
    accounts,
    assets,
    buying,
    cm_planning,
    compliance,
    core,
    manufacturing,
    quality,
    secretarial,
    selling,
    statutory,
    stock,
    migration,
    tax_computation,
    tax_config,
    tax_corporate,
    tax_credits,
    tax_filings,
    tax_interest,
)

__all__ = [
    "accounts",
    "assets",
    "buying",
    "cm_planning",
    "compliance",
    "core",
    "manufacturing",
    "quality",
    "secretarial",
    "selling",
    "statutory",
    "stock",
    "migration",
    "tax_computation",
    "tax_config",
    "tax_corporate",
    "tax_credits",
    "tax_filings",
    "tax_interest",
]
