"""media archive: yuklanmaslik sababi, majburiy yuklash, fayl axlati

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25

``media`` jadvalida FTS triggerlari yo'q — oddiy ``add_column`` yetarli.
Avval ``skipped`` bo'lgan qatorlar sababsiz qolmasligi uchun ``pending`` ga qaytariladi:
media worker ularni joriy sozlamalar bo'yicha qayta ko'rib chiqadi.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.add_column("media", sa.Column("skip_reason", sa.String(length=32), nullable=True))
    op.add_column(
        "media", sa.Column("force_download", sa.Boolean(), nullable=False, server_default=sa.text("0"))
    )
    op.create_index(
        "ix_media_local_path",
        "media",
        ["local_path"],
        sqlite_where=sa.text("local_path IS NOT NULL"),
        postgresql_where=sa.text("local_path IS NOT NULL"),
    )
    op.execute("UPDATE media SET status = 'pending', status_note = NULL WHERE status = 'skipped'")

    op.create_table(
        "media_trash",
        sa.Column("id", PK, nullable=False),
        sa.Column("local_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_trash")),
        sa.UniqueConstraint("local_path", name=op.f("uq_media_trash_local_path")),
    )


def downgrade() -> None:
    op.drop_table("media_trash")
    op.drop_index("ix_media_local_path", table_name="media")
    with op.batch_alter_table("media") as batch_op:
        batch_op.drop_column("force_download")
        batch_op.drop_column("skip_reason")
