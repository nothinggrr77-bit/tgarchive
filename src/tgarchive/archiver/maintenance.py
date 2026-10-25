"""Archiver ichidagi rejali ishlar: zaxira, saqlash muddati (retention), eski eksportlarni tozalash.

* Zaxira har ``BACKUP_INTERVAL_H`` da (sukut 24 soat). Oxirgi natija ``app_meta.last_backup`` da —
  ``tgarchive status`` va bot ``/status`` shuni ko'rsatadi. Xato bo'lsa, 1 soatdan keyin qayta urinadi.
* Zaxira alohida thread'da (``asyncio.to_thread``) — real vaqt hodisalari to'xtamaydi.
* Retention va eksport tozalash — kuniga bir marta.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from tgarchive.archiver.history import Sleep
from tgarchive.backup import BackupInfo, clean_stale_tmp, create_backup
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.meta import LAST_BACKUP_KEY, LAST_RETENTION_KEY, get_json_meta, set_json_meta
from tgarchive.db.retention import apply_retention
from tgarchive.db.store import add_audit
from tgarchive.db.types import utcnow
from tgarchive.export import cleanup_exports
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

CHECK_S = 600.0
RETRY_AFTER_ERROR = timedelta(hours=1)
DAILY = timedelta(days=1)


class Maintenance:
    def __init__(
        self,
        db: Database,
        settings: ArchiverSettings,
        *,
        stop: asyncio.Event | None = None,
        sleep: Sleep | None = None,
        clock: Callable[[], datetime] = utcnow,
        backup_fn: Callable[..., BackupInfo] = create_backup,
    ) -> None:
        self.db = db
        self.settings = settings
        self.stop = stop or asyncio.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._clock = clock
        self._backup_fn = backup_fn
        self.state = "idle"
        self.last_error_at: datetime | None = None
        self.last_daily: datetime | None = None

    async def run(self) -> None:
        clean_stale_tmp(self.settings.paths.backups, self._clock())
        while not self.stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("maintenance_failed")
            await self._sleep(CHECK_S)

    async def tick(self) -> None:
        now = self._clock()
        if self.settings.backup_enabled and await self.backup_due(now):
            await self.backup(now)
        if self.last_daily is None or now - self.last_daily >= DAILY:
            self.last_daily = now
            await self.daily(now)

    async def backup_due(self, now: datetime) -> bool:
        if self.last_error_at is not None and now - self.last_error_at < RETRY_AFTER_ERROR:
            return False
        async with self.db.read() as session:
            meta = await get_json_meta(session, LAST_BACKUP_KEY) or {}
        if not meta.get("at"):
            return True
        return now - datetime.fromisoformat(meta["at"]) >= timedelta(hours=self.settings.backup_interval_h)

    async def backup(self, now: datetime) -> BackupInfo | None:
        self.state = "backup"
        try:
            info = await asyncio.to_thread(self._backup_fn, self.settings, now=now)
        except Exception as exc:
            self.last_error_at = now
            log.exception("backup_failed")
            await self._record(now, ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
            return None
        finally:
            self.state = "idle"
        self.last_error_at = None
        await self._record(now, ok=True, info=info)
        log.info("backup_created", file=info.path.name, size=info.size, encrypted=info.encrypted)
        if not info.encrypted:
            log.warning("backup_not_encrypted", hint="BACKUP_AGE_RECIPIENTS ga age ochiq kalitini yozing")
        return info

    async def _record(self, now: datetime, *, ok: bool, info: BackupInfo | None = None, error: str | None = None) -> None:
        async with self.db.write() as session:
            meta: dict[str, Any] = await get_json_meta(session, LAST_BACKUP_KEY) or {}
            if ok and info is not None:
                meta = {
                    "at": now.isoformat(),
                    "file": info.path.name,
                    "size": info.size,
                    "encrypted": info.encrypted,
                }
                await add_audit(
                    session, "backup_created", target=info.path.name, details={"size": info.size, "encrypted": info.encrypted}
                )
            else:
                meta.update({"error": error, "error_at": now.isoformat()})
                await add_audit(session, "backup_failed", ok=False, details={"error": error})
            await set_json_meta(session, LAST_BACKUP_KEY, meta)

    async def daily(self, now: datetime) -> None:
        removed = await apply_retention(self.db, now=now, global_days=self.settings.retention_days)
        if removed:
            log.info("retention_applied", total=sum(removed.values()), chats=len(removed))
        async with self.db.write() as session:
            await set_json_meta(
                session, LAST_RETENTION_KEY, {"at": now.isoformat(), "removed": sum(removed.values())}
            )
        exports = cleanup_exports(self.settings.paths.exports, self.settings.export_keep_days, now)
        if exports:
            log.info("exports_cleaned", removed=len(exports))

    def details(self) -> dict[str, Any]:
        return {"state": self.state}

    async def _interruptible_sleep(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
