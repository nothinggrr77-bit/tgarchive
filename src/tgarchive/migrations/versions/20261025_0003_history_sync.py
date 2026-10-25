"""history sync: oldinga sinxronlash, kanal oynasi va izohlar uchun maydonlar

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25

``messages`` ga ustun ``op.add_column`` bilan (ALTER TABLE ADD COLUMN) qo'shiladi —
jadval qayta yaratilmaydi, shuning uchun FTS triggerlari saqlanib qoladi.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("linked_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("messages", sa.Column("comments_synced_id", sa.BigInteger(), nullable=True))
    op.add_column("chat_sync_state", sa.Column("remote_top_id", sa.BigInteger(), nullable=True))
    op.add_column("chat_sync_state", sa.Column("last_refresh_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_messages_reply_peer",
        "messages",
        ["reply_to_peer_id", "reply_to_msg_id"],
        sqlite_where=sa.text("reply_to_peer_id IS NOT NULL"),
        postgresql_where=sa.text("reply_to_peer_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_reply_peer", table_name="messages")
    with op.batch_alter_table("chat_sync_state") as batch_op:
        batch_op.drop_column("last_refresh_at")
        batch_op.drop_column("remote_top_id")
    with op.batch_alter_table("chats") as batch_op:
        batch_op.drop_column("linked_chat_id")
    # messages batch rejimida qayta yaratilsa FTS triggerlari yo'qoladi — shuning uchun
    # SQLite 3.35+ ning to'g'ridan-to'g'ri DROP COLUMN buyrug'i ishlatiladi.
    if op.get_context().dialect.name == "sqlite":
        op.execute("ALTER TABLE messages DROP COLUMN comments_synced_id")
    else:
        op.drop_column("messages", "comments_synced_id")
