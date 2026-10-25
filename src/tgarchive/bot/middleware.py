"""Faqat egasi: barcha update'lar uchun bitta tashqi (outer) middleware.

Ruxsat faqat ``OWNER_ID`` foydalanuvchiga va faqat u bilan shaxsiy chatda. Bot guruhga
qo'shilsa ham, u yerda egasi yozsa ham — javob yo'q (arxiv boshqalarga ko'rinmasin).
Begonaga hech qanday javob berilmaydi (bot borligini ham tasdiqlamaydi); urinish
``audit_log`` ga yoziladi — xabar matnisiz, har foydalanuvchi uchun 10 daqiqada ko'pi bilan bir marta.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from tgarchive.db.engine import Database
from tgarchive.db.store import add_audit
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

AUDIT_EVERY_S = 600.0
_MAX_TRACKED = 1000


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(
        self,
        owner_id: int,
        db: Database,
        *,
        audit_every_s: float = AUDIT_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.owner_id = owner_id
        self.db = db
        self.audit_every_s = audit_every_s
        self._clock = clock
        self._last_audit: dict[int | None, float] = {}

    def allowed(self, data: dict[str, Any]) -> bool:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or user.id != self.owner_id:
            return False
        return chat is None or (chat.id == self.owner_id and chat.type == "private")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self.allowed(data):
            return await handler(event, data)
        await self._record(event, data)
        return None

    async def _record(self, event: TelegramObject, data: dict[str, Any]) -> None:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        user_id = user.id if user else None
        now = self._clock()
        last = self._last_audit.get(user_id)
        if last is not None and now - last < self.audit_every_s:
            return
        if len(self._last_audit) >= _MAX_TRACKED:
            self._last_audit.clear()
        self._last_audit[user_id] = now
        details = {
            "update": getattr(event, "event_type", type(event).__name__),
            "chat_id": chat.id if chat else None,
            "chat_type": chat.type if chat else None,
            "username": user.username if user else None,
        }
        log.warning("bot_unauthorized", user_id=user_id, **{k: v for k, v in details.items() if k != "username"})
        try:
            async with self.db.write() as session:
                await add_audit(
                    session,
                    "unauthorized",
                    actor="owner" if user_id == self.owner_id else "stranger",
                    actor_id=user_id,
                    ok=False,
                    details=details,
                )
        except Exception:
            log.exception("audit_write_failed")
