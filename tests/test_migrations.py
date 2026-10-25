from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from factories import sqlite_url
from tgarchive.db import models  # noqa: F401
from tgarchive.db.base import Base
from tgarchive.db.migrate import downgrade, head_revision, upgrade

EXPECTED_TABLES = {
    "chats",
    "messages",
    "message_versions",
    "media",
    "chat_sync_state",
    "deletion_events",
    "audit_log",
    "component_heartbeats",
    "app_meta",
    "media_trash",
    "messages_fts",
}


def _objects(path: Path, kind: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_upgrade_creates_schema(migrated_template: Path) -> None:
    assert EXPECTED_TABLES <= _objects(migrated_template, "table")
    assert {"messages_fts_ai", "messages_fts_ad", "messages_fts_au"} <= _objects(migrated_template, "trigger")
    with sqlite3.connect(migrated_template) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision() == "0004"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        media_columns = {row[1] for row in conn.execute("PRAGMA table_info(media)")}
    assert {"archive_mode", "linked_chat_id"} <= columns
    assert "comments_synced_id" in message_columns
    assert {"skip_reason", "force_download"} <= media_columns


def test_0004_requeues_previously_skipped_media(tmp_path: Path) -> None:
    """0004 dan oldin sababsiz "skipped" bo'lgan media qayta ko'rib chiqish uchun navbatga qaytadi."""
    path = tmp_path / "old.sqlite3"
    url = sqlite_url(path)
    upgrade(url, "0003")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO chats (chat_id, chat_type, is_protected, first_seen_at, updated_at) "
            "VALUES (7, 'private', 0, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO messages (chat_id, message_id, is_outgoing, date, noforwards, content_hash, edit_count, "
            "source, archived_at, updated_at) VALUES (7, 1, 0, '2026-01-01', 0, 'h', 0, 'history', "
            "'2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO media (message_pk, media_type, status, status_note, attempts, created_at, updated_at) "
            "VALUES (1, 'video', 'skipped', 'eski sabab', 0, '2026-01-01', '2026-01-01')"
        )
    upgrade(url)
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT status, status_note, skip_reason, force_download FROM media").fetchone()
    assert row == ("pending", None, None, 0)


def test_models_match_migration(migrated_template: Path) -> None:
    """Modellar o'zgarib, migratsiya yozilmay qolsa — shu test yiqiladi."""

    def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
        return not (type_ == "table" and name is not None and name.startswith("messages_fts"))

    engine = create_engine(URL.create("sqlite", database=str(migrated_template)))
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "include_object": include_object}
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_downgrade_removes_everything(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    upgrade(sqlite_url(path))
    downgrade(sqlite_url(path), "base")
    assert _objects(path, "table") <= {"alembic_version"}
    assert _objects(path, "trigger") == set()
