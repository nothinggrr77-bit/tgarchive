"""account binding (app_meta) va chats.archive_mode

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25

``app_meta.account_id`` bazani bitta Telegram akkauntiga bog'laydi: shaxsiy chat
xabar ID'lari faqat bitta akkaunt doirasida noyob, o'chirish hodisalarini to'g'ri
talqin qilish shunga tayanadi.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_meta",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_meta")),
    )
    with op.batch_alter_table("chats") as batch_op:
        batch_op.add_column(sa.Column("archive_mode", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chats") as batch_op:
        batch_op.drop_column("archive_mode")
    op.drop_table("app_meta")
