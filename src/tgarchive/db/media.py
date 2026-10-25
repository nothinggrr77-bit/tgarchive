"""Media arxivi bo'yicha so'rovlar: statistika, katta fayllar, majburiy yuklash.

Telethon'ga bog'liq emas — CLI (``tgarchive media``) va admin bot (9-bosqich) ishlatadi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive.db.enums import MediaSkipReason, MediaStatus, MediaType
from tgarchive.db.models import Chat, Media, Message
from tgarchive.db.types import utcnow

NEVER_DOWNLOADED = frozenset({MediaSkipReason.PROTECTED, MediaSkipReason.VIEW_ONCE})
_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@dataclass(frozen=True, slots=True)
class MediaSummary:
    by_status: dict[str, int] = field(default_factory=dict)
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    stored_bytes: int = 0  # diskdagi noyob fayllar hajmi
    large_count: int = 0
    large_bytes: int = 0


async def media_summary(session: AsyncSession) -> MediaSummary:
    current = Media.superseded_at.is_(None)
    by_status = {
        status.value: count
        for status, count in await session.execute(
            select(Media.status, func.count()).where(current).group_by(Media.status)
        )
    }
    by_reason = {
        reason.value: count
        for reason, count in await session.execute(
            select(Media.skip_reason, func.count())
            .where(current, Media.status == MediaStatus.SKIPPED, Media.skip_reason.is_not(None))
            .group_by(Media.skip_reason)
        )
    }
    per_file = (
        select(func.max(Media.stored_size).label("size"))
        .where(Media.status == MediaStatus.DONE, Media.local_path.is_not(None))
        .group_by(Media.local_path)
        .subquery()
    )
    stored = await session.scalar(select(func.coalesce(func.sum(per_file.c.size), 0)))
    large_count, large_bytes = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Media.expected_size), 0)).where(
                current, Media.status == MediaStatus.SKIPPED, Media.skip_reason == MediaSkipReason.TOO_LARGE
            )
        )
    ).one()
    return MediaSummary(by_status, by_reason, int(stored or 0), int(large_count), int(large_bytes))


def format_media_summary(summary: MediaSummary, limit_mb: int | None = None) -> str:
    s = summary.by_status
    limit = f" (> {limit_mb} MB)" if limit_mb else ""
    line = (
        f"Media: yuklangan {s.get('done', 0)} ({format_size(summary.stored_bytes)}), "
        f"navbatda {s.get('pending', 0) + s.get('missing', 0) + s.get('downloading', 0)}, "
        f"katta fayllar{limit} {summary.large_count} ({format_size(summary.large_bytes)}), "
        f"xato {s.get('failed', 0)}, Telegram'da yo'q {s.get('unavailable', 0)}"
    )
    other = {k: v for k, v in summary.skipped_by_reason.items() if k != MediaSkipReason.TOO_LARGE}
    if other:
        line += "\n  Yuklanmaydi: " + ", ".join(f"{k}: {v}" for k, v in sorted(other.items()))
    return line


@dataclass(frozen=True, slots=True)
class LargeFile:
    media_id: int
    chat_id: int
    chat_title: str | None
    message_id: int
    date: datetime
    media_type: MediaType
    file_name: str | None
    mime_type: str | None
    size: int | None


async def list_large_files(session: AsyncSession, *, limit: int = 20, offset: int = 0) -> list[LargeFile]:
    """Hajm chegarasidan katta bo'lgani uchun yuklanmagan fayllar, eng yangisi birinchi."""
    rows = await session.execute(
        select(Media, Message.chat_id, Message.message_id, Message.date, Chat.title)
        .join(Message, Message.id == Media.message_pk)
        .join(Chat, Chat.chat_id == Message.chat_id)
        .where(
            Media.superseded_at.is_(None),
            Media.status == MediaStatus.SKIPPED,
            Media.skip_reason == MediaSkipReason.TOO_LARGE,
        )
        .order_by(Message.date.desc(), Media.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        LargeFile(
            media_id=media.id,
            chat_id=chat_id,
            chat_title=title,
            message_id=message_id,
            date=date,
            media_type=media.media_type,
            file_name=media.file_name,
            mime_type=media.mime_type,
            size=media.expected_size,
        )
        for media, chat_id, message_id, date, title in rows
    ]


def format_large_files(files: list[LargeFile]) -> str:
    if not files:
        return "Katta fayllar yo'q."
    lines = [f"{'ID':>7}  {'SANA (UTC)':<16}  {'HAJM':>9}  {'TUR':<10}  NOM — CHAT"]
    for f in files:
        name = f.file_name or f.mime_type or "-"
        lines.append(
            f"{f.media_id:>7}  {f.date:%Y-%m-%d %H:%M}  {format_size(f.size):>9}  {f.media_type.value:<10}  "
            f"{name} — {f.chat_title or f.chat_id} (xabar {f.message_id})"
        )
    return "\n".join(lines)


class FetchRefused(Exception):
    pass


async def request_fetch(session: AsyncSession, media_id: int) -> Media:
    """Egasi so'ragan faylni hajm/tur chegarasiga qaramay navbatga qo'yadi.

    Himoyalangan (noforwards) va bir marta ko'riladigan media bundan mustasno.
    """
    media = await session.get(Media, media_id)
    if media is None:
        raise FetchRefused(f"media {media_id} topilmadi")
    if media.skip_reason in NEVER_DOWNLOADED:
        raise FetchRefused(f"media {media_id}: {media.skip_reason.value} — yuklab bo'lmaydi")
    if media.status is MediaStatus.DONE:
        raise FetchRefused(f"media {media_id} allaqachon yuklangan: {media.local_path}")
    media.force_download = True
    media.status = MediaStatus.PENDING
    media.skip_reason = None
    media.status_note = "egasi so'ragan"
    media.attempts = 0
    media.next_attempt_at = None
    media.updated_at = utcnow()
    await session.flush()
    return media
