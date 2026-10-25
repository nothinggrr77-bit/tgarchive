"""6-bosqich: tarix importi va checkpoint mexanizmi.

Uch turdagi ish (hammasi idempotent, har sahifa o'z checkpoint'i bilan bitta tranzaksiyada):

* **backfill** (to'liq chatlar) — eng yangidan eskiga, ``backfill_cursor_id`` bo'yicha.
  ``done`` faqat Telegram bo'sh sahifa qaytarganda (yoki ``HISTORY_MAX_AGE_DAYS`` chegarasida).
* **forward** (to'liq chatlar) — ``(high_water_id, eng yangi]`` oralig'i eskidan yangiga;
  har sahifadan keyin ``high_water_id`` oshadi, shuning uchun uzilishdan keyin ham davom etadi.
* **window** (kanallar) — oxirgi N ta post, ularning izohlari, oynadan chiqqan postlarni
  olib tashlash, oyna ichida yo'qolgan postlarni o'chirilgan deb tasdiqlash.

Reply qilingan xabar arxivda bo'lmasa (boshqa chatda yoki kanal oynasidan tashqarida),
u ham Telegram'dan olinadi. Bitta tick'da avval yangi xabarlar (forward), keyin kanallar,
keyin backfill bo'laklari (har chatga ``BACKFILL_SLICE_PAGES`` sahifa) — shunda katta
chat importi boshqa chatlardagi yangi xabarlarni soatlab to'sib qo'ymaydi.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from telethon import errors, utils
from telethon.tl import functions, types

from tgarchive.archiver.convert import chat_info_from_entity, message_to_record
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import ArchiveMode, ChatType, DeletionSource, MessageSource, SyncStatus
from tgarchive.db.models import Chat, ChatSyncState, Message
from tgarchive.db.store import (
    MessageRecord,
    SaveOutcome,
    add_audit,
    apply_deletions,
    prune_channel_window,
    save_messages,
    upsert_chat,
)
from tgarchive.db.types import utcnow
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

BACKFILL_SLICE_PAGES = 20
COMMENTS_BATCH = 100
REPLY_TARGET_DEPTH = 3
RETRY_BASE_S = 60
RETRY_MAX_S = 6 * 3600
INACCESSIBLE_RETRY_S = 24 * 3600
ERROR_TEXT_LIMIT = 500

# Chatga kirish yo'q: faqat shu chat keyinga qoldiriladi, qolganlari davom etadi.
INACCESSIBLE_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChatAdminRequiredError,
    errors.ChannelInvalidError,
    errors.ChatIdInvalidError,
    errors.PeerIdInvalidError,
    errors.UserBannedInChannelError,
    errors.ChatForbiddenError,
    errors.ChannelPublicGroupNaError,
)
# Session yaroqsiz: butun xizmat to'xtaydi (xizmat qatlami hal qiladi).
FATAL_ERRORS: tuple[type[Exception], ...] = (errors.UnauthorizedError, errors.AuthKeyDuplicatedError)
# Tarmoq uzildi: chat aybdor emas — xizmat qayta ulanadi.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (ConnectionError, TimeoutError)

_TYPE_PRIORITY = {
    ChatType.SAVED: 0,
    ChatType.PRIVATE: 1,
    ChatType.GROUP: 2,
    ChatType.SUPERGROUP: 3,
    ChatType.BOT: 4,
    ChatType.CHANNEL: 5,
}


class JobKind(StrEnum):
    FORWARD = "forward"
    WINDOW = "window"
    BACKFILL = "backfill"


@dataclass(frozen=True, slots=True)
class Job:
    kind: JobKind
    chat_id: int
    chat_type: ChatType
    title: str | None


class InaccessibleChat(Exception):
    """Telethon chatni topa olmadi (session keshida yo'q yoki kirish yo'q)."""


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class HistoryImporter:
    def __init__(
        self,
        client: Any,
        db: Database,
        settings: ArchiverSettings,
        *,
        self_id: int,
        stop: asyncio.Event | None = None,
        sleep: Sleep | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.self_id = self_id
        self.stop = stop or asyncio.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._clock = clock
        self.progress: dict[str, Any] = {}

    # ------------------------------------------------------------ rejalashtirish

    async def plan(self) -> list[Job]:
        now = self._clock()
        refresh_every = timedelta(seconds=self.settings.channel_refresh_interval_s)
        async with self.db.read() as session:
            rows = (
                await session.execute(
                    select(Chat, ChatSyncState)
                    .join(ChatSyncState, ChatSyncState.chat_id == Chat.chat_id)
                    .where(Chat.archive_mode.in_([ArchiveMode.FULL, ArchiveMode.WINDOW]))
                )
            ).all()
        forward: list[Job] = []
        window: list[Job] = []
        backfill: list[Job] = []
        for chat, state in rows:
            if state.next_retry_at is not None and state.next_retry_at > now:
                continue
            if chat.archive_mode is ArchiveMode.WINDOW:
                stale = state.last_refresh_at is None or now - state.last_refresh_at >= refresh_every
                new_posts = state.remote_top_id is not None and (state.high_water_id or 0) < state.remote_top_id
                if state.backfill_status is not SyncStatus.DONE or stale or new_posts:
                    window.append(Job(JobKind.WINDOW, chat.chat_id, chat.chat_type, chat.title))
                continue
            if (
                state.high_water_id is not None
                and state.remote_top_id is not None
                and state.remote_top_id > state.high_water_id
            ):
                forward.append(Job(JobKind.FORWARD, chat.chat_id, chat.chat_type, chat.title))
            # HISTORY_ENABLED=false: eski tarix olinmaydi, faqat yangi xabarlar (forward) va kanal oynasi
            if self.settings.history_enabled and state.backfill_status is not SyncStatus.DONE:
                backfill.append(Job(JobKind.BACKFILL, chat.chat_id, chat.chat_type, chat.title))

        def order(job: Job) -> tuple[int, int]:
            return (_TYPE_PRIORITY[job.chat_type], job.chat_id)

        return sorted(forward, key=order) + sorted(window, key=order) + sorted(backfill, key=order)

    async def run_tick(self) -> bool:
        """Bitta aylanish. Backfill ishi hali qolgan bo'lsa ``True``."""
        remaining = False
        for job in await self.plan():
            if self.stop.is_set():
                break
            remaining = await self._run_job(job) or remaining
        self.progress = {}
        return remaining

    async def run_until_idle(self) -> None:
        while not self.stop.is_set() and await self.run_tick():
            pass

    # ------------------------------------------------------------- bitta ish

    async def _run_job(self, job: Job) -> bool:
        self.progress = {"job": job.kind.value, "chat_id": job.chat_id, "title": job.title}
        try:
            entity = await self._input_entity(job.chat_id)
            if job.kind is JobKind.FORWARD:
                await self._forward(job, entity)
                return False
            if job.kind is JobKind.WINDOW:
                await self._window(job, entity)
                return False
            return await self._backfill_slice(job, entity)
        except errors.FloodWaitError as exc:
            log.warning("flood_wait", chat_id=job.chat_id, job=job.kind.value, seconds=exc.seconds)
            await self._schedule_retry(job, f"FloodWait {exc.seconds} s", exc.seconds, count_attempt=False)
            # Limit akkaunt bo'yicha: boshqa chatlarga ham shoshilmaymiz.
            await self._sleep(min(exc.seconds, self.settings.max_flood_wait_s))
            return False
        except (InaccessibleChat, *INACCESSIBLE_ERRORS) as exc:
            log.warning("chat_inaccessible", chat_id=job.chat_id, error=type(exc).__name__)
            await self._schedule_retry(job, f"kirish yo'q: {type(exc).__name__}", INACCESSIBLE_RETRY_S)
            return False
        except (*FATAL_ERRORS, *TRANSIENT_ERRORS, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.exception("import_job_failed", chat_id=job.chat_id, job=job.kind.value)
            await self._schedule_retry(job, f"{type(exc).__name__}: {exc}", None)
            return False

    async def _input_entity(self, chat_id: int) -> Any:
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            raise InaccessibleChat(chat_id) from None

    async def _schedule_retry(self, job: Job, error: str, delay_s: float | None, *, count_attempt: bool = True) -> None:
        now = self._clock()
        async with self.db.write() as session:
            state = await session.get(ChatSyncState, job.chat_id)
            if state is None:
                return
            if count_attempt:
                state.attempts += 1
            if delay_s is None:
                delay_s = min(RETRY_BASE_S * 2 ** max(state.attempts - 1, 0), RETRY_MAX_S)
            state.last_error = error[:ERROR_TEXT_LIMIT]
            state.last_error_at = now
            state.next_retry_at = now + timedelta(seconds=delay_s)
            if state.backfill_status is not SyncStatus.DONE:
                state.backfill_status = SyncStatus.FAILED

    @staticmethod
    def _mark_success(state: ChatSyncState) -> None:
        state.attempts = 0
        state.last_error = None
        state.next_retry_at = None

    # -------------------------------------------------------------- backfill

    async def _backfill_slice(self, job: Job, entity: Any) -> bool:
        cursor = await self._ensure_boundary(job, entity)
        cutoff = self._cutoff()
        for _ in range(BACKFILL_SLICE_PAGES):
            if self.stop.is_set():
                return True
            observed = self._clock()
            page = await self.client.get_messages(entity, limit=self.settings.history_page_size, offset_id=cursor)
            if not page:
                await self._finish_backfill(job.chat_id)
                return False
            records = self._convert(page)
            kept = [r for r in records if cutoff is None or r.date >= cutoff]
            reached_cutoff = len(kept) < len(records)
            cursor = min(m.id for m in page)
            async with self.db.write() as session:
                outcomes = await save_messages(session, kept, MessageSource.HISTORY, observed_at=observed)
                state = await session.get(ChatSyncState, job.chat_id)
                state.backfill_cursor_id = cursor
                state.backfill_imported += outcomes.count(SaveOutcome.CREATED)
                self._mark_success(state)
                if reached_cutoff:
                    state.backfill_status = SyncStatus.DONE
                    state.finished_at = self._clock()
            self.progress["cursor"] = cursor
            await self._resolve_reply_targets(kept)
            if reached_cutoff:
                log.info("backfill_done", chat_id=job.chat_id, reason="max_age")
                return False
            await self._sleep(self.settings.history_page_delay_s)
        return True

    async def _ensure_boundary(self, job: Job, entity: Any) -> int:
        """Backfill chegarasini (eng yangi ID) bir marta aniqlaydi. Qaytaradi: keyingi ``offset_id``."""
        async with self.db.read() as session:
            state = await session.get(ChatSyncState, job.chat_id)
            cursor = state.backfill_cursor_id
            running = state.backfill_status is SyncStatus.RUNNING
        if cursor is not None:
            if not running:
                async with self.db.write() as session:
                    (await session.get(ChatSyncState, job.chat_id)).backfill_status = SyncStatus.RUNNING
            return cursor
        top = await self.client.get_messages(entity, limit=1)
        top_id = top[0].id if top else 0
        async with self.db.write() as session:
            state = await session.get(ChatSyncState, job.chat_id)
            state.backfill_boundary_id = top_id
            state.backfill_cursor_id = top_id + 1  # offset_id dan kichiklar olinadi: top ham kiradi
            if state.high_water_id is None:
                state.high_water_id = top_id
            state.backfill_status = SyncStatus.RUNNING
            state.started_at = state.started_at or self._clock()
        log.info("backfill_started", chat_id=job.chat_id, title=job.title, boundary=top_id)
        return top_id + 1

    async def _finish_backfill(self, chat_id: int) -> None:
        async with self.db.write() as session:
            state = await session.get(ChatSyncState, chat_id)
            state.backfill_status = SyncStatus.DONE
            state.finished_at = self._clock()
            self._mark_success(state)
            imported = state.backfill_imported
        log.info("backfill_done", chat_id=chat_id, imported=imported)

    def _cutoff(self) -> datetime | None:
        days = self.settings.history_max_age_days
        return self._clock() - timedelta(days=days) if days else None

    # --------------------------------------------------------------- forward

    async def _forward(self, job: Job, entity: Any) -> None:
        async with self.db.read() as session:
            high_water = (await session.get(ChatSyncState, job.chat_id)).high_water_id or 0
        page_size = self.settings.history_page_size
        while not self.stop.is_set():
            observed = self._clock()
            # reverse=True: eskidan yangiga, min_id — pastki chegara (shu ID kirmaydi)
            page = await self.client.get_messages(entity, limit=page_size, min_id=high_water, reverse=True)
            if not page:
                break
            records = self._convert(page)
            high_water = max(m.id for m in page)
            async with self.db.write() as session:
                await save_messages(session, records, MessageSource.GAPFILL, observed_at=observed)
                state = await session.get(ChatSyncState, job.chat_id)
                state.high_water_id = max(state.high_water_id or 0, high_water)
                state.last_refresh_at = self._clock()
                self._mark_success(state)
            await self._resolve_reply_targets(records)
            if len(page) < page_size:
                break
            await self._sleep(self.settings.history_page_delay_s)
        async with self.db.write() as session:
            state = await session.get(ChatSyncState, job.chat_id)
            state.last_refresh_at = self._clock()
            self._mark_success(state)

    # ---------------------------------------------------------------- window

    async def _window(self, job: Job, entity: Any) -> None:
        keep = self.settings.channel_keep_posts
        observed = self._clock()
        posts = [m for m in await self.client.get_messages(entity, limit=keep) if m is not None]
        records = self._convert(posts)
        pruned: list[int] = []
        deleted: list[int] = []
        async with self.db.write() as session:
            outcomes = await save_messages(session, records, MessageSource.HISTORY, observed_at=observed)
            state = await session.get(ChatSyncState, job.chat_id)
            if posts:
                top_id = max(m.id for m in posts)
                keep_from = min(m.id for m in posts)
                deleted = await self._missing_in_window(session, job.chat_id, keep_from, top_id, {m.id for m in posts})
                if deleted:
                    await apply_deletions(session, deleted, chat_id=job.chat_id, source=DeletionSource.VERIFICATION)
                pruned = await prune_channel_window(session, job.chat_id, keep_from)
                if pruned:
                    await add_audit(
                        session, "window_pruned", target=f"chat:{job.chat_id}", details={"count": len(pruned), "ids": pruned[:100]}
                    )
                state.backfill_boundary_id = top_id
                state.backfill_cursor_id = keep_from
                state.high_water_id = max(state.high_water_id or 0, top_id)
            state.backfill_imported += outcomes.count(SaveOutcome.CREATED)
            if state.backfill_status is not SyncStatus.DONE:
                state.backfill_status = SyncStatus.DONE
                state.started_at = state.started_at or observed
                state.finished_at = self._clock()
            state.last_refresh_at = self._clock()
            self._mark_success(state)
        log.info("window_refreshed", chat_id=job.chat_id, posts=len(posts), pruned=len(pruned), deleted=len(deleted))
        await self._resolve_reply_targets(records)
        if self.settings.channel_save_comments:
            await self._sync_comments(job, entity, posts)

    @staticmethod
    async def _missing_in_window(session: Any, chat_id: int, low: int, high: int, returned: set[int]) -> list[int]:
        """Oyna oralig'ida arxivda bor, lekin Telegram qaytarmagan postlar — demak o'chirilgan.

        ``getHistory`` eng yangi N ta mavjud xabarni qaytaradi: ``[low, high]`` oralig'idagi
        har bir mavjud post albatta natijada bo'ladi.
        """
        rows = await session.scalars(
            select(Message.message_id).where(
                Message.chat_id == chat_id,
                Message.message_id >= low,
                Message.message_id <= high,
                Message.deletion_status.is_(None),
            )
        )
        return sorted(set(rows) - returned)

    # -------------------------------------------------------------- comments

    async def _sync_comments(self, job: Job, entity: Any, posts: Sequence[Any]) -> None:
        for post in posts:
            if self.stop.is_set():
                return
            replies = getattr(post, "replies", None)
            if not replies or not replies.comments or not replies.max_id:
                continue
            async with self.db.read() as session:
                row = await session.scalar(
                    select(Message).where(Message.chat_id == job.chat_id, Message.message_id == post.id)
                )
            if row is None:
                continue
            synced = row.comments_synced_id
            if synced is not None and synced >= replies.max_id:
                continue
            try:
                if synced is None:
                    await self._save_discussion_root(job.chat_id, entity, post.id)
                await self._fetch_comments(entity, post.id, min_id=synced or 0)
            except errors.MsgIdInvalidError:
                continue
            except INACCESSIBLE_ERRORS as exc:
                log.info("comments_unavailable", chat_id=job.chat_id, post_id=post.id, error=type(exc).__name__)
                continue
            async with self.db.write() as session:
                (await session.get(Message, row.id)).comments_synced_id = replies.max_id

    async def _save_discussion_root(self, channel_id: int, entity: Any, post_id: int) -> None:
        """Muhokama guruhidagi post nusxasi — izohlar unga reply qiladi."""
        result = await self.client(functions.messages.GetDiscussionMessageRequest(peer=entity, msg_id=post_id))
        entities = {utils.get_peer_id(chat): chat for chat in result.chats}
        records = await self._save_with_chats(result.messages, MessageSource.HISTORY, entities=entities)
        if records:
            async with self.db.write() as session:
                channel = await session.get(Chat, channel_id)
                if channel is not None:
                    channel.linked_chat_id = records[0].chat_id

    async def _fetch_comments(self, entity: Any, post_id: int, *, min_id: int) -> None:
        batch: list[Any] = []
        saved: list[MessageRecord] = []
        async for msg in self.client.iter_messages(
            entity, reply_to=post_id, min_id=min_id, wait_time=self.settings.history_page_delay_s
        ):
            batch.append(msg)
            if len(batch) >= COMMENTS_BATCH:
                saved += await self._save_with_chats(batch, MessageSource.HISTORY)
                batch = []
        if batch:
            saved += await self._save_with_chats(batch, MessageSource.HISTORY)
        # Butun mavzu saqlangandan keyin: izoh boshqa izohga javob bo'lsa, u allaqachon arxivda.
        await self._resolve_reply_targets(saved)

    # --------------------------------------------------------- reply targets

    async def _resolve_reply_targets(self, records: Iterable[MessageRecord], depth: int = 0) -> None:
        """Reply qilingan xabar arxivda bo'lmasa — uni ham oladi.

        To'liq arxivlanadigan chat ichidagi reply'lar o'tkazib yuboriladi: ularni backfill
        o'zi olib keladi. Boshqa chatdagi nishonlar va kanal oynasidan tashqaridagi
        postlar alohida so'raladi. Zanjir ``REPLY_TARGET_DEPTH`` qadamgacha kuzatiladi.
        """
        records = [r for r in records if r.reply_to_msg_id is not None]
        if depth >= REPLY_TARGET_DEPTH or not records:
            return
        async with self.db.read() as session:
            modes = dict(
                (
                    await session.execute(
                        select(Chat.chat_id, Chat.archive_mode).where(
                            Chat.chat_id.in_(sorted({r.chat_id for r in records}))
                        )
                    )
                ).all()
            )
            wanted: dict[int, set[int]] = defaultdict(set)
            for record in records:
                if record.reply_to_peer_id is None and modes.get(record.chat_id) is ArchiveMode.FULL:
                    continue
                wanted[record.reply_to_peer_id or record.chat_id].add(record.reply_to_msg_id)
            for chat_id, ids in wanted.items():
                existing = await session.scalars(
                    select(Message.message_id).where(Message.chat_id == chat_id, Message.message_id.in_(sorted(ids)))
                )
                ids.difference_update(existing)

        fetched: list[MessageRecord] = []
        for chat_id, ids in wanted.items():
            if not ids:
                continue
            try:
                entity = await self._input_entity(chat_id)
                messages = await self.client.get_messages(entity, ids=sorted(ids))
            except (InaccessibleChat, errors.MsgIdInvalidError, *INACCESSIBLE_ERRORS) as exc:
                log.info("reply_target_unavailable", chat_id=chat_id, error=type(exc).__name__)
                continue
            found = [m for m in messages if m is not None and not isinstance(m, types.MessageEmpty)]
            fetched += await self._save_with_chats(found, MessageSource.HISTORY)
        await self._resolve_reply_targets(fetched, depth + 1)

    # --------------------------------------------------------------- helpers

    def _convert(self, messages: Iterable[Any]) -> list[MessageRecord]:
        records = []
        for msg in messages:
            record = message_to_record(msg, self_id=self.self_id)
            if record is not None:
                records.append(record)
        return records

    async def _save_with_chats(
        self, messages: Sequence[Any], source: MessageSource, *, entities: dict[int, Any] | None = None
    ) -> list[MessageRecord]:
        """Chati hali bazada bo'lmagan xabarlarni saqlash (izohlar guruhi, boshqa chatdagi nishon)."""
        records = self._convert(messages)
        if not records:
            return []
        entities = dict(entities or {})
        for msg in messages:
            chat = getattr(msg, "chat", None)
            if chat is not None:
                entities.setdefault(utils.get_peer_id(chat), chat)
        chat_ids = {r.chat_id for r in records}
        async with self.db.read() as session:
            known = set(await session.scalars(select(Chat.chat_id).where(Chat.chat_id.in_(sorted(chat_ids)))))
        infos = []
        for chat_id in chat_ids - known:
            entity = entities.get(chat_id)
            if entity is None:
                try:
                    entity = await self.client.get_entity(chat_id)
                except (ValueError, *INACCESSIBLE_ERRORS) as exc:
                    log.info("chat_unresolved", chat_id=chat_id, error=type(exc).__name__)
                    records = [r for r in records if r.chat_id != chat_id]
                    continue
            infos.append(chat_info_from_entity(entity))
        if not records:
            return []
        async with self.db.write() as session:
            for info in infos:
                await upsert_chat(session, info)
            await save_messages(session, records, source, observed_at=self._clock())
        return records

    async def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
