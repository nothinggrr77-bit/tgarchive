"""Saqlash muddati (retention): muddati o'tgan xabarlarni arxivdan olib tashlash.

Sukut bo'yicha o'chiq (egasi qarori: shaxsiy ma'lumotlar doimiy saqlanadi). Yoqish:
global ``RETENTION_DAYS`` yoki chat bo'yicha ``chats.retention_days`` (``tgarchive retention set``).
Chat sozlamasi globaldan ustun.

* Xabar sanasi bo'yicha (``messages.date``), kichik partiyalar bilan — qulf qisqa.
* Versiyalar va media qatorlari CASCADE bilan o'chadi; fayllar ``media_trash`` orqali.
* ``audit_log`` ga faqat sonlar yoziladi, mazmun emas.
* Backfill shu chegaradan eskisini qayta import qilmaydi (``HistoryImporter._cutoff``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from tgarchive.db.engine import Database
from tgarchive.db.enums import ArchiveMode
from tgarchive.db.models import Chat, Message
from tgarchive.db.store import add_audit, delete_messages

BATCH = 1000


async def apply_retention(db: Database, *, now: datetime, global_days: int | None) -> dict[int, int]:
    """Qaytaradi: chat_id -> olib tashlangan xabarlar soni (faqat > 0)."""
    async with db.read() as session:
        chats = list(
            await session.execute(
                select(Chat.chat_id, Chat.retention_days).where(
                    (Chat.archive_mode.is_(None)) | (Chat.archive_mode != ArchiveMode.SKIP)
                )
            )
        )
    removed: dict[int, int] = {}
    for chat_id, chat_days in chats:
        days = chat_days or global_days
        if not days:
            continue
        cutoff = now - timedelta(days=days)
        while True:
            async with db.write() as session:
                pks = list(
                    await session.scalars(
                        select(Message.id).where(Message.chat_id == chat_id, Message.date < cutoff).limit(BATCH)
                    )
                )
                if not pks:
                    break
                await delete_messages(session, pks)
            removed[chat_id] = removed.get(chat_id, 0) + len(pks)
    if removed:
        async with db.write() as session:
            await add_audit(
                session,
                "retention_applied",
                details={"total": sum(removed.values()), "chats": {str(k): v for k, v in removed.items()}},
            )
    return removed


async def set_chat_retention(db: Database, chat_id: int, days: int | None) -> bool:
    async with db.write() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            return False
        chat.retention_days = days
        await add_audit(
            session, "retention_set", actor="owner", target=f"chat:{chat_id}", details={"days": days}
        )
    return True
