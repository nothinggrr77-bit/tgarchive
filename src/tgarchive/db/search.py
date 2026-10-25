"""Arxivdan qidirish va ro'yxatlar: matn (FTS5), filtrlar, chatlar, statistika.

Admin bot va (10-bosqichda) eksport ishlatadi. Foydalanuvchi matni hech qachon SQL'ga
qo'shilmaydi: FTS so'rovi ``fts.build_match_query`` bilan quriladi, qolgan hamma narsa
bound parametr.

Qidiruv sintaksisi (``parse_search``)::

    salom dunyo chat:-1001234 from:7 from:me after:2026-01-01 before:2026-02-01 on:2026-01-15
    type:photo has:media has:reply is:edited is:deleted name:hisobot
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, column, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive.db.enums import ArchiveMode, ChatType, DeletionStatus, MediaStatus, MediaType, SyncStatus
from tgarchive.db.fts import FTS_TABLE, build_match_query
from tgarchive.db.models import Chat, ChatSyncState, Media, Message, MessageVersion
from tgarchive.db.store import get_reply_parent

ARCHIVED = (ArchiveMode.FULL, ArchiveMode.WINDOW)
NAME_LIMIT = 100


# ---------------------------------------------------------------- so'rov


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str = ""
    chat_id: int | None = None
    sender_id: int | None = None
    outgoing: bool | None = None
    date_from: datetime | None = None  # UTC, shu vaqt ham kiradi
    date_to: datetime | None = None  # UTC, shu vaqt kirmaydi
    media_type: MediaType | None = None
    has_media: bool = False
    has_reply: bool = False
    edited: bool = False
    deleted: bool = False
    file_name: str | None = None

    @property
    def is_empty(self) -> bool:
        return self == SearchQuery()

    def describe(self) -> str:
        parts = []
        if self.text:
            parts.append(f"«{self.text}»")
        if self.chat_id is not None:
            parts.append(f"chat:{self.chat_id}")
        if self.sender_id is not None:
            parts.append(f"from:{self.sender_id}")
        if self.outgoing:
            parts.append("from:me")
        if self.date_from:
            parts.append(f"after:{self.date_from:%Y-%m-%d %H:%M} UTC")
        if self.date_to:
            parts.append(f"before:{self.date_to:%Y-%m-%d %H:%M} UTC")
        if self.media_type:
            parts.append(f"type:{self.media_type.value}")
        if self.has_media:
            parts.append("has:media")
        if self.has_reply:
            parts.append("has:reply")
        if self.edited:
            parts.append("is:edited")
        if self.deleted:
            parts.append("is:deleted")
        if self.file_name:
            parts.append(f"name:{self.file_name}")
        return " ".join(parts) or "barcha xabarlar"


_KEYS = {"chat", "from", "after", "since", "before", "until", "on", "type", "has", "is", "name"}


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(ZoneInfo("UTC"))


def parse_search(raw: str, tz: ZoneInfo) -> tuple[SearchQuery, list[str]]:
    """Foydalanuvchi matnidan so'rov va xatolar ro'yxati. Sanalar ``tz`` bo'yicha kun boshi."""
    words: list[str] = []
    errors: list[str] = []
    values: dict[str, object] = {}
    for token in raw.split():
        key, sep, value = token.partition(":")
        key = key.lower()
        if not sep or key not in _KEYS or not value:
            words.append(token)
            continue
        try:
            if key == "chat":
                values["chat_id"] = int(value)
            elif key == "from":
                if value.lower() == "me":
                    values["outgoing"] = True
                else:
                    values["sender_id"] = int(value)
            elif key in ("after", "since"):
                values["date_from"] = _local_midnight(_parse_day(value), tz)
            elif key in ("before", "until"):
                values["date_to"] = _local_midnight(_parse_day(value), tz)
            elif key == "on":
                day = _parse_day(value)
                values["date_from"] = _local_midnight(day, tz)
                values["date_to"] = _local_midnight(day + timedelta(days=1), tz)
            elif key == "type":
                values["media_type"] = MediaType(value.lower())
            elif key == "has":
                if value.lower() == "media":
                    values["has_media"] = True
                elif value.lower() == "reply":
                    values["has_reply"] = True
                else:
                    errors.append(f"has:{value} — faqat has:media yoki has:reply")
            elif key == "is":
                if value.lower() == "edited":
                    values["edited"] = True
                elif value.lower() == "deleted":
                    values["deleted"] = True
                else:
                    errors.append(f"is:{value} — faqat is:edited yoki is:deleted")
            elif key == "name":
                values["file_name"] = value[:NAME_LIMIT]
        except ValueError:
            if key in ("chat", "from"):
                errors.append(f"{key}:{value} — son bo'lishi kerak")
            elif key == "type":
                kinds = ", ".join(t.value for t in MediaType)
                errors.append(f"type:{value} — noma'lum tur ({kinds})")
            else:
                errors.append(f"{key}:{value} — sana YYYY-MM-DD ko'rinishida bo'lsin")
    query = SearchQuery(text=" ".join(words), **values)  # type: ignore[arg-type]
    if query.date_from and query.date_to and query.date_from >= query.date_to:
        errors.append("sana oralig'i bo'sh: after/since before/until dan oldin bo'lishi kerak")
    return query, errors


