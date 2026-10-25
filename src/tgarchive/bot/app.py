"""Admin botni ishga tushirish: ``tgarchive bot``.

* Bot migratsiya qilmaydi — bazani archiver yangilaydi. Sxema eski bo'lsa, bot kutadi
  (ikki jarayon bir vaqtda migratsiya qilmasin).
* Holat har ``BOT_HEARTBEAT_S`` da ``component_heartbeats`` ga (``bot``) yoziladi — Docker
  healthcheck shuni tekshiradi.
* ``SIGTERM`` / ``SIGINT`` ni aiogram o'zi ushlaydi va polling'ni to'xtatadi.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from tgarchive.bot.handlers import build_router, set_owner_commands
from tgarchive.bot.middleware import OwnerOnlyMiddleware
from tgarchive.bot.views import Views
from tgarchive.config import BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.migrate import current_revision, head_revision
from tgarchive.db.status import beat
from tgarchive.db.types import utcnow
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

BOT_COMPONENT = "bot"
BOT_HEARTBEAT_S = 30
SCHEMA_WAIT_S = 10.0


def build_dispatcher(db: Database, settings: BotSettings, *, started_at: datetime | None = None) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_id, db))
    views = Views(db, settings, started_at=started_at)
    dispatcher.include_router(build_router(views))

    @dispatcher.errors()
    async def on_error(event: ErrorEvent, bot: Bot) -> None:
        log.error("bot_handler_failed", error=type(event.exception).__name__, exc_info=event.exception)
        with suppress(Exception):
            await bot.send_message(settings.owner_id, "⚠️ Ichki xato yuz berdi, tafsiloti server log'ida.")

    return dispatcher


def make_bot(settings: BotSettings, **kwargs: Any) -> Bot:
    return Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        **kwargs,
    )


async def wait_for_schema(db: Database, stop: asyncio.Event, *, interval_s: float = SCHEMA_WAIT_S) -> bool:
    """Baza archiver tomonidan oxirgi migratsiyagacha yangilanishini kutadi."""
    head = head_revision()
    warned = False
    while not stop.is_set():
        try:
            async with db.read() as session:
                conn = await session.connection()
                revision = await conn.run_sync(current_revision)
        except Exception as exc:  # baza hali yo'q yoki band
            revision = None
            if not warned:
                log.info("bot_db_unavailable", error=type(exc).__name__)
        if revision == head:
            return True
        if not warned:
            log.warning("bot_waiting_for_schema", current=revision, required=head, hint="archiver migratsiyani qiladi")
            warned = True
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval_s)
    return False


async def _heartbeat(db: Database, started_at: datetime, details: Callable[[], dict[str, Any]]) -> None:
    while True:
        try:
            async with db.write() as session:
                await beat(session, BOT_COMPONENT, "running", started_at=started_at, details=details())
        except Exception:
            log.exception("bot_heartbeat_failed")
        await asyncio.sleep(BOT_HEARTBEAT_S)


async def run_bot(settings: BotSettings, *, bot: Bot | None = None) -> None:
    started_at = utcnow()
    db = Database(settings.database_url, busy_timeout_ms=settings.db_busy_timeout_ms)
    stop = asyncio.Event()
    bot = bot or make_bot(settings)
    heartbeat: asyncio.Task[None] | None = None
    try:
        if not await wait_for_schema(db, stop):
            return
        me = await bot.get_me()
        log.info("bot_started", username=me.username, owner_id=settings.owner_id)
        await set_owner_commands(bot, settings.owner_id)
        heartbeat = asyncio.create_task(
            _heartbeat(db, started_at, lambda: {"username": me.username, "owner_id": settings.owner_id})
        )
        dispatcher = build_dispatcher(db, settings, started_at=started_at)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(Exception):
                async with db.write() as session:
                    await beat(session, BOT_COMPONENT, "stopped", started_at=started_at)
        await bot.session.close()
        await db.dispose()
        log.info("bot_stopped")
