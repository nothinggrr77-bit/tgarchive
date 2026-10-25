"""Akkaunt dialoglarini aniqlash va har biriga arxivlash siyosatini qo'llash.

``fetch_dialogs`` Telegram'dan o'qiydi (Archive papkasi ham kiradi — Telethon
``iter_dialogs`` ``folder`` berilmasa barcha papkalarni qaytaradi). ``plan_dialogs``
bazadagi egasi qarorlarini (override) hisobga olib siyosatni hisoblaydi va
``save=True`` bo'lsa chatlarni bazaga yozadi.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive.archiver.convert import chat_info_from_entity
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import ArchiveMode, SyncStatus
from tgarchive.db.models import Chat, ChatSyncState
from tgarchive.db.store import ChatInfo, add_audit, upsert_chat
from tgarchive.logging_setup import get_logger
from tgarchive.policy import ChatPolicy, decide_policy

log = get_logger(__name__)
TITLE_WIDTH = 32


@dataclass(frozen=True, slots=True)
class DialogInfo:
    chat: ChatInfo
    top_message_id: int | None
    in_archive_folder: bool
    unread_count: int


@dataclass(frozen=True, slots=True)
class PlannedDialog:
    dialog: DialogInfo
    policy: ChatPolicy


async def fetch_dialogs(client: Any, *, limit: int | None = None) -> list[DialogInfo]:
    result: list[DialogInfo] = []
    async for dialog in client.iter_dialogs(limit=limit):
        try:
            chat = chat_info_from_entity(dialog.entity)
        except TypeError:
            log.warning("dialog_skipped", entity_type=type(dialog.entity).__name__)
            continue
        top = getattr(dialog.message, "id", None)
        result.append(DialogInfo(chat, top, bool(dialog.archived), int(dialog.unread_count or 0)))
    return result


async def plan_dialogs(
    db: Database,
    settings: ArchiverSettings,
    dialogs: Sequence[DialogInfo],
    *,
    save: bool = False,
) -> list[PlannedDialog]:
    if not save:
        async with db.read() as session:
            ids = [d.chat.chat_id for d in dialogs]
            rows = {c.chat_id: c for c in await session.scalars(select(Chat).where(Chat.chat_id.in_(ids)))}
        return [_plan(d, settings, rows.get(d.chat.chat_id)) for d in dialogs]

    planned: list[PlannedDialog] = []
    async with db.write() as session:
        for dialog in dialogs:
            row = await upsert_chat(session, dialog.chat)
            item = _plan(dialog, settings, row)
            row.archive_mode = item.policy.mode
            planned.append(item)
        counts = Counter(p.policy.mode.value for p in planned)
        await add_audit(session, "dialogs_saved", details={"total": len(planned), **counts})
    return planned


async def sync_dialogs(client: Any, db: Database, settings: ArchiverSettings) -> list[PlannedDialog]:
    """Xizmat uchun: dialoglarni o'qiydi, chatlar va checkpoint'larni bitta tranzaksiyada yangilaydi.

    * Yangi arxivlanadigan chat: ``pending``, ``high_water_id`` = hozirgi eng yangi ID
      (undan yangilari oldinga sinxronlash bilan, eskilari backfill bilan olinadi).
    * ``remote_top_id`` — har safar yangilanadi; ``high_water_id`` dan katta bo'lsa,
      chatda yangi xabar bor.
    * Siyosat ``skip`` ga o'zgarsa — ``disabled``; qayta yoqilsa — ``pending``
      (avvalgi cursor'dan davom etadi).
    """
    dialogs = await fetch_dialogs(client)
    planned: list[PlannedDialog] = []
    async with db.write() as session:
        for dialog in dialogs:
            policy = await register_chat(session, dialog.chat, settings, top_message_id=dialog.top_message_id)
            planned.append(PlannedDialog(dialog, policy))
    counts = Counter(p.policy.mode.value for p in planned)
    log.info("dialogs_synced", total=len(planned), **counts)
    return planned


async def register_chat(
    session: AsyncSession, chat: ChatInfo, settings: ArchiverSettings, *, top_message_id: int | None
) -> ChatPolicy:
    """Chatni yozadi, siyosatni qo'llaydi va checkpoint holatini yangilaydi (``sync_dialogs`` qoidalari).

    Real vaqtda yangi chat paydo bo'lganda ham shu funksiya ishlatiladi.
    """
    row = await upsert_chat(session, chat)
    policy = decide_policy(
        chat, settings, archive_override=row.archive_override, media_override=row.media_override
    )
    row.archive_mode = policy.mode
    state = await session.get(ChatSyncState, chat.chat_id)
    if policy.archived:
        if state is None:
            state = ChatSyncState(
                chat_id=chat.chat_id,
                backfill_status=SyncStatus.PENDING,
                high_water_id=top_message_id,
                backfill_imported=0,
                attempts=0,
            )
            session.add(state)
        elif state.backfill_status is SyncStatus.DISABLED:
            state.backfill_status = SyncStatus.PENDING
        if top_message_id is not None:
            state.remote_top_id = top_message_id
    elif state is not None and state.backfill_status is not SyncStatus.DISABLED:
        state.backfill_status = SyncStatus.DISABLED
    await session.flush()
    return policy


def _plan(dialog: DialogInfo, settings: ArchiverSettings, row: Chat | None) -> PlannedDialog:
    policy = decide_policy(
        dialog.chat,
        settings,
        archive_override=row.archive_override if row else None,
        media_override=row.media_override if row else None,
    )
    return PlannedDialog(dialog, policy)


def _mode_label(policy: ChatPolicy) -> str:
    if policy.mode is ArchiveMode.WINDOW:
        return f"window{policy.keep_last}"
    return policy.mode.value


def _shorten(text: str | None, width: int = TITLE_WIDTH) -> str:
    text = (text or "-").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def format_plan(planned: Sequence[PlannedDialog]) -> str:
    """Terminal uchun jadval: rejim, tur, ID, nom, sabab, media."""
    lines = [f"{'REJIM':<9} {'TUR':<10} {'CHAT ID':>15}  {'NOM':<{TITLE_WIDTH}}  SABAB / MEDIA"]
    for item in planned:
        chat, policy = item.dialog.chat, item.policy
        has_media = "ha" if policy.media else "yo'q"
        media = f"media: {has_media} ({policy.media_reason})"
        folder = " [Archive]" if item.dialog.in_archive_folder else ""
        lines.append(
            f"{_mode_label(policy):<9} {chat.chat_type.value:<10} {chat.chat_id:>15}  "
            f"{_shorten(chat.title):<{TITLE_WIDTH}}  {policy.reason}; {media}{folder}"
        )
    counts = Counter(p.policy.mode for p in planned)
    lines.append(
        f"Jami: {len(planned)} | to'liq: {counts[ArchiveMode.FULL]} | "
        f"oxirgi postlar: {counts[ArchiveMode.WINDOW]} | o'tkazib yuboriladi: {counts[ArchiveMode.SKIP]}"
    )
    return "\n".join(lines)
