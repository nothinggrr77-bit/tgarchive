"""Archiver xizmati: ``tgarchive run`` (doimiy, 24/7) va ``tgarchive import`` (bir martalik).

Aylanish::

    dialoglar sinxroni (har SYNC_INTERVAL_S) -> importer tick -> ish qolmasa keyingi sinxrongacha kutish

* Holat har ``HEARTBEAT_INTERVAL_S`` da ``component_heartbeats`` ga yoziladi
  (``tgarchive status``, Docker healthcheck, keyinroq bot ``/status``).
* Tarmoq uzilsa — kutib, qayta ulanadi (10 s dan 5 daqiqagacha oshib boradi).
* Session yaroqsiz bo'lsa (``Devices`` dan o'chirilgan) xizmat yiqilmaydi: aks holda
  Docker uni to'xtovsiz qayta ishga tushirib, Telegram'ga keraksiz so'rov yuboradi.
  Holat ``auth_error`` bo'ladi va ``tgarchive login`` kutiladi.
* ``SIGTERM`` / ``SIGINT`` da joriy sahifa yozib bo'linadi, keyin toza to'xtaydi.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from telethon import errors

from tgarchive.archiver.client import create_client
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import FATAL_ERRORS, TRANSIENT_ERRORS, HistoryImporter, Sleep
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.meta import AccountMismatchError, bind_account
from tgarchive.db.status import ARCHIVER, beat
from tgarchive.db.types import utcnow
from tgarchive.exitcodes import EXIT_AUTH, EXIT_OK
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

RECONNECT_BACKOFF_S = (10, 30, 60, 120, 300)
ERROR_RETRY_S = 60
AUTH_ERROR_LOG_EVERY_S = 3600


class _AuthProblem(Exception):
    pass


class ArchiverService:
    def __init__(
        self,
        settings: ArchiverSettings,
        *,
        client_factory: Callable[[ArchiverSettings], Any] | None = None,
        stop: asyncio.Event | None = None,
        sleep: Sleep | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory or (lambda s: create_client(s))
        self.stop = stop or asyncio.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._clock = clock
        self.started_at = clock()
        self.state = "starting"
        self.error: str | None = None
        self.account_id: int | None = None
        self.last_sync_at: datetime | None = None
        self.importer: HistoryImporter | None = None

    async def run(self, *, once: bool = False) -> int:
        db = Database(self.settings.database_url, busy_timeout_ms=self.settings.db_busy_timeout_ms)
        client = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self._beat(db)
            heartbeat = asyncio.create_task(self._heartbeat_loop(db))
            if not self.settings.session_path.exists():
                return await self._auth_failed(db, "session yo'q: avval tgarchive login", once)
            client = self.client_factory(self.settings)
            try:
                me = await self._connect(client)
                async with db.write() as session:
                    await bind_account(session, me.id)
            except (_AuthProblem, AccountMismatchError, *FATAL_ERRORS) as exc:
                return await self._auth_failed(db, str(exc) or type(exc).__name__, once)
            self.account_id = me.id
            log.info("archiver_started", account_id=me.id, once=once)
            self.importer = HistoryImporter(
                client, db, self.settings, self_id=me.id, stop=self.stop, sleep=self._sleep, clock=self._clock
            )
            return await self._loop(client, db, once=once)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if self.state != "auth_error":
                self.state = "stopped"
            await self._beat(db)
            if client is not None:
                await client.disconnect()
            await db.dispose()
            log.info("archiver_stopped", state=self.state)

    async def _connect(self, client: Any) -> Any:
        await client.connect()
        if not await client.is_user_authorized():
            raise _AuthProblem("session yaroqsiz yoki Telegram'da bekor qilingan: qayta tgarchive login")
        return await client.get_me()

    async def _loop(self, client: Any, db: Database, *, once: bool) -> int:
        assert self.importer is not None
        next_sync: float | None = None
        failures = 0
        while not self.stop.is_set():
            try:
                if next_sync is None or time.monotonic() >= next_sync:
                    self.state = "syncing"
                    await sync_dialogs(client, db, self.settings)
                    self.last_sync_at = self._clock()
                    next_sync = time.monotonic() + self.settings.sync_interval_s
                self.state = "importing"
                more = await self.importer.run_tick()
                failures = 0
            except FATAL_ERRORS as exc:
                return await self._auth_failed(db, f"session yaroqsiz: {type(exc).__name__}", once)
            except TRANSIENT_ERRORS as exc:
                delay = RECONNECT_BACKOFF_S[min(failures, len(RECONNECT_BACKOFF_S) - 1)]
                failures += 1
                self.state = "reconnecting"
                log.warning("telegram_unreachable", error=type(exc).__name__, retry_in=delay)
                await self._sleep(delay)
                with suppress(*TRANSIENT_ERRORS):
                    await client.connect()
                continue
            except errors.FloodWaitError as exc:
                log.warning("flood_wait", where="dialogs", seconds=exc.seconds)
                await self._sleep(min(exc.seconds, self.settings.max_flood_wait_s))
                continue
            except Exception:
                log.exception("archiver_loop_error", retry_in=ERROR_RETRY_S)
                await self._sleep(ERROR_RETRY_S)
                continue
            if not more:
                if once:
                    return EXIT_OK
                self.state = "idle"
                await self._sleep(max(0.0, (next_sync or 0) - time.monotonic()))
        return EXIT_OK

    async def _auth_failed(self, db: Database, reason: str, once: bool) -> int:
        self.state = "auth_error"
        self.error = reason
        log.error("archiver_auth_error", reason=reason)
        await self._beat(db)
        if once:
            return EXIT_AUTH
        while not self.stop.is_set():
            await self._sleep(AUTH_ERROR_LOG_EVERY_S)
            if not self.stop.is_set():
                log.error("archiver_auth_error", reason=reason)
        return EXIT_AUTH

    def details(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "job": dict(self.importer.progress) if self.importer else {},
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "error": self.error,
        }

    async def _beat(self, db: Database) -> None:
        try:
            async with db.write() as session:
                await beat(session, ARCHIVER, self.state, started_at=self.started_at, details=self.details())
        except Exception:
            log.exception("heartbeat_failed")

    async def _heartbeat_loop(self, db: Database) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_s)
            await self._beat(db)

    async def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
