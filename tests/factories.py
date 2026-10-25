"""Testlar uchun tayyor chatlar va xabar yasovchi yordamchilar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from tgarchive.db.engine import Database
from tgarchive.db.enums import ChatType
from tgarchive.db.store import ChatInfo, MessageRecord, upsert_chat

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

PRIVATE = ChatInfo(chat_id=1001, chat_type=ChatType.PRIVATE, title="Ali")
PRIVATE_2 = ChatInfo(chat_id=1002, chat_type=ChatType.PRIVATE, title="Vali")
GROUP = ChatInfo(chat_id=-555, chat_type=ChatType.GROUP, title="Oila")
CHANNEL = ChatInfo(chat_id=-1001234567890, chat_type=ChatType.CHANNEL, title="Yangiliklar")


def sqlite_url(path: Path) -> URL:
    return URL.create("sqlite+aiosqlite", database=str(path))


def at(minutes: int = 0) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def record(chat_id: int = PRIVATE.chat_id, message_id: int = 1, **fields: Any) -> MessageRecord:
    fields.setdefault("date", at(message_id))
    fields.setdefault("text", f"xabar {message_id}")
    return MessageRecord(chat_id=chat_id, message_id=message_id, **fields)


async def add_chats(db: Database, *chats: ChatInfo) -> None:
    async with db.write() as session:
        for chat in chats:
            await upsert_chat(session, chat)
