"""8-bosqich: media arxivi — navbat, siyosat, yuklash, tekshirish, disk nazorati.

Navbat — ``media`` jadvalining o'zi (``status`` + ``next_attempt_at``); alohida xizmat kerak emas.
Tarix importi va real vaqt har bir fayl uchun ``pending`` qator yaratadi, worker esa:

1. **Tasniflaydi.** View-once va himoyalangan (noforwards) — hech qachon. Siyosat
   (``MEDIA_ENABLED``, kanal, egasi qarori), tur (``MEDIA_TYPES``) va hajm
   (``MEDIA_MAX_FILE_MB``) bo'yicha yuklanmaydiganlar ``skipped`` + ``skip_reason``.
   Katta fayl (``too_large``) — nomi, hajmi, turi va xabari bilan arxivda ko'rinib turadi.
2. **Takrorni tekshiradi.** Xuddi shu fayl (forward) oldin yuklangan bo'lsa — qayta yuklamaydi.
3. **Diskni tekshiradi.** Bo'sh joy ``MEDIA_MIN_FREE_GB`` dan kam yoki ``MEDIA_MAX_TOTAL_GB``
   to'lgan bo'lsa — yuklash to'xtaydi, navbat saqlanadi.
4. **Yuklaydi.** Xabar Telegram'dan qayta olinadi (``file_reference`` eskiradi), fayl
   ``.tmp`` ga oqim bilan yoziladi, sha256 hisoblanadi, keyin atomik ``rename``.
   Yuklash paytida hajm chegaradan oshsa — to'xtatiladi va ``too_large`` bo'ladi.
5. **Xatoda** — 1 daqiqa → 12 soatgacha oshib boruvchi qayta urinish, ``MEDIA_MAX_ATTEMPTS``
   dan keyin ``failed``. FloodWait urinish hisoblanmaydi.

Fayl yo'li faqat raqamlardan va tozalangan kengaytmadan yasaladi:
``MEDIA_DIR/<chat_id>/<YYYY-MM>/<message_id>-<media_id>.<ext>``. O'qishda yo'l har doim
``MEDIA_DIR`` ichida ekanligi tekshiriladi.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import select, update
from telethon import errors
from telethon.tl import types

from tgarchive.archiver.history import FATAL_ERRORS, INACCESSIBLE_ERRORS, TRANSIENT_ERRORS, Sleep
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import REVIEWABLE_SKIPS, MediaSkipReason, MediaStatus, MediaType
from tgarchive.db.media import format_size, media_summary
from tgarchive.db.meta import get_meta, set_meta
from tgarchive.db.models import Chat, Media, MediaTrash, Message
from tgarchive.db.store import ChatInfo
from tgarchive.db.types import utcnow
from tgarchive.fs import ensure_private_dir
from tgarchive.logging_setup import get_logger
from tgarchive.mediafiles import hash_file, media_relpath, resolve_media_path
from tgarchive.policy import media_decision

log = get_logger(__name__)

BATCH = 20
IDLE_S = 30.0
PAUSE_S = 600.0
RETRY_S = (60, 300, 1800, 7200, 43200)
TRANSIENT_RETRY_S = 60
VERIFY_CHUNK = 500
TRASH_CHUNK = 200
TMP_DIR = ".tmp"
POLICY_META_KEY = "media_policy"
NOTE_LIMIT = 255


class Decision(NamedTuple):
    download: bool
    reason: MediaSkipReason | None = None
    note: str | None = None


class TooLarge(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


# ------------------------------------------------------------------ siyosat


def classify(media: Media, message: Message, chat: Chat, settings: ArchiverSettings) -> Decision:
    if media.ttl_seconds:
        return Decision(False, MediaSkipReason.VIEW_ONCE, "bir marta ko'riladigan media")
    if message.noforwards or chat.is_protected:
        return Decision(False, MediaSkipReason.PROTECTED, "himoyalangan kontent (noforwards)")
    if media.force_download:
        return Decision(True)
    info = ChatInfo(chat.chat_id, chat.chat_type, chat.title, chat.username, chat.is_protected)
    allowed, why = media_decision(info, settings, chat.media_override)
    if not allowed:
        return Decision(False, MediaSkipReason.POLICY, why)
    if media.media_type not in settings.media_types:
        return Decision(False, MediaSkipReason.TYPE, f"{media.media_type.value} MEDIA_TYPES da yo'q")
    limit = settings.media_max_file_mb * 1024 * 1024
    if media.expected_size is not None and media.expected_size > limit:
        return Decision(
            False,
            MediaSkipReason.TOO_LARGE,
            f"katta fayl: {format_size(media.expected_size)} > {settings.media_max_file_mb} MB",
        )
    return Decision(True)


def policy_fingerprint(settings: ArchiverSettings) -> str:
    return json.dumps(
        {
            "enabled": settings.media_enabled,
            "types": sorted(settings.media_types),
            "max_mb": settings.media_max_file_mb,
            "channels": settings.media_in_channels,
        },
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class VerifyReport:
    checked: int = 0
    missing: int = 0
    mismatched: int = 0


@dataclass(frozen=True, slots=True)
class _Job:
    media_pk: int
    chat_id: int
    message_id: int
    relpath: str
    force: bool


# --------------------------------------------------------------------- worker


class MediaWorker:
    def __init__(
        self,
        client: Any,
        db: Database,
        settings: ArchiverSettings,
        *,
        stop: asyncio.Event | None = None,
        sleep: Sleep | None = None,
        clock: Callable[[], datetime] = utcnow,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.root = settings.paths.media
        self.tmp = self.root / TMP_DIR
        self.stop = stop or asyncio.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._clock = clock
        self._disk_usage = disk_usage
        self.state = "starting"
        self.paused: str | None = None
        self.current: dict[str, Any] = {}
        self.stats: Counter[str] = Counter()

    # ------------------------------------------------------------ aylanish

    async def run(self) -> None:
        try:
            await self.prepare()
        except Exception:
            log.exception("media_prepare_failed")
        while not self.stop.is_set():
            try:
                handled = await self.run_once()
            except FATAL_ERRORS:
                self.state = "stopped"
                log.error("media_worker_stopped", reason="session yaroqsiz")
                return
            except TRANSIENT_ERRORS as exc:
                self.state = "waiting_network"
                log.warning("media_network_error", error=type(exc).__name__)
                await self._sleep(TRANSIENT_RETRY_S)
                continue
            except Exception:
                self.state = "error"
                log.exception("media_worker_error")
                await self._sleep(PAUSE_S / 10)
                continue
            if self.paused:
                self.state = "paused"
                await self._sleep(PAUSE_S)
            elif handled == 0:
                self.state = "idle"
                await self._sleep(IDLE_S)

    async def prepare(self) -> None:
        """Ishga tushishda: kataloglar, uzilgan yuklashlar, sozlama o'zgarishi, axlat, tekshiruv."""
        ensure_private_dir(self.root)
        ensure_private_dir(self.tmp)
        for leftover in self.tmp.iterdir():
            with suppress(OSError):
                leftover.unlink()
        now = self._clock()
        async with self.db.write() as session:
            await session.execute(
                update(Media)
                .where(Media.status == MediaStatus.DOWNLOADING)
                .values(status=MediaStatus.PENDING, updated_at=now)
            )
            fingerprint = policy_fingerprint(self.settings)
            if await get_meta(session, POLICY_META_KEY) != fingerprint:
                result = await session.execute(
                    update(Media)
                    .where(Media.status == MediaStatus.SKIPPED, Media.skip_reason.in_(sorted(REVIEWABLE_SKIPS)))
                    .values(status=MediaStatus.PENDING, skip_reason=None, status_note=None, updated_at=now)
                )
                await set_meta(session, POLICY_META_KEY, fingerprint)
                if result.rowcount:
                    log.info("media_policy_changed", requeued=result.rowcount)
        await self.empty_trash()
        report = await self.verify()
        if report.missing or report.mismatched:
            log.warning("media_files_missing", missing=report.missing, mismatched=report.mismatched)

    async def run_once(self) -> int:
        """Bitta partiya. Qaytaradi: ko'rib chiqilgan qatorlar soni."""
        ensure_private_dir(self.root)
        ensure_private_dir(self.tmp)
        await self.empty_trash()
        now = self._clock()
        jobs: list[_Job] = []
        scheduled: set[tuple[MediaType, int]] = set()
        async with self.db.write() as session:
            rows = (
                await session.execute(
                    select(Media, Message, Chat)
                    .join(Message, Message.id == Media.message_pk)
                    .join(Chat, Chat.chat_id == Message.chat_id)
                    .where(
                        Media.superseded_at.is_(None),
                        Media.status.in_([MediaStatus.PENDING, MediaStatus.MISSING]),
                        (Media.next_attempt_at.is_(None)) | (Media.next_attempt_at <= now),
                    )
                    .order_by(Message.date.desc(), Media.id.desc())
                    .limit(BATCH)
                )
            ).all()
            for media, message, chat in rows:
                decision = classify(media, message, chat, self.settings)
                twin_key = (media.media_type, media.tg_media_id) if media.tg_media_id is not None else None
                if not decision.download:
                    self._skip(media, decision.reason, decision.note, now)
                elif await self._reuse_existing(session, media, now):
                    pass
                elif twin_key in scheduled:
                    continue  # xuddi shu fayl shu partiyada yuklanyapti — keyingi partiyada qayta ishlatiladi
                else:
                    if twin_key is not None:
                        scheduled.add(twin_key)
                    jobs.append(
                        _Job(
                            media.id,
                            message.chat_id,
                            message.message_id,
                            media.local_path or media_relpath(media, message),
                            media.force_download,
                        )
                    )
            jobs = self._within_disk_budget(jobs, {m.id: m for m, _, _ in rows}, await self._stored_bytes(session))
            for job in jobs:
                row = next(m for m, _, _ in rows if m.id == job.media_pk)
                row.status = MediaStatus.DOWNLOADING
                row.updated_at = now
        if jobs:
            limit = asyncio.Semaphore(self.settings.media_concurrency)

            async def guarded(job: _Job) -> None:
                async with limit:
                    await self._download(job)

            await asyncio.gather(*(guarded(job) for job in jobs))
        return len(rows)

    # -------------------------------------------------------------- tasnif

    def _skip(self, media: Media, reason: MediaSkipReason | None, note: str | None, now: datetime) -> None:
        media.status = MediaStatus.SKIPPED
        media.skip_reason = reason
        media.status_note = (note or "")[:NOTE_LIMIT] or None
        media.next_attempt_at = None
        media.updated_at = now
        self.stats[f"skipped_{reason.value if reason else 'other'}"] += 1

    async def _reuse_existing(self, session: Any, media: Media, now: datetime) -> bool:
        """Xuddi shu Telegram fayli (forward, qayta yuborish) allaqachon diskda bo'lsa — qayta ishlatish."""
        if media.tg_media_id is None:
            return False
        twin = await session.scalar(
            select(Media)
            .where(
                Media.id != media.id,
                Media.tg_media_id == media.tg_media_id,
                Media.media_type == media.media_type,
                Media.status == MediaStatus.DONE,
                Media.local_path.is_not(None),
            )
            .limit(1)
        )
        if twin is None:
            return False
        try:
            path = resolve_media_path(self.root, twin.local_path)
        except ValueError:
            return False
        if not path.is_file() or path.stat().st_size != twin.stored_size:
            return False
        media.status = MediaStatus.DONE
        media.local_path = twin.local_path
        media.stored_size = twin.stored_size
        media.sha256 = twin.sha256
        media.downloaded_at = twin.downloaded_at
        media.verified_at = now
        media.skip_reason = None
        media.status_note = f"media {twin.id} bilan bir xil fayl"
        media.updated_at = now
        self.stats["reused"] += 1
        return True

    # -------------------------------------------------------------- disk

    async def _stored_bytes(self, session: Any) -> int:
        return (await media_summary(session)).stored_bytes

    def _within_disk_budget(self, jobs: list[_Job], media: dict[int, Media], stored: int) -> list[_Job]:
        min_free = int(self.settings.media_min_free_gb * 1024**3)
        free = self._disk_usage(self.root).free
        max_total = (
            int(self.settings.media_max_total_gb * 1024**3) if self.settings.media_max_total_gb else None
        )
        accepted: list[_Job] = []
        planned = 0
        for job in jobs:
            size = media[job.media_pk].expected_size or 0
            if free - planned - size < min_free:
                self.paused = f"diskda joy kam: {format_size(free)} bo'sh, kamida {self.settings.media_min_free_gb} GB qolishi kerak"
                break
            if max_total is not None and stored + planned + size > max_total:
                self.paused = f"MEDIA_MAX_TOTAL_GB to'ldi: {format_size(stored + planned)} / {format_size(max_total)}"
                break
            accepted.append(job)
            planned += size
        else:
            self.paused = None
        if self.paused:
            log.warning("media_paused", reason=self.paused, waiting=len(jobs) - len(accepted))
        return accepted

    # ------------------------------------------------------------ yuklash

    async def _download(self, job: _Job) -> None:
        self.current = {"media_id": job.media_pk, "chat_id": job.chat_id, "message_id": job.message_id}
        self.state = "downloading"
        tmp = self.tmp / f"{job.media_pk}.part"
        limit = None if job.force else self.settings.media_max_file_mb * 1024 * 1024

        def progress(current: int, _total: int) -> None:
            if limit is not None and current > limit:
                raise TooLarge(current)

        try:
            entity = await self.client.get_input_entity(job.chat_id)
            [message] = await self.client.get_messages(entity, ids=[job.message_id])
            if message is None or isinstance(message, types.MessageEmpty) or message.media is None:
                await self._finish(job, MediaStatus.UNAVAILABLE, note="xabar yoki media Telegram'da endi yo'q")
                return
            result = await self.client.download_media(message, file=str(tmp), progress_callback=progress)
            if result is None:
                await self._finish(job, MediaStatus.UNAVAILABLE, note="Telegram faylni bermadi")
                return
            size, digest = await asyncio.to_thread(hash_file, tmp)
            if limit is not None and size > limit:
                raise TooLarge(size)
            final = resolve_media_path(self.root, job.relpath)
            ensure_private_dir(final.parent)
            os.replace(tmp, final)
            await self._finish(job, MediaStatus.DONE, size=size, digest=digest)
            self.stats["downloaded"] += 1
            log.info("media_downloaded", media_id=job.media_pk, chat_id=job.chat_id, size=size)
        except TooLarge as exc:
            await self._finish(
                job,
                MediaStatus.SKIPPED,
                reason=MediaSkipReason.TOO_LARGE,
                note=f"katta fayl: {format_size(exc.size)}+ > {self.settings.media_max_file_mb} MB",
                expected=exc.size,
            )
        except errors.FloodWaitError as exc:
            await self._retry(job, f"FloodWait {exc.seconds} s", delay_s=exc.seconds, count=False)
            await self._sleep(min(exc.seconds, self.settings.max_flood_wait_s))
        except (*FATAL_ERRORS, asyncio.CancelledError):
            raise
        except TRANSIENT_ERRORS as exc:
            await self._retry(job, f"tarmoq: {type(exc).__name__}", delay_s=TRANSIENT_RETRY_S, count=False)
        except (ValueError, *INACCESSIBLE_ERRORS) as exc:
            await self._retry(job, f"chatga kirish yo'q: {type(exc).__name__}")
        except Exception as exc:
            log.warning("media_download_failed", media_id=job.media_pk, error=type(exc).__name__)
            await self._retry(job, f"{type(exc).__name__}: {exc}")
        finally:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            self.current = {}

    async def _finish(
        self,
        job: _Job,
        status: MediaStatus,
        *,
        size: int | None = None,
        digest: str | None = None,
        reason: MediaSkipReason | None = None,
        note: str | None = None,
        expected: int | None = None,
    ) -> None:
        now = self._clock()
        async with self.db.write() as session:
            media = await session.get(Media, job.media_pk)
            if media is None:  # yuklash paytida xabar arxivdan olib tashlangan
                return
            media.status = status
            media.skip_reason = reason
            media.status_note = note[:NOTE_LIMIT] if note else None
            media.next_attempt_at = None
            media.updated_at = now
            if expected is not None:
                media.expected_size = max(media.expected_size or 0, expected)
            if status is MediaStatus.DONE:
                media.local_path = job.relpath
                media.stored_size = size
                media.sha256 = digest
                media.downloaded_at = now
                media.verified_at = now
            elif status is MediaStatus.SKIPPED:
                self.stats[f"skipped_{reason.value if reason else 'other'}"] += 1
            elif status is MediaStatus.UNAVAILABLE:
                self.stats["unavailable"] += 1

    async def _retry(self, job: _Job, error: str, *, delay_s: float | None = None, count: bool = True) -> None:
        now = self._clock()
        async with self.db.write() as session:
            media = await session.get(Media, job.media_pk)
            if media is None:
                return
            if count:
                media.attempts += 1
            media.status_note = error[:NOTE_LIMIT]
            media.updated_at = now
            if count and media.attempts >= self.settings.media_max_attempts:
                media.status = MediaStatus.FAILED
                media.next_attempt_at = None
                self.stats["failed"] += 1
                log.warning("media_failed", media_id=job.media_pk, attempts=media.attempts, error=error)
                return
            if delay_s is None:
                delay_s = RETRY_S[min(max(media.attempts - 1, 0), len(RETRY_S) - 1)]
            media.status = MediaStatus.PENDING
            media.next_attempt_at = now + timedelta(seconds=delay_s)
            self.stats["retried"] += 1

    # --------------------------------------------------- tekshirish va axlat

    async def verify(self, *, checksum: bool = False) -> VerifyReport:
        """Yuklangan fayllar diskda bormi (va ``checksum=True`` da — buzilmaganmi).

        Topilmagan yoki buzilgan fayl ``missing`` bo'ladi — worker uni qayta yuklaydi.
        """
        checked = missing = mismatched = 0
        last_id = 0
        while True:
            async with self.db.read() as session:
                rows = list(
                    await session.scalars(
                        select(Media)
                        .where(Media.status == MediaStatus.DONE, Media.id > last_id)
                        .order_by(Media.id)
                        .limit(VERIFY_CHUNK)
                    )
                )
            if not rows:
                break
            last_id = rows[-1].id
            problems: dict[int, str] = {}
            for media in rows:
                checked += 1
                problem = await self._check_file(media, checksum=checksum)
                if problem is not None:
                    problems[media.id] = problem
                    if problem.startswith("diskda yo'q"):
                        missing += 1
                    else:
                        mismatched += 1
            now = self._clock()
            async with self.db.write() as session:
                for media_id, problem in problems.items():
                    media = await session.get(Media, media_id)
                    if media is not None and media.status is MediaStatus.DONE:
                        media.status = MediaStatus.MISSING
                        media.status_note = problem
                        media.next_attempt_at = None
                        media.updated_at = now
                if not problems and checksum:
                    await session.execute(
                        update(Media).where(Media.id.in_([m.id for m in rows])).values(verified_at=now)
                    )
        return VerifyReport(checked, missing, mismatched)

    async def _check_file(self, media: Media, *, checksum: bool) -> str | None:
        try:
            path = resolve_media_path(self.root, media.local_path or "")
        except ValueError:
            return "diskda yo'q: yo'l noto'g'ri"
        if not path.is_file():
            return "diskda yo'q"
        size = path.stat().st_size
        if media.stored_size is not None and size != media.stored_size:
            return f"hajm mos emas: {size} != {media.stored_size}"
        if checksum:
            _, digest = await asyncio.to_thread(hash_file, path)
            if digest != media.sha256:
                return "checksum mos emas"
        return None

    async def empty_trash(self) -> int:
        """Arxivdan olib tashlangan xabarlarning fayllarini o'chiradi (boshqa xabar ishlatmasa)."""
        removed = 0
        async with self.db.write() as session:
            items = list(await session.scalars(select(MediaTrash).order_by(MediaTrash.id).limit(TRASH_CHUNK)))
            for item in items:
                in_use = await session.scalar(select(Media.id).where(Media.local_path == item.local_path).limit(1))
                if in_use is None:
                    try:
                        resolve_media_path(self.root, item.local_path).unlink(missing_ok=True)
                        removed += 1
                    except ValueError:
                        log.warning("media_trash_bad_path", path=item.local_path)
                await session.delete(item)
        if removed:
            log.info("media_trash_emptied", removed=removed)
        return removed

    def details(self) -> dict[str, Any]:
        return {"state": self.state, "paused": self.paused, "current": self.current, "stats": dict(self.stats)}

    async def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
