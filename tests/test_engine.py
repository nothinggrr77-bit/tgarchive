from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from factories import PRIVATE, add_chats, record
from tgarchive.db.engine import Database
from tgarchive.db.enums import MessageSource
from tgarchive.db.fts import match_message_pks
from tgarchive.db.models import Chat, Message
from tgarchive.db.store import save_message, upsert_chat


def _other_connection(path: Path) -> sqlite3.Connection:
    # timeout=0: qulf band bo'lsa kutmasdan darhol xato
    return sqlite3.connect(path, timeout=0, isolation_level=None)


async def test_sqlite_pragmas(db: Database) -> None:
    async with db.read() as session:
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        assert (await session.scalar(text("PRAGMA journal_mode"))).lower() == "wal"
        assert await session.scalar(text("PRAGMA busy_timeout")) == 2000


async def test_write_takes_lock_at_begin(db: Database, db_path: Path) -> None:
    async with db.write() as session:
        await session.execute(text("SELECT 1"))  # hali hech narsa yozilmagan
        other = _other_connection(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()

    other = _other_connection(db_path)
    try:
        other.execute("BEGIN IMMEDIATE")  # commit'dan keyin qulf bo'shagan
        other.execute("ROLLBACK")
    finally:
        other.close()


async def test_read_does_not_block_writers(db: Database, db_path: Path) -> None:
    async with db.read() as session:
        await session.execute(select(func.count()).select_from(Chat))
        other = _other_connection(db_path)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()


async def test_write_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.write() as session:
            await upsert_chat(session, PRIVATE)
            raise RuntimeError("bekor qilish")
    async with db.read() as session:
        assert await session.get(Chat, PRIVATE.chat_id) is None


async def test_concurrent_writers_do_not_duplicate(db: Database) -> None:
    await add_chats(db, PRIVATE)

    async def writer(ids: range, source: MessageSource) -> None:
        for message_id in ids:
            async with db.write() as session:
                await save_message(session, record(message_id=message_id), source)

    # Tarix importi va real vaqt bir xil xabarlarni bir vaqtda yozmoqda
    await asyncio.gather(
        writer(range(1, 21), MessageSource.HISTORY),
        writer(range(1, 21), MessageSource.REALTIME),
        writer(range(100, 120), MessageSource.REALTIME),
    )
    async with db.read() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 40


async def test_deleting_chat_cascades_and_cleans_search_index(db: Database) -> None:
    await add_chats(db, PRIVATE)
    async with db.write() as session:
        await save_message(session, record(text="noyob qidiruv sozi"), MessageSource.HISTORY)
    async with db.write() as session:
        await session.delete(await session.get(Chat, PRIVATE.chat_id))
    async with db.read() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await match_message_pks(session, "noyob") == []
