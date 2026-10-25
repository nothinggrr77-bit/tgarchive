"""7-bosqich: real vaqt hodisalari — yangi xabar, tahrir, o'chirish, kanal oynasi."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select
from telethon import events
from telethon.tl import types

from factories import at
from fake_telegram import FakeClock, FakeTelegram, RecordingSleep
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import HistoryImporter
from tgarchive.archiver.realtime import DeletedEvent, MessageEvent, RealtimeArchiver, stop_consumer
from tgarchive.config import ArchiverSettings
from tgarchive.db.engine import Database
from tgarchive.db.enums import (
    ArchiveMode,
    ChatType,
    DeletionResolution,
    DeletionSource,
    DeletionStatus,
    MessageSource,
    SyncStatus,
)
from tgarchive.db.models import AuditLog, Chat, ChatSyncState, DeletionEvent, Message
from tgarchive.db.store import ChatInfo, get_message, get_versions, upsert_chat
from tl_objects import ALI_ID, CHANNEL_ID, CHANNEL_RAW_ID, ME_ID, channel, message, user

VALID_HASH = "0123456789abcdef0123456789abcdef"


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    values: dict[str, Any] = {"history_page_size": 10, "history_page_delay_s": 0, "channel_keep_posts": 5} | overrides
    return ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, **values)


def private(n: int, start: int = 1, user_id: int = ALI_ID, **fields: Any) -> list[Any]:
    return [message(i, peer=types.PeerUser(user_id), **fields) for i in range(start, n + 1)]


def post(i: int, **fields: Any) -> Any:
    return message(i, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, **fields)


async def setup(
    tmp_path: Path, db: Database, tg: FakeTelegram, *, import_history: bool = False, **overrides: Any
) -> RealtimeArchiver:
    cfg = settings(tmp_path, **overrides)
    importer = HistoryImporter(tg, db, cfg, self_id=ME_ID, sleep=RecordingSleep(), clock=FakeClock())
    planned = await sync_dialogs(tg, db, cfg)
    if import_history:
        await importer.run_until_idle()
    rt = RealtimeArchiver(tg, db, cfg, importer)
    rt.update_modes(planned)
    return rt


async def ids(db: Database, chat_id: int) -> list[int]:
    async with db.read() as session:
        return sorted(await session.scalars(select(Message.message_id).where(Message.chat_id == chat_id)))


async def deletion_events(db: Database) -> list[tuple[DeletionResolution, DeletionSource, int]]:
    async with db.read() as session:
        rows = await session.scalars(select(DeletionEvent).order_by(DeletionEvent.id))
        return [(e.resolution, e.source, e.message_id) for e in rows]


# ------------------------------------------------------------- yangi xabarlar


async def test_new_incoming_and_outgoing_messages(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    rt = await setup(tmp_path, db, tg)

    await rt.process(MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="salom")))
    await rt.process(MessageEvent(message(5, peer=types.PeerUser(ALI_ID), out=True, message="javob")))

    async with db.read() as session:
        incoming = await get_message(session, chat, 4)
        outgoing = await get_message(session, chat, 5)
    assert (incoming.source, incoming.sender_id, incoming.is_outgoing) == (MessageSource.REALTIME, ALI_ID, False)
    assert (outgoing.sender_id, outgoing.is_outgoing, outgoing.text) == (ME_ID, True, "javob")
    assert rt.stats["created"] == 2


async def test_duplicate_event_is_unchanged(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    rt = await setup(tmp_path, db, tg, import_history=True)
    await rt.process(MessageEvent(tg.history[chat][3]))
    assert await ids(db, chat) == [1, 2, 3]
    assert rt.stats["unchanged"] == 1


async def test_not_archived_chats_are_ignored(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    bot = tg.add_chat(user(9, bot=True, first_name="Bot"), message(1, peer=types.PeerUser(9)))
    rt = await setup(tmp_path, db, tg)
    async with db.write() as session:  # reply nishoni sifatida olingan, o'zi arxivlanmaydigan chat
        await upsert_chat(session, ChatInfo(chat_id=-1000000000555, chat_type=ChatType.CHANNEL, title="x"))
    await rt.load_modes()

    await rt.process(MessageEvent(message(2, peer=types.PeerUser(9))))
    await rt.process(MessageEvent(message(7, peer=types.PeerChannel(555), post=True)))

    assert await ids(db, bot) == []
    assert await ids(db, -1000000000555) == []
    assert rt.stats["ignored"] == 2


async def test_unknown_chat_is_registered_with_policy(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    rt = await setup(tmp_path, db, tg)
    tg.add_chat(user(8, first_name="Yangi"))
    tg.add_chat(user(9, bot=True, first_name="Bot"))

    await rt.process(MessageEvent(message(40, peer=types.PeerUser(8))))
    await rt.process(MessageEvent(message(41, peer=types.PeerUser(9))))

    async with db.read() as session:
        person, bot = await session.get(Chat, 8), await session.get(Chat, 9)
        state = await session.get(ChatSyncState, 8)
    assert (person.archive_mode, bot.archive_mode) == (ArchiveMode.FULL, ArchiveMode.SKIP)
    assert (state.backfill_status, state.high_water_id) == (SyncStatus.PENDING, 40)
    assert await ids(db, 8) == [40] and await ids(db, 9) == []
    assert rt.modes[8] is ArchiveMode.FULL


async def test_unresolvable_chat_is_ignored(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    rt = await setup(tmp_path, db, tg)
    await rt.process(MessageEvent(message(1, peer=types.PeerUser(77))))
    assert await ids(db, 77) == [] and rt.stats["ignored"] == 1


async def test_cross_chat_reply_target_is_fetched(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(1))
    other = tg.add_chat(channel(555, title="Boshqa"), message(2, peer=types.PeerChannel(555), post=True), dialog=False)
    rt = await setup(tmp_path, db, tg)
    header = types.MessageReplyHeader(reply_to_msg_id=2, reply_to_peer_id=types.PeerChannel(555))

    await rt.process(MessageEvent(message(5, peer=types.PeerUser(ALI_ID), reply_to=header)))

    assert await ids(db, chat) == [5]
    assert await ids(db, other) == [2]


# --------------------------------------------------------------------- tahrir


async def test_edit_keeps_previous_version(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(1))
    rt = await setup(tmp_path, db, tg)

    await rt.process(MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="birinchi")))
    await rt.process(
        MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="ikkinchi", edit_date=at(10)), edited=True)
    )
    # Faqat ko'rishlar soni o'zgargan "tahrir" — yangi versiya emas
    await rt.process(
        MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="ikkinchi", edit_date=at(10), views=5), edited=True)
    )

    async with db.read() as session:
        current = await get_message(session, chat, 4)
        versions = await get_versions(session, current.id)
    assert (current.text, current.edit_count) == ("ikkinchi", 1)
    assert [(v.version_no, v.text) for v in versions] == [(1, "birinchi")]
    assert (rt.stats["edited"], rt.stats["unchanged"]) == (1, 1)


async def test_late_original_does_not_overwrite_edit(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(1))
    rt = await setup(tmp_path, db, tg)

    await rt.process(
        MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="yangi", edit_date=at(10)), edited=True)
    )
    await rt.process(MessageEvent(message(4, peer=types.PeerUser(ALI_ID), message="eski")))

    async with db.read() as session:
        current = await get_message(session, chat, 4)
        assert await get_versions(session, current.id) == []
    assert current.text == "yangi" and rt.stats["stale"] == 1


# ---------------------------------------------------------------- kanal oynasi


async def test_new_posts_roll_the_window_and_keep_reply_targets(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *[post(i) for i in range(1, 13)])
    tg.add_chat(user(), *private(1))
    rt = await setup(tmp_path, db, tg, import_history=True)
    assert await ids(db, ch) == [8, 9, 10, 11, 12]

    header = types.MessageReplyHeader(reply_to_msg_id=8, reply_to_peer_id=types.PeerChannel(CHANNEL_RAW_ID))
    await rt.process(MessageEvent(message(2, peer=types.PeerUser(ALI_ID), reply_to=header)))
    await rt.process(MessageEvent(post(13)))
    await rt.process(MessageEvent(post(14)))

    assert await ids(db, ch) == [8, 10, 11, 12, 13, 14]  # 8 — shaxsiy chatdan reply qilingan
    async with db.read() as session:
        audits = list(await session.scalars(select(AuditLog).where(AuditLog.action == "window_pruned")))
    assert [a.details["ids"] for a in audits] == [[9]]


async def test_window_accepts_edits_only_for_archived_posts(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *[post(i) for i in range(1, 13)])
    rt = await setup(tmp_path, db, tg, import_history=True)

    await rt.process(MessageEvent(post(3, message="eski post tahriri", edit_date=at(30)), edited=True))
    await rt.process(MessageEvent(post(10, message="tahrir", edit_date=at(30)), edited=True))

    assert 3 not in await ids(db, ch)
    async with db.read() as session:
        edited = await get_message(session, ch, 10)
        assert [v.text for v in await get_versions(session, edited.id)] == ["xabar 10"]
    assert (edited.text, rt.stats["ignored"], rt.stats["edited"]) == ("tahrir", 1, 1)


# ------------------------------------------------------------------ o'chirish


async def test_channel_deletion_is_confirmed(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *[post(i) for i in range(1, 13)])
    rt = await setup(tmp_path, db, tg, import_history=True)

    await rt.process(DeletedEvent(CHANNEL_ID, (10,)))

    async with db.read() as session:
        deleted = await get_message(session, ch, 10)
    assert deleted.deletion_status is DeletionStatus.CONFIRMED and deleted.text == "xabar 10"
    assert await deletion_events(db) == [(DeletionResolution.CONFIRMED, DeletionSource.EVENT, 10)]
    assert tg.count("get_messages", ids=[10]) == 0  # kanal hodisasi tekshiruvsiz aniq


async def test_deletions_in_skipped_or_unknown_channels_are_ignored(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(channel(), *[post(i) for i in range(1, 4)])
    rt = await setup(tmp_path, db, tg, archive_channels=False)

    await rt.process(DeletedEvent(CHANNEL_ID, (2,)))
    await rt.process(DeletedEvent(-1000000000777, (1,)))

    assert await deletion_events(db) == []
    assert rt.stats["ignored"] == 2


async def test_private_deletion_is_probable_then_verified(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    rt = await setup(tmp_path, db, tg, import_history=True)
    del tg.history[chat][2]  # Telegram'da haqiqatan o'chirilgan

    await rt.process(DeletedEvent(None, (2,)))

    async with db.read() as session:
        deleted = await get_message(session, chat, 2)
    assert deleted.deletion_status is DeletionStatus.CONFIRMED and deleted.text == "xabar 2"
    assert await deletion_events(db) == [
        (DeletionResolution.PROBABLE, DeletionSource.EVENT, 2),
        (DeletionResolution.CONFIRMED, DeletionSource.VERIFICATION, 2),
    ]
    assert rt.stats["verified_confirmed"] == 1


async def test_private_deletion_revoked_when_message_still_exists(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    rt = await setup(tmp_path, db, tg, import_history=True)

    await rt.process(DeletedEvent(None, (2,)))

    async with db.read() as session:
        kept = await get_message(session, chat, 2)
    assert kept.deletion_status is None and kept.deleted_at is None
    assert await deletion_events(db) == [
        (DeletionResolution.PROBABLE, DeletionSource.EVENT, 2),
        (DeletionResolution.REVOKED, DeletionSource.VERIFICATION, 2),
    ]


async def test_failed_verification_keeps_probable(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    rt = await setup(tmp_path, db, tg, import_history=True)
    tg.fail("get_messages", ConnectionError("tarmoq"), on_call=1)

    await rt.process(DeletedEvent(None, (2,)))

    async with db.read() as session:
        assert (await get_message(session, chat, 2)).deletion_status is DeletionStatus.PROBABLE


async def test_unknown_private_deletion_is_recorded_without_guessing(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    tg.add_chat(user(), *private(3))
    ch = tg.add_chat(channel(), post(999))
    rt = await setup(tmp_path, db, tg, import_history=True)

    # Chatsiz hodisa kanal postiga hech qachon bog'lanmaydi, hatto ID bir xil bo'lsa ham
    await rt.process(DeletedEvent(None, (999,)))

    assert await deletion_events(db) == [(DeletionResolution.NOT_ARCHIVED, DeletionSource.EVENT, 999)]
    async with db.read() as session:
        assert (await get_message(session, ch, 999)).deletion_status is None
    assert tg.count("get_messages", ids=[999]) == 0


# ------------------------------------------------------------ Telethon va navbat


async def test_telethon_handlers_enqueue_events_in_order(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    rt = await setup(tmp_path, db, tg)
    rt.register()
    assert len(tg.handlers) == 3

    msg = message(1)
    await tg.emit(events.NewMessage, events.NewMessage.Event(msg))
    await tg.emit(events.MessageEdited, events.MessageEdited.Event(msg))
    await tg.emit(events.MessageDeleted, events.MessageDeleted.Event(deleted_ids=[1, 2], peer=None))
    await tg.emit(
        events.MessageDeleted, events.MessageDeleted.Event(deleted_ids=[5], peer=types.PeerChannel(CHANNEL_RAW_ID))
    )

    queued = [rt.queue.get_nowait() for _ in range(rt.queue.qsize())]
    assert queued == [
        MessageEvent(msg, edited=False),
        MessageEvent(msg, edited=True),
        DeletedEvent(None, (1, 2)),
        DeletedEvent(CHANNEL_ID, (5,)),
    ]
    rt.unregister()
    assert tg.handlers == []


async def test_consumer_survives_a_bad_event(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(1))
    rt = await setup(tmp_path, db, tg)
    consumer = asyncio.create_task(rt.run())
    try:
        await rt.queue.put(MessageEvent(object()))  # buzilgan hodisa
        await rt.queue.put(MessageEvent(message(2, peer=types.PeerUser(ALI_ID))))
        assert await rt.drain(timeout=5)
    finally:
        await stop_consumer(consumer)
    assert rt.stats["errors"] == 1
    assert await ids(db, chat) == [2]
    assert rt.details()["queue"] == 0 and rt.details()["last_event_at"] is not None
