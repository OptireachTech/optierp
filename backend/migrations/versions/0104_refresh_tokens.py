"""Privacy Phase 0 — refresh-token revocation and reuse detection.

Before this migration, ``/auth/refresh`` (see ``app/api/v1/auth.py``) decoded
the presented refresh token, verified only its signature and expiry, and
minted a new one — there was nowhere to check whether that token had already
been logged out, or already used once (rotated) and was now being replayed.
A stolen refresh token stayed valid for its full 7-day life no matter what
the legitimate user did.

``refresh_tokens`` gives every issued refresh token a row (by its ``jti``) so
the app can revoke one (logout), detect reuse of an already-rotated one
(theft signal — the app responds by revoking every token for that user), and
distinguish "this token is fine" from "this token was replayed" from "this
user never had this token" without trusting the JWT payload alone.

Global (not company-scoped, not RLS'd): a refresh token authenticates a user
account across companies, the same reason ``users`` itself carries no
``company_id``.

Revision ID: 0104_refresh_tokens
Revises: 0103_rls_coverage
Create Date: 2026-09-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0104_refresh_tokens"
down_revision: Union[str, None] = "0103_rls_coverage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("creation", pg.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("jti", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("expires_at", pg.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", pg.TIMESTAMP(timezone=True)),
        sa.Column("revoked_reason", sa.String(30)),
        sa.Column("replaced_by_jti", pg.UUID(as_uuid=True)),
        sa.UniqueConstraint("jti", name="uq_refresh_tokens_jti"),
    )
    op.create_index("ix_refresh_tokens_jti", "refresh_tokens", ["jti"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    # Reaping expired rows (a scheduled job, Phase 4) filters on this; index now,
    # add the job when the rest of Phase 4's machine-identity work lands.
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_jti", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
