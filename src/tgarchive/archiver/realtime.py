"""7-bosqich: real vaqt hodisalari — yangi xabar, tahrir, o'chirish.

Telethon handler'lari hech narsani o'zi yozmaydi: hodisani navbatga qo'yadi va darhol
qaytadi (Telethon'ning update aylanishi to'xtab qolmaydi). Bitta iste'molchi navbatni
kelish tartibida qayta ishlaydi.

Handler'lar ``connect()`` dan **oldin** ro'yxatdan o'tadi: ulanish paytida ``catch_up``
olib kelgan hodisalar ham navbatga tushadi, iste'molchi esa akkaunt tekshirilgandan
keyin ishga tushadi.

Qoidalar:

* Faqat arxivlanadigan chatlar (``full`` / ``window``). Bazada yo'q yangi chat birinchi
  xabarida dialoglar qoidalari bilan ro'yxatdan o'tadi.
* Tahrir — ``save_messages`` orqali: eski holat ``message_versions`` ga ko'chadi.
  Kanal oynasida faqat arxivdagi postlarning tahriri qabul qilinadi.
* Yangi kanal posti — oyna oxirgi N ta postga qisqartiriladi.
* O'chirish: kanal hodisasida chat aniq -> ``confirmed``. Shaxsiy chat / oddiy guruh
  hodisasida chat yo'q -> ``probable``, keyin darhol ``get_messages`` bilan tekshiriladi:
  xabar yo'q -> ``confirmed``, hali bor -> belgi bekor qilinadi (``revoked``).
* Servis xabarlari (pin, qo'shilish, qo'ng'iroq) real vaqtda kelmaydi — ular davriy
  sinxron bilan olinadi (Telethon ``NewMessage`` ularni o'tkazib yuboradi).
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from telethon import errors, events
from telethon.tl import types

from tgarchive.archiver.convert import chat_info_from_entity, message_to_record
from tgarchive.archiver.dialogs import PlannedDialog, register_chat
from tgarchive.archiver.history import INACCESSIBLE_ERRORS, HistoryImporter
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import ArchiveMode, DeletionResolution, DeletionSource, MessageSource
from tgarchive.db.models import Chat, Message
from tgarchive.db.store import (
    SaveOutcome,
    add_audit,
    apply_deletions,
    get_message,
    prune_channel_window,
    revoke_deletions,
    save_messages,
)
from tgarchive.db.types import utcnow
from tgarchive.logging_setup import get_logger

log = get_logger(__name__)

QUEUE_SIZE = 10_000
DRAIN_TIMEOUT_S = 10.0
ARCHIVED_MODES = frozenset({ArchiveMode.FULL, ArchiveMode.WINDOW})
_UNKNOWN = object()


@dataclass(frozen=True, slots=True)
class MessageEvent:
    message: Any
    edited: bool = False


@dataclass(frozen=True, slots=True)
class DeletedEvent:
    chat_id: int | None  # None — shaxsiy chat / oddiy guruh (Telegram chatni aytmaydi)
    message_ids: tuple[int, ...]


RealtimeEvent = MessageEvent | DeletedEvent


class RealtimeArchiver:
    def __init__(
        self,
        client: Any,
        db: Database,
        settings: ArchiverSettings,
        importer: HistoryImporter | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.importer = importer
        self._clock = clock
        self.queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=queue_size)
        self.modes: dict[int, ArchiveMode | None] = {}
        self.stats: Counter[str] = Counter()
        self.last_event_at: datetime | None = None
        self._handlers: list[tuple[Callable[[Any], Any], Any]] = []

    # ------------------------------------------------------------ Telethon

    def register(self) -> None:
        self._handlers = [
            (self._on_new, events.NewMessage()),
            (self._on_edit, events.MessageEdited()),
            (self._on_delete, events.MessageDeleted()),
        ]
        for callback, builder in self._handlers:
            self.client.add_event_handler(callback, builder)

    def unregister(self) -> None:
        for callback, builder in self._handlers:
            self.client.remove_event_handler(callback, builder)
        self._handlers = []

    async def _on_new(self, event: Any) -> None:
        await self.queue.put(MessageEvent(event.message, edited=False))

    async def _on_edit(self, event: Any) -> None:
        await self.queue.put(MessageEvent(event.message, edited=True))

    async def _on_delete(self, event: Any) -> None:
        await self.queue.put(DeletedEvent(event.chat_id, tuple(event.deleted_ids or ())))

    # ------------------------------------------------------- siyosat keshi

    async def load_modes(self) -> None:
        async with self.db.read() as session:
            self.modes = dict((await session.execute(select(Chat.chat_id, Chat.archive_mode))).all())

    def update_modes(self, planned: Iterable[PlannedDialog]) -> None:
        for item in planned:
            self.modes[item.dialog.chat.chat_id] = item.policy.mode

    # ----------------------------------------------------------- iste'molchi

    async def run(self) -> None:
        """Navbatni to'xtatilguncha qayta ishlaydi. Bitta hodisadagi xato keyingilarini to'smaydi.

        ``importer`` (akkaunt ID'si bilan) ulanishdan keyin beriladi — undan oldin
        kelgan hodisalar navbatda kutib turadi.
        """
        if self.importer is None:
            raise RuntimeError("importer berilmagan")
        while True:
            item = await self.queue.get()
            try:
                await self.process(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                log.exception("realtime_event_failed", kind=type(item).__name__)
            finally:
                self.queue.task_done()

    async def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> bool:
        """To'xtashdan oldin navbatdagi hodisalarni yozib bo'lishga harakat qiladi."""
        try:
            await asyncio.wait_for(self.queue.join(), timeout)
            return True
        except TimeoutError:
            log.warning("realtime_drain_timeout", left=self.queue.qsize())
            return False

    async def process(self, item: RealtimeEvent) -> None:
        self.last_event_at = self._clock()
        if isinstance(item, DeletedEvent):
            await self._process_deleted(item)
        else:
            await self._process_message(item)

    def details(self) -> dict[str, Any]:
        return {
            "queue": self.queue.qsize(),
            "events": dict(self.stats),
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
        }

    # --------------------------------------------------------- xabar / tahrir

    async def _process_message(self, item: MessageEvent) -> None:
        record = message_to_record(item.message, self_id=self.importer.self_id)
        if record is None:
            return
        mode = await self._mode_for(record.chat_id, item.message)
        if mode not in ARCHIVED_MODES:
            self.stats["ignored"] += 1
            return
        pruned: list[int] = []
        async with self.db.write() as session:
            if mode is ArchiveMode.WINDOW and item.edited:
                if await get_message(session, record.chat_id, record.message_id) is None:
                    self.stats["ignored"] += 1  # oynadan tashqaridagi eski post tahriri
                    return
            [outcome] = await save_messages(session, [record], MessageSource.REALTIME, observed_at=self._clock())
            if mode is ArchiveMode.WINDOW and outcome is SaveOutcome.CREATED:
                pruned = await self._trim_window(session, record.chat_id)
        self.stats["edited" if outcome is SaveOutcome.EDITED else outcome.value] += 1
        log.debug("realtime_message", chat_id=record.chat_id, message_id=record.message_id, outcome=outcome.value)
        if pruned:
            log.info("window_trimmed", chat_id=record.chat_id, pruned=len(pruned))
        try:
            await self.importer.resolve_reply_targets([record])
        except (errors.FloodWaitError, ConnectionError, TimeoutError) as exc:
            log.info("reply_target_deferred", chat_id=record.chat_id, error=type(exc).__name__)

    async def _trim_window(self, session: Any, chat_id: int) -> list[int]:
        keep = self.settings.channel_keep_posts
        keep_from = await session.scalar(
            select(Message.message_id)
            .where(Message.chat_id == chat_id, Message.deletion_status.is_(None))
            .order_by(Message.message_id.desc())
            .offset(keep - 1)
            .limit(1)
        )
        if keep_from is None:
            return []
        pruned = await prune_channel_window(session, chat_id, keep_from)
        if pruned:
            await add_audit(
                session, "window_pruned", target=f"chat:{chat_id}", details={"count": len(pruned), "ids": pruned[:100]}
            )
        return pruned

    async def _mode_for(self, chat_id: int, message: Any) -> ArchiveMode | None:
        mode = self.modes.get(chat_id, _UNKNOWN)
        if mode is not _UNKNOWN:
            return mode  # type: ignore[return-value]
        entity = getattr(message, "chat", None)
        if entity is None:
            try:
                entity = await self.client.get_entity(chat_id)
            except (ValueError, *INACCESSIBLE_ERRORS) as exc:
                log.info("realtime_chat_unresolved", chat_id=chat_id, error=type(exc).__name__)
                return None
        try:
            info = chat_info_from_entity(entity)
        except TypeError:
            return None
        async with self.db.write() as session:
            policy = await register_chat(session, info, self.settings, top_message_id=message.id)
        self.modes[chat_id] = policy.mode
        log.info("realtime_chat_registered", chat_id=chat_id, mode=policy.mode.value, reason=policy.reason)
        return policy.mode

    # --------------------------------------------------------------- o'chirish

    async def _process_deleted(self, item: DeletedEvent) -> None:
        if not item.message_ids:
            return
        if item.chat_id is not None and self.modes.get(item.chat_id, _UNKNOWN) in (_UNKNOWN, ArchiveMode.SKIP):
            self.stats["ignored"] += 1  # arxivlanmaydigan kanal / superguruh
            return
        async with self.db.write() as session:
            deletion_events = await apply_deletions(
                session, item.message_ids, chat_id=item.chat_id, source=DeletionSource.EVENT
            )
            resolved = [(e.resolution, e.resolved_chat_id, e.message_id) for e in deletion_events]
        counts = Counter(resolution.value for resolution, _, _ in resolved)
        self.stats.update({f"deleted_{k}": v for k, v in counts.items()})
        log.info("realtime_deleted", chat_id=item.chat_id, **counts)
        probable: dict[int, list[int]] = defaultdict(list)
        for resolution, chat_id, message_id in resolved:
            if resolution is DeletionResolution.PROBABLE and chat_id is not None:
                probable[chat_id].append(message_id)
        for chat_id, ids in probable.items():
            await self._verify(chat_id, ids)

    async def _verify(self, chat_id: int, message_ids: list[int]) -> None:
        """``probable`` belgisini Telegram'dan tekshiradi."""
        try:
            entity = await self.client.get_input_entity(chat_id)
            found = await self.client.get_messages(entity, ids=message_ids)
        except (ValueError, errors.FloodWaitError, ConnectionError, TimeoutError, *INACCESSIBLE_ERRORS) as exc:
            log.info("deletion_verify_deferred", chat_id=chat_id, error=type(exc).__name__)
            return
        gone = [mid for mid, msg in zip(message_ids, found) if msg is None or isinstance(msg, types.MessageEmpty)]
        alive = [mid for mid, msg in zip(message_ids, found) if mid not in gone]
        async with self.db.write() as session:
            if gone:
                await apply_deletions(session, gone, chat_id=chat_id, source=DeletionSource.VERIFICATION)
            revoked = await revoke_deletions(session, chat_id, alive) if alive else []
        self.stats["verified_confirmed"] += len(gone)
        self.stats["verified_revoked"] += len(revoked)


async def stop_consumer(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
