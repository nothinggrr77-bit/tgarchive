"""Arxiv yozuvlari: chatlar, xabarlar, tahrir versiyalari, reply zanjiri, o'chirilganlik.

Barcha funksiyalar chaqiruvchi bergan ``AsyncSession`` ichida ishlaydi — tranzaksiya
chegarasini chaqiruvchi belgilaydi (masalan, tarix sahifasi va uning checkpoint'i
bitta tranzaksiyada saqlanadi). Yozish uchun ``Database.write()`` ishlating.

Bu modul Telethon'ga bog'liq emas: kirish ma'lumoti oddiy ``MessageRecord``.
Telethon obyektlarini ``MessageRecord`` ga aylantirish archiver qatlamida bo'ladi.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tgarchive.db.enums import (
    COMMON_BOX_TYPES,
    ChatType,
    DeletionResolution,
    DeletionSource,
    DeletionStatus,
    MediaStatus,
    MediaType,
    MessageSource,
)
from tgarchive.db.models import AuditLog, Chat, DeletionEvent, Media, MediaTrash, Message, MessageVersion
from tgarchive.db.types import utcnow

_IN_CHUNK = 500


@dataclass(frozen=True, slots=True)
class ChatInfo:
    chat_id: int
    chat_type: ChatType
    title: str | None = None
    username: str | None = None
    is_protected: bool = False
    migrated_to_chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    media_type: MediaType
    tg_media_id: int | None = None
    tg_access_hash: int | None = None
    tg_dc_id: int | None = None
    mime_type: str | None = None
    file_name: str | None = None
    size: int | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    ttl_seconds: int | None = None

    @property
    def fingerprint(self) -> str:
        return _media_fingerprint(self.media_type, self.tg_media_id)


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Telegram xabarining kutubxonadan mustaqil ko'rinishi."""

    chat_id: int
    message_id: int
    date: datetime
    sender_id: int | None = None
    sender_name: str | None = None
    is_outgoing: bool = False
    text: str | None = None
    entities: list[dict[str, Any]] | None = None
    edit_date: datetime | None = None
    reply_to_msg_id: int | None = None
    reply_to_peer_id: int | None = None
    reply_to_top_id: int | None = None
    fwd_from_id: int | None = None
    fwd_from_name: str | None = None
    fwd_date: datetime | None = None
    fwd_channel_post_id: int | None = None
    grouped_id: int | None = None
    action: str | None = None
    noforwards: bool = False
    media: MediaInfo | None = None

    def __post_init__(self) -> None:
        if self.message_id <= 0:
            raise ValueError("message_id musbat bo'lishi kerak")
        for name in ("date", "edit_date", "fwd_date"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name}: naive datetime qabul qilinmaydi")

    @property
    def media_type(self) -> MediaType | None:
        return self.media.media_type if self.media else None

    def content_hash(self) -> str:
        """Tahrirni aniqlash uchun xabar mazmunining barqaror xeshi.

        Faqat matn, formatlash, media (fayl bo'lsa ID'si bilan) va servis harakati
        hisobga olinadi. Ko'rishlar soni, reaksiyalar kabi o'zgarishlar tahrir emas.
        """
        if self.media is None:
            media = None
        elif self.media.media_type.is_file:
            media = self.media.fingerprint
        else:
            media = self.media.media_type.value
        payload = {"t": self.text or "", "e": self.entities or [], "m": media, "a": self.action}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SaveOutcome(StrEnum):
    CREATED = "created"  # yangi xabar
    UNCHANGED = "unchanged"  # dublikat: mazmun o'zgarmagan
    EDITED = "edited"  # yangi versiya, eskisi message_versions ga ko'chirildi
    STALE = "stale"  # bazadagi holatdan eskiroq nusxa — e'tiborsiz qoldirildi


def _media_fingerprint(media_type: MediaType, tg_media_id: int | None) -> str:
    return f"{media_type.value}:{tg_media_id if tg_media_id is not None else ''}"


