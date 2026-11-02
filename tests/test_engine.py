from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

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


def _locked() -> OperationalError:
    return OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))


async def _no_sleep(_delay: float) -> None:
    return


class _FlakySession:
    """connection() bir necha marta "locked" tashlab, keyin muvaffaqiyatli bo'ladi."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.rollbacks = 0

    async def connection(self, **_kwargs: object) -> object:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise _locked()
        return object()

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_begin_immediate_retries_until_lock_free(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tgarchive.db.engine.asyncio.sleep", _no_sleep)
    session = _FlakySession(fail_times=2)
    await db._begin_immediate(session)  # type: ignore[arg-type]
    assert session.attempts == 3  # 2 marta band, 3-urinishda muvaffaqiyat
    assert session.rollbacks == 2  # har muvaffaqiyatsiz urinishdan keyin tozalangan


async def test_begin_immediate_gives_up_after_retries(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tgarchive.db.engine.asyncio.sleep", _no_sleep)
    session = _FlakySession(fail_times=99)
    with pytest.raises(OperationalError, match="locked"):
        await db._begin_immediate(session)  # type: ignore[arg-type]
    from tgarchive.db.engine import _WRITE_LOCK_RETRIES

    assert session.attempts == _WRITE_LOCK_RETRIES  # chegaralangan urinishlar


async def test_begin_immediate_does_not_retry_other_errors(db: Database) -> None:
    class _Broken:
        async def connection(self, **_kwargs: object) -> object:
            raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("no such table"))

        async def rollback(self) -> None:  # pragma: no cover - chaqirilmasligi kerak
            raise AssertionError("boshqa xatoda rollback bo'lmasligi kerak")

    with pytest.raises(OperationalError, match="no such table"):
        await db._begin_immediate(_Broken())  # type: ignore[arg-type]


async def test_deleting_chat_cascades_and_cleans_search_index(db: Database) -> None:
    await add_chats(db, PRIVATE)
    async with db.write() as session:
        await save_message(session, record(text="noyob qidiruv sozi"), MessageSource.HISTORY)
    async with db.write() as session:
        await session.delete(await session.get(Chat, PRIVATE.chat_id))
    async with db.read() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await match_message_pks(session, "noyob") == []