# ------------------------------------------------------------- natijalar


@dataclass(frozen=True, slots=True)
class MessageRow:
    pk: int
    chat_id: int
    chat_title: str | None
    chat_type: ChatType
    message_id: int
    date: datetime
    sender_id: int | None
    sender_name: str | None
    is_outgoing: bool
    text: str | None
    media_type: MediaType | None
    action: str | None
    edit_count: int
    deletion_status: DeletionStatus | None
    deleted_at: datetime | None
    reply_to_msg_id: int | None


@dataclass(frozen=True, slots=True)
class Page:
    rows: list
    offset: int
    has_next: bool


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_messages(
    session: AsyncSession,
    query: SearchQuery,
    *,
    limit: int = 10,
    offset: int = 0,
    order: Literal["date", "deleted"] = "date",
) -> Page:
    stmt = select(Message, Chat.title, Chat.chat_type).join(Chat, Chat.chat_id == Message.chat_id)
    if query.text:
        match = build_match_query(query.text)
        if match is None:
            return Page([], offset, False)
        fts = (
            text(f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :fts_match")
            .bindparams(fts_match=match)
            .columns(column("rowid", Integer))
        )
        stmt = stmt.where(Message.id.in_(fts))
    if query.chat_id is not None:
        stmt = stmt.where(Message.chat_id == query.chat_id)
    if query.sender_id is not None:
        stmt = stmt.where(Message.sender_id == query.sender_id)
    if query.outgoing is not None:
        stmt = stmt.where(Message.is_outgoing == query.outgoing)
    if query.date_from is not None:
        stmt = stmt.where(Message.date >= query.date_from)
    if query.date_to is not None:
        stmt = stmt.where(Message.date < query.date_to)
    if query.media_type is not None:
        stmt = stmt.where(Message.media_type == query.media_type)
    if query.has_media:
        stmt = stmt.where(Message.media_type.is_not(None))
    if query.has_reply:
        stmt = stmt.where(Message.reply_to_msg_id.is_not(None))
    if query.edited:
        stmt = stmt.where(Message.edit_count > 0)
    if query.deleted or order == "deleted":
        stmt = stmt.where(Message.deletion_status.is_not(None))
    if query.file_name:
        stmt = stmt.where(
            exists().where(
                Media.message_pk == Message.id,
                Media.file_name.like(_like_pattern(query.file_name), escape="\\"),
            )
        )
    if order == "deleted":
        stmt = stmt.order_by(Message.deleted_at.desc(), Message.id.desc())
    else:
        stmt = stmt.order_by(Message.date.desc(), Message.id.desc())
    result = await session.execute(stmt.limit(limit + 1).offset(offset))
    rows = [
        MessageRow(
            pk=m.id,
            chat_id=m.chat_id,
            chat_title=title,
            chat_type=chat_type,
            message_id=m.message_id,
            date=m.date,
            sender_id=m.sender_id,
            sender_name=m.sender_name,
            is_outgoing=m.is_outgoing,
            text=m.text,
            media_type=m.media_type,
            action=m.action,
            edit_count=m.edit_count,
            deletion_status=m.deletion_status,
            deleted_at=m.deleted_at,
            reply_to_msg_id=m.reply_to_msg_id,
        )
        for m, title, chat_type in result
    ]
    return Page(rows[:limit], offset, len(rows) > limit)


def with_chat(query: SearchQuery, chat_id: int) -> SearchQuery:
    return replace(query, chat_id=chat_id)


# ----------------------------------------------------------------- chatlar


@dataclass(frozen=True, slots=True)
class ChatRow:
    chat_id: int
    title: str | None
    chat_type: ChatType
    archive_mode: ArchiveMode | None
    message_count: int
    last_date: datetime | None


def _chat_counts():
    return (
        select(
            Message.chat_id.label("chat_id"),
            func.count().label("n"),
            func.max(Message.date).label("last"),
        )
        .group_by(Message.chat_id)
        .subquery()
    )


async def list_chats(session: AsyncSession, *, limit: int = 10, offset: int = 0) -> Page:
    """Arxivlanayotgan chatlar, oxirgi xabar bo'yicha (eng faoli birinchi)."""
    counts = _chat_counts()
    result = await session.execute(
        select(Chat, counts.c.n, counts.c.last)
        .outerjoin(counts, counts.c.chat_id == Chat.chat_id)
        .where(Chat.archive_mode.in_(ARCHIVED))
        .order_by(counts.c.last.desc().nulls_last(), Chat.chat_id)
        .limit(limit + 1)
        .offset(offset)
    )
    rows = [
        ChatRow(chat.chat_id, chat.title, chat.chat_type, chat.archive_mode, int(n or 0), last)
        for chat, n, last in result
    ]
    return Page(rows[:limit], offset, len(rows) > limit)


@dataclass(frozen=True, slots=True)
class ChatDetail:
    chat: Chat
    state: ChatSyncState | None
    message_count: int
    deleted_count: int
    edited_count: int
    media_done: int
    first_date: datetime | None
    last_date: datetime | None


async def chat_detail(session: AsyncSession, chat_id: int) -> ChatDetail | None:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        return None
    state = await session.get(ChatSyncState, chat_id)
    total, first, last = (
        await session.execute(
            select(func.count(), func.min(Message.date), func.max(Message.date)).where(Message.chat_id == chat_id)
        )
    ).one()
    deleted = await session.scalar(
        select(func.count()).where(Message.chat_id == chat_id, Message.deletion_status.is_not(None))
    )
    edited = await session.scalar(select(func.count()).where(Message.chat_id == chat_id, Message.edit_count > 0))
    media_done = await session.scalar(
        select(func.count())
        .select_from(Media)
        .join(Message, Message.id == Media.message_pk)
        .where(Message.chat_id == chat_id, Media.status == MediaStatus.DONE, Media.superseded_at.is_(None))
    )
    return ChatDetail(chat, state, int(total), int(deleted or 0), int(edited or 0), int(media_done or 0), first, last)


# --------------------------------------------------------------- xabar


@dataclass(frozen=True, slots=True)
class MessageDetail:
    message: Message
    chat: Chat
    parent: Message | None
    media: Media | None
    version_count: int
    reply_count: int


async def message_detail(session: AsyncSession, pk: int) -> MessageDetail | None:
    message = await session.get(Message, pk)
    if message is None:
        return None
    chat = await session.get(Chat, message.chat_id)
    parent = await get_reply_parent(session, message)
    media = await session.scalar(select(Media).where(Media.message_pk == pk, Media.superseded_at.is_(None)))
    versions = await session.scalar(select(func.count()).where(MessageVersion.message_pk == pk))
    replies = await session.scalar(
        select(func.count()).where(
            Message.chat_id == message.chat_id,
            Message.reply_to_msg_id == message.message_id,
            Message.reply_to_peer_id.is_(None),
        )
    )
    return MessageDetail(message, chat, parent, media, int(versions or 0), int(replies or 0))


# ------------------------------------------------------------ statistika


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    total_messages: int = 0
    edited_messages: int = 0
    versions: int = 0
    deleted_confirmed: int = 0
    deleted_probable: int = 0
    first_date: datetime | None = None
    last_date: datetime | None = None
    chats_by_mode: dict[str, int] = field(default_factory=dict)
    backfill_by_status: dict[str, int] = field(default_factory=dict)
    top_chats: Sequence[ChatRow] = ()


async def archive_stats(session: AsyncSession, *, top: int = 5) -> ArchiveStats:
    total, first, last = (
        await session.execute(select(func.count(), func.min(Message.date), func.max(Message.date)).select_from(Message))
    ).one()
    edited = await session.scalar(select(func.count()).where(Message.edit_count > 0))
    versions = await session.scalar(select(func.count()).select_from(MessageVersion))
    deleted = dict(
        (
            await session.execute(
                select(Message.deletion_status, func.count())
                .where(Message.deletion_status.is_not(None))
                .group_by(Message.deletion_status)
            )
        ).all()
    )
    modes = {
        (mode.value if mode else "-"): n
        for mode, n in await session.execute(select(Chat.archive_mode, func.count()).group_by(Chat.archive_mode))
    }
    statuses = {
        s.value: n
        for s, n in await session.execute(
            select(ChatSyncState.backfill_status, func.count()).group_by(ChatSyncState.backfill_status)
        )
    }
    counts = _chat_counts()
    top_rows = await session.execute(
        select(Chat, counts.c.n, counts.c.last)
        .join(counts, counts.c.chat_id == Chat.chat_id)
        .where(Chat.archive_mode.in_(ARCHIVED))
        .order_by(counts.c.n.desc())
        .limit(top)
    )
    return ArchiveStats(
        total_messages=int(total),
        edited_messages=int(edited or 0),
        versions=int(versions or 0),
        deleted_confirmed=int(deleted.get(DeletionStatus.CONFIRMED, 0)),
        deleted_probable=int(deleted.get(DeletionStatus.PROBABLE, 0)),
        first_date=first,
        last_date=last,
        chats_by_mode=modes,
        backfill_by_status={k: v for k, v in statuses.items() if k != SyncStatus.DISABLED},
        top_chats=[ChatRow(c.chat_id, c.title, c.chat_type, c.archive_mode, int(n), d) for c, n, d in top_rows],
    )