def _chunks(items: Sequence[int], size: int = _IN_CHUNK) -> Iterator[Sequence[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --------------------------------------------------------------------------- chats


async def upsert_chat(session: AsyncSession, info: ChatInfo) -> Chat:
    """Chatni yaratadi yoki nomi/turini yangilaydi. Egasi qo'ygan override'lar o'zgarmaydi."""
    chat = await session.get(Chat, info.chat_id)
    if chat is None:
        chat = Chat(
            chat_id=info.chat_id,
            chat_type=info.chat_type,
            title=info.title,
            username=info.username,
            is_protected=info.is_protected,
            migrated_to_chat_id=info.migrated_to_chat_id,
        )
        session.add(chat)
    else:
        chat.chat_type = info.chat_type
        chat.title = info.title
        chat.username = info.username
        chat.is_protected = info.is_protected
        if info.migrated_to_chat_id is not None:
            chat.migrated_to_chat_id = info.migrated_to_chat_id
    await session.flush()
    return chat


# ------------------------------------------------------------------------ messages


async def save_message(
    session: AsyncSession,
    record: MessageRecord,
    source: MessageSource,
    *,
    observed_at: datetime | None = None,
) -> SaveOutcome:
    return (await save_messages(session, [record], source, observed_at=observed_at))[0]


async def save_messages(
    session: AsyncSession,
    records: Sequence[MessageRecord],
    source: MessageSource,
    *,
    observed_at: datetime | None = None,
) -> list[SaveOutcome]:
    """Xabarlarni idempotent saqlaydi.

    ``observed_at`` — ma'lumot Telegram'dan olingan vaqt (sahifa so'rovi yuborilgan payt).
    U o'chirilgan deb belgilangan xabar qayta ko'ringanda, bu nusxa o'chirishdan keyin
    olinganmi yoki oldinmi — shuni aniqlash uchun kerak.
    """
    if not records:
        return []
    now = utcnow()
    observed_at = observed_at or now
    existing = await _load_existing(session, records)
    outcomes: list[SaveOutcome] = []
    for record in records:
        key = (record.chat_id, record.message_id)
        message = existing.get(key)
        if message is None:
            existing[key] = await _insert_message(session, record, source, now)
            outcomes.append(SaveOutcome.CREATED)
        else:
            outcomes.append(await _merge_message(session, message, record, now, observed_at))
    await session.flush()
    return outcomes


async def _load_existing(session: AsyncSession, records: Sequence[MessageRecord]) -> dict[tuple[int, int], Message]:
    by_chat: dict[int, set[int]] = defaultdict(set)
    for record in records:
        by_chat[record.chat_id].add(record.message_id)
    found: dict[tuple[int, int], Message] = {}
    for chat_id, ids in by_chat.items():
        for chunk in _chunks(sorted(ids)):
            rows = await session.scalars(
                select(Message).where(Message.chat_id == chat_id, Message.message_id.in_(chunk))
            )
            for message in rows:
                found[(message.chat_id, message.message_id)] = message
    return found


def _reply_peer(record: MessageRecord) -> int | None:
    # Shu chatning o'ziga ishora qilsa saqlamaymiz — reply'lar bir xil ko'rinishda bo'lsin.
    if record.reply_to_peer_id == record.chat_id:
        return None
    return record.reply_to_peer_id


async def _insert_message(session: AsyncSession, record: MessageRecord, source: MessageSource, now: datetime) -> Message:
    message = Message(
        chat_id=record.chat_id,
        message_id=record.message_id,
        sender_id=record.sender_id,
        sender_name=record.sender_name,
        is_outgoing=record.is_outgoing,
        date=record.date,
        edit_date=record.edit_date,
        text=record.text or None,
        entities=record.entities or None,
        reply_to_msg_id=record.reply_to_msg_id,
        reply_to_peer_id=_reply_peer(record),
        reply_to_top_id=record.reply_to_top_id,
        fwd_from_id=record.fwd_from_id,
        fwd_from_name=record.fwd_from_name,
        fwd_date=record.fwd_date,
        fwd_channel_post_id=record.fwd_channel_post_id,
        grouped_id=record.grouped_id,
        media_type=record.media_type,
        action=record.action,
        noforwards=record.noforwards,
        content_hash=record.content_hash(),
        edit_count=0,
        source=source,
        archived_at=now,
        updated_at=now,
    )
    session.add(message)
    await session.flush()
    if record.media is not None and record.media.media_type.is_file:
        session.add(_new_media(message.id, record.media, now))
    return message


def _is_stale(stored_edit: datetime | None, incoming_edit: datetime | None) -> bool:
    """Kelgan nusxa bazadagidan eskimi (masalan, tahrirdan oldin olingan tarix sahifasi)."""
    if stored_edit is None:
        return False
    return incoming_edit is None or incoming_edit < stored_edit


async def _merge_message(
    session: AsyncSession,
    message: Message,
    record: MessageRecord,
    now: datetime,
    observed_at: datetime,
) -> SaveOutcome:
    new_hash = record.content_hash()
    if new_hash != message.content_hash:
        if _is_stale(message.edit_date, record.edit_date):
            return SaveOutcome.STALE
        await _archive_current_version(session, message, now)
        message.text = record.text or None
        message.entities = record.entities or None
        message.media_type = record.media_type
        message.action = record.action
        message.edit_date = record.edit_date
        message.content_hash = new_hash
        message.edit_count += 1
        await _sync_media(session, message, record.media, now)
        outcome = SaveOutcome.EDITED
    else:
        outcome = SaveOutcome.UNCHANGED
        if record.edit_date is not None and (message.edit_date is None or record.edit_date > message.edit_date):
            message.edit_date = record.edit_date

    if message.sender_name is None and record.sender_name:
        message.sender_name = record.sender_name

    if message.deletion_status is not None and message.deleted_at is not None and observed_at > message.deleted_at:
        _revoke_deletion(session, message, now)

    if session.is_modified(message):
        message.updated_at = now
    return outcome


async def _archive_current_version(session: AsyncSession, message: Message, now: datetime) -> None:
    last_replaced = await session.scalar(
        select(func.max(MessageVersion.replaced_at)).where(MessageVersion.message_pk == message.id)
    )
    session.add(
        MessageVersion(
            message_pk=message.id,
            version_no=message.edit_count + 1,
            text=message.text,
            entities=message.entities,
            media_type=message.media_type,
            edit_date=message.edit_date,
            content_hash=message.content_hash,
            observed_at=last_replaced or message.archived_at,
            replaced_at=now,
        )
    )


async def _sync_media(session: AsyncSession, message: Message, media: MediaInfo | None, now: datetime) -> None:
    current = await session.scalar(
        select(Media).where(Media.message_pk == message.id, Media.superseded_at.is_(None))
    )
    new_is_file = media is not None and media.media_type.is_file
    if current is not None:
        if new_is_file and _media_fingerprint(current.media_type, current.tg_media_id) == media.fingerprint:
            return
        current.superseded_at = now
        # Yangi joriy yozuvdan oldin flush: uq_media_current indeksi buzilmasin.
        await session.flush()
    if new_is_file:
        session.add(_new_media(message.id, media, now))


def _new_media(message_pk: int, media: MediaInfo, now: datetime) -> Media:
    return Media(
        message_pk=message_pk,
        media_type=media.media_type,
        tg_media_id=media.tg_media_id,
        tg_access_hash=media.tg_access_hash,
        tg_dc_id=media.tg_dc_id,
        mime_type=media.mime_type,
        file_name=media.file_name,
        expected_size=media.size,
        duration=media.duration,
        width=media.width,
        height=media.height,
        ttl_seconds=media.ttl_seconds,
        status=MediaStatus.PENDING,
        attempts=0,
        created_at=now,
        updated_at=now,
    )


async def get_message(session: AsyncSession, chat_id: int, message_id: int) -> Message | None:
    return await session.scalar(
        select(Message).where(Message.chat_id == chat_id, Message.message_id == message_id)
    )


async def get_versions(session: AsyncSession, message_pk: int) -> list[MessageVersion]:
    rows = await session.scalars(
        select(MessageVersion).where(MessageVersion.message_pk == message_pk).order_by(MessageVersion.version_no)
    )
    return list(rows)


# --------------------------------------------------------------------- reply chain


async def get_reply_parent(session: AsyncSession, message: Message) -> Message | None:
    """Reply qilingan xabar, agar u arxivda bo'lsa."""
    if message.reply_to_msg_id is None:
        return None
    return await get_message(session, message.reply_to_peer_id or message.chat_id, message.reply_to_msg_id)


async def get_reply_chain(session: AsyncSession, message: Message, *, max_depth: int = 50) -> list[Message]:
    """Reply zanjiri yuqoriga: [bevosita ota, uning otasi, ...]. Arxivda yo'q joyda to'xtaydi."""
    chain: list[Message] = []
    seen = {message.id}
    current = message
    while len(chain) < max_depth:
        parent = await get_reply_parent(session, current)
        if parent is None or parent.id in seen:
            break
        chain.append(parent)
        seen.add(parent.id)
        current = parent
    return chain


async def get_replies(session: AsyncSession, message: Message, *, limit: int = 100) -> list[Message]:
    """Shu xabarga shu chat ichida yozilgan javoblar, vaqt bo'yicha."""
    rows = await session.scalars(
        select(Message)
        .where(
            Message.chat_id == message.chat_id,
            Message.reply_to_msg_id == message.message_id,
            Message.reply_to_peer_id.is_(None),
        )
        .order_by(Message.date, Message.message_id)
        .limit(limit)
    )
    return list(rows)


# ---------------------------------------------------------------------- deletions


async def apply_deletions(
    session: AsyncSession,
    message_ids: Iterable[int],
    *,
    chat_id: int | None,
    source: DeletionSource = DeletionSource.EVENT,
) -> list[DeletionEvent]:
    """O'chirish hodisasini qayd etadi. Xabar yozuvlari HECH QACHON o'chirilmaydi.

    * ``chat_id`` berilgan (kanal/superguruh hodisasi yoki tekshiruv): shu chatdagi
      xabar ``CONFIRMED`` bo'ladi.
    * ``chat_id=None`` (shaxsiy chat / oddiy guruh hodisasi — Telegram chatni aytmaydi):
      faqat common box chatlari orasidan qidiriladi. Yagona moslik ``PROBABLE``,
      bir nechta moslik ``AMBIGUOUS`` (hech narsa belgilanmaydi), moslik yo'q ``NOT_ARCHIVED``.
    """
    ids = sorted(set(message_ids))
    now = utcnow()
    candidates: dict[int, list[Message]] = defaultdict(list)
    for chunk in _chunks(ids):
        query = select(Message).where(Message.message_id.in_(chunk))
        if chat_id is not None:
            query = query.where(Message.chat_id == chat_id)
        else:
            query = query.join(Chat, Chat.chat_id == Message.chat_id).where(
                Chat.chat_type.in_(sorted(COMMON_BOX_TYPES))
            )
        for message in await session.scalars(query):
            candidates[message.message_id].append(message)

    events: list[DeletionEvent] = []
    for message_id in ids:
        found = candidates.get(message_id, [])
        target: Message | None = None
        if not found:
            resolution = DeletionResolution.NOT_ARCHIVED
        elif len(found) > 1:
            resolution = DeletionResolution.AMBIGUOUS
        else:
            target = found[0]
            if chat_id is not None:
                resolution = DeletionResolution.CONFIRMED
                target.deletion_status = DeletionStatus.CONFIRMED
            else:
                resolution = DeletionResolution.PROBABLE
                if target.deletion_status is None:
                    target.deletion_status = DeletionStatus.PROBABLE
            if target.deleted_at is None:
                target.deleted_at = now
            target.updated_at = now
        event = DeletionEvent(
            detected_at=now,
            chat_id=chat_id,
            message_id=message_id,
            resolution=resolution,
            source=source,
            message_pk=target.id if target else None,
            resolved_chat_id=target.chat_id if target else None,
            note=f"{len(found)} ta nomzod" if resolution is DeletionResolution.AMBIGUOUS else None,
        )
        session.add(event)
        events.append(event)
    await session.flush()
    return events


async def revoke_deletions(
    session: AsyncSession,
    chat_id: int,
    message_ids: Iterable[int],
    *,
    source: DeletionSource = DeletionSource.VERIFICATION,
) -> list[int]:
    """Tekshiruvda xabar Telegram'da hali mavjudligi ko'rinsa — o'chirish belgisini bekor qiladi.

    Qaytaradi: belgisi bekor qilingan ``message_id`` lar.
    """
    ids = sorted(set(message_ids))
    now = utcnow()
    revoked: list[int] = []
    for chunk in _chunks(ids):
        rows = await session.scalars(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.message_id.in_(chunk),
                Message.deletion_status.is_not(None),
            )
        )
        for message in rows:
            _revoke_deletion(session, message, now, source=source)
            message.updated_at = now
            revoked.append(message.message_id)
    await session.flush()
    return sorted(revoked)


def _revoke_deletion(
    session: AsyncSession, message: Message, now: datetime, *, source: DeletionSource = DeletionSource.RESYNC
) -> None:
    session.add(
        DeletionEvent(
            detected_at=now,
            chat_id=message.chat_id,
            message_id=message.message_id,
            resolution=DeletionResolution.REVOKED,
            source=source,
            message_pk=message.id,
            resolved_chat_id=message.chat_id,
            note=f"avvalgi holat: {message.deletion_status}",
        )
    )
    message.deletion_status = None
    message.deleted_at = None


# ------------------------------------------------------------------ kanal oynasi


async def prune_channel_window(session: AsyncSession, chat_id: int, keep_from_id: int) -> list[int]:
    """Kanal oynasidan chiqqan postlarni (``message_id < keep_from_id``) arxivdan olib tashlaydi.

    Reply qilingan postlar saqlanadi: boshqa chatdan qilingan reply nishoni va oyna
    ichidagi postlar reply qilgan eski postlar. Izohlar muhokama guruhi chatida
    turadi va bu funksiyaga bog'liq emas. Versiyalar va media qatorlari CASCADE bilan,
    FTS indeksi trigger bilan tozalanadi. Qaytaradi: olib tashlangan ``message_id`` lar.
    """
    cross = aliased(Message)
    inside = aliased(Message)
    cross_reply = exists().where(cross.reply_to_peer_id == chat_id, cross.reply_to_msg_id == Message.message_id)
    inside_reply = exists().where(
        inside.chat_id == chat_id,
        inside.reply_to_peer_id.is_(None),
        inside.reply_to_msg_id == Message.message_id,
        inside.message_id >= keep_from_id,
    )
    rows = (
        await session.execute(
            select(Message.id, Message.message_id).where(
                Message.chat_id == chat_id,
                Message.message_id < keep_from_id,
                ~cross_reply,
                ~inside_reply,
            )
        )
    ).all()
    pks = [row.id for row in rows]
    await delete_messages(session, pks)
    return sorted(row.message_id for row in rows)


async def delete_messages(session: AsyncSession, message_pks: Sequence[int]) -> None:
    """Xabarlarni arxivdan butunlay olib tashlaydi (kanal oynasi, keyinroq retention).

    Versiyalar va media qatorlari CASCADE bilan o'chadi. Yuklangan fayl boshqa xabarda
    ishlatilmasa, ``media_trash`` ga yoziladi — faylni media worker o'chiradi.
    """
    paths: set[str] = set()
    for chunk in _chunks(list(message_pks)):
        paths.update(
            await session.scalars(
                select(Media.local_path).where(Media.message_pk.in_(chunk), Media.local_path.is_not(None))
            )
        )
        await session.execute(delete(Message).where(Message.id.in_(chunk)))
    for path in sorted(paths):
        still_used = await session.scalar(select(Media.id).where(Media.local_path == path).limit(1))
        queued = await session.scalar(select(MediaTrash.id).where(MediaTrash.local_path == path))
        if still_used is None and queued is None:
            session.add(MediaTrash(local_path=path))
    await session.flush()


# -------------------------------------------------------------------------- audit


async def add_audit(
    session: AsyncSession,
    action: str,
    *,
    actor: str = "system",
    actor_id: int | None = None,
    target: str | None = None,
    ok: bool = True,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(actor=actor, actor_id=actor_id, action=action, target=target, ok=ok, details=details)
    session.add(entry)
    await session.flush()
    return entry
