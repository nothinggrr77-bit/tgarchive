"""initial schema: chatlar, xabarlar, versiyalar, media, checkpoint, audit, FTS5

Revision ID: 0001
Revises:
Create Date: 2026-10-25

Migratsiya ilova kodiga bog'liq emas (muzlatilgan): enum'lar VARCHAR(32), vaqt
DATETIME (naive UTC) sifatida yoziladi — bu models.py dagi turlar bilan bir xil saqlanish.

DIQQAT: keyingi migratsiyada ``messages`` jadvali batch rejimida qayta yaratilsa
(SQLite'da ustun turini o'zgartirish va h.k.), uning FTS triggerlari ham o'chadi —
o'sha migratsiyada ``_FTS_TRIGGERS`` ni qayta yarating.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM = sa.String(length=32)
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# fts.normalize_search_text bilan bir xil: tutuq belgisining barcha shakllari -> '
_APOSTROPHE_CODEPOINTS = (0x2018, 0x2019, 0x02BB, 0x02BC, 0x60, 0xB4)


def _norm(expr: str) -> str:
    for cp in _APOSTROPHE_CODEPOINTS:
        expr = f"replace({expr}, char({cp}), '''')"
    return expr


_FTS_TABLE = (
    "CREATE VIRTUAL TABLE messages_fts USING fts5("
    "body, content='', tokenize='unicode61 remove_diacritics 2')"
)

_FTS_TRIGGERS = (
    f"""
    CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages
    WHEN new.text IS NOT NULL AND new.text <> ''
    BEGIN
        INSERT INTO messages_fts(rowid, body) VALUES (new.id, {_norm("new.text")});
    END
    """,
    f"""
    CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages
    WHEN old.text IS NOT NULL AND old.text <> ''
    BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, {_norm("old.text")});
    END
    """,
    f"""
    CREATE TRIGGER messages_fts_au AFTER UPDATE OF text ON messages
    WHEN old.text IS NOT new.text
    BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, body)
            SELECT 'delete', old.id, {_norm("old.text")} WHERE old.text IS NOT NULL AND old.text <> '';
        INSERT INTO messages_fts(rowid, body)
            SELECT new.id, {_norm("new.text")} WHERE new.text IS NOT NULL AND new.text <> '';
    END
    """,
)


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_type", ENUM, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("is_protected", sa.Boolean(), nullable=False),
        sa.Column("migrated_to_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("archive_override", sa.Boolean(), nullable=True),
        sa.Column("media_override", sa.Boolean(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_chats")),
    )

    op.create_table(
        "messages",
        sa.Column("id", PK, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("is_outgoing", sa.Boolean(), nullable=False),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column("edit_date", sa.DateTime(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("reply_to_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_peer_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_top_id", sa.BigInteger(), nullable=True),
        sa.Column("fwd_from_id", sa.BigInteger(), nullable=True),
        sa.Column("fwd_from_name", sa.String(length=255), nullable=True),
        sa.Column("fwd_date", sa.DateTime(), nullable=True),
        sa.Column("fwd_channel_post_id", sa.BigInteger(), nullable=True),
        sa.Column("grouped_id", sa.BigInteger(), nullable=True),
        sa.Column("media_type", ENUM, nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("noforwards", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("edit_count", sa.Integer(), nullable=False),
        sa.Column("deletion_status", ENUM, nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("source", ENUM, nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], name=op.f("fk_messages_chat_id_chats"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_id_message_id"),
    )
    op.create_index("ix_messages_chat_date", "messages", ["chat_id", "date"])
    op.create_index("ix_messages_date", "messages", ["date"])
    op.create_index("ix_messages_sender_date", "messages", ["sender_id", "date"])
    op.create_index("ix_messages_message_id", "messages", ["message_id"])
    for name, columns, where in (
        ("ix_messages_reply", ["chat_id", "reply_to_msg_id"], "reply_to_msg_id IS NOT NULL"),
        ("ix_messages_media_type_date", ["media_type", "date"], "media_type IS NOT NULL"),
        ("ix_messages_edit_date", ["edit_date"], "edit_date IS NOT NULL"),
        ("ix_messages_deleted_at", ["deleted_at"], "deletion_status IS NOT NULL"),
    ):
        op.create_index(
            name, "messages", columns, sqlite_where=sa.text(where), postgresql_where=sa.text(where)
        )

    op.create_table(
        "message_versions",
        sa.Column("id", PK, nullable=False),
        sa.Column("message_pk", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("media_type", ENUM, nullable=True),
        sa.Column("edit_date", sa.DateTime(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("replaced_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_pk"],
            ["messages.id"],
            name=op.f("fk_message_versions_message_pk_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_versions")),
        sa.UniqueConstraint("message_pk", "version_no", name="uq_message_versions_message_pk_version_no"),
    )

    op.create_table(
        "media",
        sa.Column("id", PK, nullable=False),
        sa.Column("message_pk", sa.BigInteger(), nullable=False),
        sa.Column("media_type", ENUM, nullable=False),
        sa.Column("tg_media_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_access_hash", sa.BigInteger(), nullable=True),
        sa.Column("tg_dc_id", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(length=127), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("expected_size", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("status_note", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("local_path", sa.String(length=1024), nullable=True),
        sa.Column("stored_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_pk"], ["messages.id"], name=op.f("fk_media_message_pk_messages"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media")),
    )
    op.create_index("ix_media_message_pk", "media", ["message_pk"])
    op.create_index(
        "uq_media_current",
        "media",
        ["message_pk"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index("ix_media_queue", "media", ["status", "next_attempt_at"])
    op.create_index("ix_media_tg_media_id", "media", ["tg_media_id"])

    op.create_table(
        "chat_sync_state",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("backfill_status", ENUM, nullable=False),
        sa.Column("backfill_boundary_id", sa.BigInteger(), nullable=True),
        sa.Column("backfill_cursor_id", sa.BigInteger(), nullable=True),
        sa.Column("backfill_imported", sa.Integer(), nullable=False),
        sa.Column("high_water_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], name=op.f("fk_chat_sync_state_chat_id_chats"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_chat_sync_state")),
    )
    op.create_index("ix_chat_sync_state_status_retry", "chat_sync_state", ["backfill_status", "next_retry_at"])

    op.create_table(
        "deletion_events",
        sa.Column("id", PK, nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("resolution", ENUM, nullable=False),
        sa.Column("source", ENUM, nullable=False),
        sa.Column("message_pk", sa.BigInteger(), nullable=True),
        sa.Column("resolved_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["message_pk"],
            ["messages.id"],
            name=op.f("fk_deletion_events_message_pk_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deletion_events")),
    )
    op.create_index("ix_deletion_events_detected_at", "deletion_events", ["detected_at"])
    op.create_index("ix_deletion_events_message_pk", "deletion_events", ["message_pk"])
    op.create_index("ix_deletion_events_chat_detected", "deletion_events", ["resolved_chat_id", "detected_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", PK, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_action_created", "audit_log", ["action", "created_at"])

    op.create_table(
        "component_heartbeats",
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_beat_at", sa.DateTime(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("component", name=op.f("pk_component_heartbeats")),
    )

    if op.get_context().dialect.name == "sqlite":
        op.execute(_FTS_TABLE)
        for ddl in _FTS_TRIGGERS:
            op.execute(ddl)


def downgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        for trigger in ("messages_fts_au", "messages_fts_ad", "messages_fts_ai"):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.execute("DROP TABLE IF EXISTS messages_fts")

    op.drop_table("component_heartbeats")
    op.drop_table("audit_log")
    op.drop_table("deletion_events")
    op.drop_table("chat_sync_state")
    op.drop_table("media")
    op.drop_table("message_versions")
    op.drop_table("messages")
    op.drop_table("chats")
