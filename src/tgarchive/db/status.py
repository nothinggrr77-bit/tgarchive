"""Komponentlar holati (heartbeat) va import statistikasi.

``tgarchive status``, Docker healthcheck va (9-bosqichda) bot ``/status`` / ``/stats``
shu moduldan o'qiydi — archiver bilan to'g'ridan-to'g'ri gaplashmaydi.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive import __version__
from tgarchive.db.models import Chat, ChatSyncState, ComponentHeartbeat, Message
from tgarchive.db.types import utcnow

ARCHIVER = "archiver"


async def beat(
    session: AsyncSession,
    component: str,
    state: str,
    *,
    started_at: datetime,
    details: dict[str, Any] | None = None,
) -> None:
    row = await session.get(ComponentHeartbeat, component)
    if row is None:
        row = ComponentHeartbeat(component=component, started_at=started_at, last_beat_at=utcnow(), state=state)
        session.add(row)
    row.state = state
    row.pid = os.getpid()
    row.hostname = socket.gethostname()
    row.version = __version__
    row.started_at = started_at
    row.last_beat_at = utcnow()
    row.details = details
    await session.flush()


async def get_heartbeat(session: AsyncSession, component: str) -> ComponentHeartbeat | None:
    return await session.get(ComponentHeartbeat, component)


@dataclass(frozen=True, slots=True)
class FailingChat:
    chat_id: int
    title: str | None
    last_error: str | None
    next_retry_at: datetime | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ImportSummary:
    chats_by_mode: dict[str, int] = field(default_factory=dict)
    backfill_by_status: dict[str, int] = field(default_factory=dict)
    imported_messages: int = 0
    total_messages: int = 0
    deleted_messages: int = 0
    failing: list[FailingChat] = field(default_factory=list)


async def import_summary(session: AsyncSession, *, failing_limit: int = 10) -> ImportSummary:
    by_mode = {
        (mode.value if mode else "-"): count
        for mode, count in await session.execute(select(Chat.archive_mode, func.count()).group_by(Chat.archive_mode))
    }
    by_status = {
        status.value: count
        for status, count in await session.execute(
            select(ChatSyncState.backfill_status, func.count()).group_by(ChatSyncState.backfill_status)
        )
    }
    imported = await session.scalar(select(func.coalesce(func.sum(ChatSyncState.backfill_imported), 0)))
    total = await session.scalar(select(func.count()).select_from(Message))
    deleted = await session.scalar(
        select(func.count()).select_from(Message).where(Message.deletion_status.is_not(None))
    )
    failing_rows = await session.execute(
        select(Chat.chat_id, Chat.title, ChatSyncState.last_error, ChatSyncState.next_retry_at, ChatSyncState.attempts)
        .join(ChatSyncState, ChatSyncState.chat_id == Chat.chat_id)
        .where(ChatSyncState.last_error.is_not(None))
        .order_by(ChatSyncState.last_error_at.desc())
        .limit(failing_limit)
    )
    return ImportSummary(
        chats_by_mode=by_mode,
        backfill_by_status=by_status,
        imported_messages=int(imported or 0),
        total_messages=int(total or 0),
        deleted_messages=int(deleted or 0),
        failing=[FailingChat(*row) for row in failing_rows],
    )


def format_summary(summary: ImportSummary, heartbeat: ComponentHeartbeat | None, now: datetime) -> str:
    lines = []
    if heartbeat is None:
        lines.append("Archiver: hali ishga tushmagan")
    else:
        age = int((now - heartbeat.last_beat_at).total_seconds())
        job = (heartbeat.details or {}).get("job") or {}
        current = f", hozir: {job.get('job')} {job.get('title') or job.get('chat_id')}" if job else ""
        lines.append(f"Archiver: {heartbeat.state} (oxirgi signal {age} s oldin, v{heartbeat.version}){current}")
        error = (heartbeat.details or {}).get("error")
        if error:
            lines.append(f"  Sabab: {error}")
    modes = ", ".join(f"{k}: {v}" for k, v in sorted(summary.chats_by_mode.items())) or "-"
    statuses = ", ".join(f"{k}: {v}" for k, v in sorted(summary.backfill_by_status.items())) or "-"
    lines.append(f"Chatlar (rejim): {modes}")
    lines.append(f"Tarix importi (holat): {statuses}")
    lines.append(
        f"Xabarlar: jami {summary.total_messages}, tarixdan import qilingan {summary.imported_messages}, "
        f"o'chirilgan deb belgilangan {summary.deleted_messages}"
    )
    if summary.failing:
        lines.append("Muammoli chatlar:")
        for item in summary.failing:
            retry = item.next_retry_at.strftime("%Y-%m-%d %H:%M UTC") if item.next_retry_at else "-"
            lines.append(f"  {item.chat_id} {item.title or '-'}: {item.last_error} (qayta: {retry}, urinish: {item.attempts})")
    return "\n".join(lines)
