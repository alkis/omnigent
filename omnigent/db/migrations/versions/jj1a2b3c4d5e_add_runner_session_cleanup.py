"""Retain explicit runner cleanup commands across disconnects and server restarts."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "jj1a2b3c4d5e"
down_revision: str | None = "ii1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the cleanup journal independently of conversation rows."""
    op.create_table(
        "runner_session_cleanup",
        sa.Column("workspace_id", sa.BigInteger, primary_key=True, server_default="0"),
        sa.Column("command_id", Uuid16(), primary_key=True),
        sa.Column("runner_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("delete_completed", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_runner_session_cleanup_runner", "runner_session_cleanup", ["workspace_id", "runner_id"]
    )


def downgrade() -> None:
    """Remove the cleanup journal."""
    op.drop_table("runner_session_cleanup")
