"""6-bosqich: tarix importi, checkpoint, oldinga sinxronlash, kanal oynasi, izohlar."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from telethon import errors
from telethon.tl import types

from fake_telegram import FakeClock, FakeTelegram, RecordingSleep
from factories import at
from tgarchive.archiver.dialogs import sync_dialogs
from tgarchive.archiver.history import (
    BACKFILL_SLICE_PAGES,
    INACCESSIBLE_RETRY_S,
    RETRY_BASE_S,
    HistoryImporter,
    JobKind,
)
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
from tgarchive.db.store import get_message, get_reply_chain
from tl_objects import ALI_ID, CHANNEL_ID, CHANNEL_RAW_ID, ME_ID, channel, message, user

VALID_HASH = "0123456789abcdef0123456789abcdef"
DISC_RAW = 999000
DISC_ID = -1000000999000


def settings(tmp_path: Path, **overrides: Any) -> ArchiverSettings:
    values: dict[str, Any] = {
        "history_page_size": 10,
        "history_page_delay_s": 0,
        "channel_keep_posts": 5,
    } | overrides
    return ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, data_dir=tmp_path, **values)


def make_importer(tg: FakeTelegram, db: Database, cfg: ArchiverSettings, **kwargs: Any) -> HistoryImporter:
    kwargs.setdefault("sleep", RecordingSleep())
    kwargs.setdefault("clock", FakeClock())
    return HistoryImporter(tg, db, cfg, self_id=ME_ID, **kwargs)


def private(n: int, start: int = 1, user_id: int = ALI_ID) -> list[Any]:
    return [message(i, peer=types.PeerUser(user_id), message=f"xabar {i}") for i in range(start, n + 1)]


def posts(n: int, start: int = 1, **fields: Any) -> list[Any]:
    return [message(i, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, **fields) for i in range(start, n + 1)]


async def state(db: Database, chat_id: int) -> ChatSyncState:
    async with db.read() as session:
        row = await session.get(ChatSyncState, chat_id)
    assert row is not None
    return row


async def ids(db: Database, chat_id: int) -> list[int]:
    async with db.read() as session:
        return sorted(await session.scalars(select(Message.message_id).where(Message.chat_id == chat_id)))


# --------------------------------------------------------------------- backfill


async def test_full_backfill_in_pages(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(25))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)

    await make_importer(tg, db, cfg).run_until_idle()

    st = await state(db, chat)
    assert st.backfill_status is SyncStatus.DONE
    assert (st.backfill_boundary_id, st.backfill_cursor_id, st.high_water_id) == (25, 1, 25)
    assert st.backfill_imported == 25
    assert await ids(db, chat) == list(range(1, 26))
    assert tg.count("get_messages") == 5  # eng yangi ID + 3 sahifa + bo'sh sahifa


async def test_backfill_resumes_after_failure_and_is_not_done_meanwhile(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(25))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    tg.fail("get_messages", RuntimeError("uzilish"), on_call=3)  # 1: eng yangi ID, 2: 1-sahifa, 3: 2-sahifa
    clock = FakeClock()
    importer = make_importer(tg, db, cfg, clock=clock)

    assert await importer.run_tick() is False
    st = await state(db, chat)
    assert st.backfill_status is SyncStatus.FAILED
    assert (st.backfill_cursor_id, st.backfill_imported, st.attempts) == (16, 10, 1)
    assert "RuntimeError: uzilish" in st.last_error
    assert st.next_retry_at > clock.now
    assert await ids(db, chat) == list(range(16, 26))
    assert await importer.plan() == []  # qayta urinish vaqti hali kelmagan

    clock.advance(RETRY_BASE_S + 1)
    await importer.run_until_idle()
    st = await state(db, chat)
    assert st.backfill_status is SyncStatus.DONE
    assert (st.backfill_imported, st.attempts, st.last_error) == (25, 0, None)
    assert await ids(db, chat) == list(range(1, 26))


async def test_large_chat_is_imported_in_slices(db: Database, tmp_path: Path) -> None:
    total = BACKFILL_SLICE_PAGES * 10 + 50
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(total))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)

    assert await importer.run_tick() is True  # bitta tick'da faqat bir bo'lak
    assert (await state(db, chat)).backfill_cursor_id == total - BACKFILL_SLICE_PAGES * 10 + 1
    assert await importer.run_tick() is False
    assert len(await ids(db, chat)) == total


async def test_history_max_age_cutoff(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(25))
    cfg = settings(tmp_path, history_max_age_days=1)
    await sync_dialogs(tg, db, cfg)
    clock = FakeClock(at(10) + timedelta(days=1))  # chegara = at(10): 10..25-xabarlar qoladi

    await make_importer(tg, db, cfg, clock=clock).run_until_idle()

    assert await ids(db, chat) == list(range(10, 26))
    assert (await state(db, chat)).backfill_status is SyncStatus.DONE


async def test_empty_chat_is_done(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user())
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    await make_importer(tg, db, cfg).run_until_idle()
    st = await state(db, chat)
    assert (st.backfill_status, st.backfill_boundary_id) == (SyncStatus.DONE, 0)


# ------------------------------------------------------------- oldinga sinxron


async def test_new_messages_are_caught_by_forward_sync(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(25))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    tg.add(chat, *private(30, start=26))
    await sync_dialogs(tg, db, cfg)
    assert [job.kind for job in await importer.plan()] == [JobKind.FORWARD]
    await importer.run_until_idle()

    assert await ids(db, chat) == list(range(1, 31))
    assert (await state(db, chat)).high_water_id == 30
    assert tg.count("get_messages", min_id=25, reverse=True) == 1
    async with db.read() as session:
        assert (await get_message(session, chat, 30)).source is MessageSource.GAPFILL
    assert await importer.plan() == []


async def test_forward_sync_resumes_from_high_water(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(5))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    clock = FakeClock()
    importer = make_importer(tg, db, cfg, clock=clock)
    await importer.run_until_idle()

    tg.add(chat, *private(30, start=6))
    await sync_dialogs(tg, db, cfg)
    tg.fail("get_messages", RuntimeError("uzilish"), on_call=2)
    await importer.run_tick()
    st = await state(db, chat)
    assert st.high_water_id == 15  # birinchi sahifa saqlangan
    assert st.backfill_status is SyncStatus.DONE  # backfill holati buzilmaydi

    clock.advance(RETRY_BASE_S + 1)
    await importer.run_until_idle()
    assert await ids(db, chat) == list(range(1, 31))


# ------------------------------------------------------------------ xatolar


async def test_flood_wait_pauses_and_retries_without_counting_attempt(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(25))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    tg.fail("get_messages", errors.FloodWaitError(request=None, capture=120), on_call=2)
    clock, sleep = FakeClock(), RecordingSleep()
    importer = make_importer(tg, db, cfg, clock=clock, sleep=sleep)

    await importer.run_tick()
    st = await state(db, chat)
    assert st.last_error == "FloodWait 120 s" and st.attempts == 0
    assert (st.next_retry_at - clock.now).total_seconds() == 120
    assert 120 in sleep.calls

    clock.advance(121)
    await importer.run_until_idle()
    assert (await state(db, chat)).backfill_status is SyncStatus.DONE


async def test_inaccessible_chat_does_not_block_others(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    broken = tg.add_chat(user(7, first_name="A"), *private(3, user_id=7))
    fine = tg.add_chat(user(8, first_name="B"), *private(3, user_id=8))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    tg.fail("get_messages", errors.ChannelPrivateError(request=None), on_call=1)  # 7 birinchi navbatda
    clock = FakeClock()

    await make_importer(tg, db, cfg, clock=clock).run_until_idle()

    st = await state(db, broken)
    assert st.backfill_status is SyncStatus.FAILED
    assert st.last_error == "kirish yo'q: ChannelPrivateError"
    assert (st.next_retry_at - clock.now).total_seconds() == INACCESSIBLE_RETRY_S
    assert (await state(db, fine)).backfill_status is SyncStatus.DONE


async def test_unknown_entity_is_inaccessible(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    tg.hidden.add(chat)
    await make_importer(tg, db, cfg).run_until_idle()
    assert (await state(db, chat)).last_error == "kirish yo'q: InaccessibleChat"


@pytest.mark.parametrize(
    "exc",
    [errors.AuthKeyUnregisteredError(request=None), ConnectionError("tarmoq yo'q")],
    ids=["session-revoked", "network"],
)
async def test_fatal_and_transient_errors_reach_the_service(db: Database, tmp_path: Path, exc: Exception) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(3))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    tg.fail("get_messages", exc, on_call=1)
    with pytest.raises(type(exc)):
        await make_importer(tg, db, cfg).run_tick()
    assert (await state(db, chat)).last_error is None  # chat aybdor deb belgilanmaydi


# ---------------------------------------------------------------- kanal oynasi


async def test_channel_keeps_only_latest_posts_and_rolls_the_window(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *posts(12))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    assert await ids(db, ch) == [8, 9, 10, 11, 12]
    st = await state(db, ch)
    assert (st.backfill_status, st.high_water_id) == (SyncStatus.DONE, 12)
    assert tg.count("get_messages", limit=5) == 1  # butun tarix o'qilmaydi

    tg.add(ch, *posts(14, start=13))
    await sync_dialogs(tg, db, cfg)
    assert [job.kind for job in await importer.plan()] == [JobKind.WINDOW]
    await importer.run_until_idle()

    assert await ids(db, ch) == [10, 11, 12, 13, 14]
    async with db.read() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "window_pruned"))
    assert audit.details == {"count": 2, "ids": [8, 9]}


async def test_window_keeps_reply_targets(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *posts(12))
    cross_reply = types.MessageReplyHeader(reply_to_msg_id=9, reply_to_peer_id=types.PeerChannel(CHANNEL_RAW_ID))
    tg.add_chat(user(), message(1, peer=types.PeerUser(ALI_ID), reply_to=cross_reply))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    new_posts = posts(16, start=13)
    new_posts[1] = message(14, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True,
                           reply_to=types.MessageReplyHeader(reply_to_msg_id=2))
    tg.add(ch, *new_posts)
    await sync_dialogs(tg, db, cfg)
    await importer.run_until_idle()
    await sync_dialogs(tg, db, cfg)
    importer._clock.advance(cfg.channel_refresh_interval_s + 1)
    await importer.run_until_idle()

    # 9 — shaxsiy chatdan reply qilingan; 2 — oynadagi 14-post reply qilgan (alohida olib kelingan)
    assert await ids(db, ch) == [2, 9, 12, 13, 14, 15, 16]


async def test_window_confirms_deleted_posts(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    ch = tg.add_chat(channel(), *posts(12))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    del tg.history[ch][10]
    importer._clock.advance(cfg.channel_refresh_interval_s + 1)
    await importer.run_until_idle()

    async with db.read() as session:
        deleted = await get_message(session, ch, 10)
        event = await session.scalar(select(DeletionEvent))
    assert deleted.deletion_status is DeletionStatus.CONFIRMED
    assert deleted.text == "xabar 10"  # nusxa saqlanadi
    assert (event.resolution, event.source) == (DeletionResolution.CONFIRMED, DeletionSource.VERIFICATION)
    assert 7 in await ids(db, ch)  # oyna bitta pastga surildi


async def test_comments_are_saved_with_discussion_root(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    replies = types.MessageReplies(replies=3, replies_pts=0, comments=True, channel_id=DISC_RAW, max_id=103)
    channel_posts = posts(11) + [message(12, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, replies=replies)]
    ch = tg.add_chat(channel(), *channel_posts)
    disc = channel(DISC_RAW, broadcast=False, title="Muhokama")
    tg.add_chat(disc, dialog=False)
    in_disc = types.PeerChannel(DISC_RAW)
    root = message(100, peer=in_disc, from_id=types.PeerChannel(CHANNEL_RAW_ID),
                   fwd_from=types.MessageFwdHeader(date=at(), from_id=types.PeerChannel(CHANNEL_RAW_ID), channel_post=12))
    comment = lambda i, to, top=None: message(  # noqa: E731
        i, peer=in_disc, from_id=types.PeerUser(ALI_ID),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=to, reply_to_top_id=top),
    )
    tg.roots[(ch, 12)] = (root, disc)
    tg.comments[(ch, 12)] = [comment(101, 100), comment(102, 101, 100), comment(103, 100)]
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    assert await ids(db, DISC_ID) == [100, 101, 102, 103]
    async with db.read() as session:
        disc_row = await session.get(Chat, DISC_ID)
        channel_row = await session.get(Chat, CHANNEL_ID)
        post = await get_message(session, ch, 12)
        chain = await get_reply_chain(session, await get_message(session, DISC_ID, 102))
    assert (disc_row.chat_type, disc_row.archive_mode) == (ChatType.SUPERGROUP, None)
    assert channel_row.linked_chat_id == DISC_ID
    assert post.comments_synced_id == 103
    assert [m.message_id for m in chain] == [101, 100]
    assert tg.count("get_messages", ids=[101]) == 0  # izohlar ichidagi reply alohida so'ralmaydi

    # Yangi izoh: faqat 103 dan keyingilar olinadi, ildiz qayta so'ralmaydi
    tg.comments[(ch, 12)].append(comment(104, 100))
    tg.history[ch][12].replies.max_id = 104
    importer._clock.advance(cfg.channel_refresh_interval_s + 1)
    await importer.run_until_idle()
    assert await ids(db, DISC_ID) == [100, 101, 102, 103, 104]
    assert tg.count("iter_messages", min_id=103) == 1
    assert tg.count("request") == 1


# --------------------------------------------------------------- reply nishoni


async def test_cross_chat_reply_target_is_fetched_from_unjoined_chat(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    other = channel(555, title="Boshqa kanal")
    other_id = tg.add_chat(other, message(2, peer=types.PeerChannel(555), post=True, message="asl post"), dialog=False)
    header = types.MessageReplyHeader(reply_to_msg_id=2, reply_to_peer_id=types.PeerChannel(555))
    chat = tg.add_chat(user(), message(1, peer=types.PeerUser(ALI_ID), reply_to=header))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    await make_importer(tg, db, cfg).run_until_idle()

    async with db.read() as session:
        target_chat = await session.get(Chat, other_id)
        reply = await get_message(session, chat, 1)
        chain = await get_reply_chain(session, reply)
    assert target_chat is not None and target_chat.archive_mode is None
    assert [m.text for m in chain] == ["asl post"]


async def test_unavailable_reply_target_is_skipped(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    header = types.MessageReplyHeader(reply_to_msg_id=2, reply_to_peer_id=types.PeerChannel(777))
    chat = tg.add_chat(user(), message(1, peer=types.PeerUser(ALI_ID), reply_to=header))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    await make_importer(tg, db, cfg).run_until_idle()
    assert await ids(db, chat) == [1]
    assert (await state(db, chat)).backfill_status is SyncStatus.DONE


# -------------------------------------------------------------- rejalashtirish


async def test_plan_order_new_messages_first(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    done = tg.add_chat(user(7, first_name="A"), *private(3, user_id=7))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    tg.add(done, *private(5, start=4, user_id=7))
    tg.add_chat(user(8, first_name="B"), *private(3, user_id=8))
    tg.add_chat(channel(), *posts(3))
    await sync_dialogs(tg, db, cfg)

    plan = await importer.plan()
    assert [(job.kind, job.chat_type) for job in plan] == [
        (JobKind.FORWARD, ChatType.PRIVATE),
        (JobKind.WINDOW, ChatType.CHANNEL),
        (JobKind.BACKFILL, ChatType.PRIVATE),
    ]


async def test_sync_dialogs_manages_states(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    person = tg.add_chat(user(), *private(4))
    bot = tg.add_chat(user(9, bot=True, first_name="Bot"), message(1, peer=types.PeerUser(9)))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)

    st = await state(db, person)
    assert (st.backfill_status, st.high_water_id, st.remote_top_id) == (SyncStatus.PENDING, 4, 4)
    async with db.read() as session:
        assert await session.get(ChatSyncState, bot) is None
        assert (await session.get(Chat, bot)).archive_mode is ArchiveMode.SKIP

    await sync_dialogs(tg, db, settings(tmp_path, archive_private=False))
    assert (await state(db, person)).backfill_status is SyncStatus.DISABLED
    await sync_dialogs(tg, db, cfg)
    assert (await state(db, person)).backfill_status is SyncStatus.PENDING


async def test_history_disabled_archives_only_new_messages_and_channel_window(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(10))
    ch = tg.add_chat(channel(), *posts(12))
    cfg = settings(tmp_path, history_enabled=False)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    assert await ids(db, chat) == []  # eski tarix olinmadi
    assert await ids(db, ch) == [8, 9, 10, 11, 12]  # kanal oynasi baribir ishlaydi

    tg.add(chat, *private(12, start=11))
    await sync_dialogs(tg, db, cfg)
    await importer.run_until_idle()
    assert await ids(db, chat) == [11, 12]
    assert (await state(db, chat)).backfill_status is SyncStatus.PENDING


async def test_deleted_top_message_does_not_trigger_forward_forever(db: Database, tmp_path: Path) -> None:
    tg = FakeTelegram()
    chat = tg.add_chat(user(), *private(5))
    cfg = settings(tmp_path)
    await sync_dialogs(tg, db, cfg)
    importer = make_importer(tg, db, cfg)
    await importer.run_until_idle()

    tg.add(chat, *private(7, start=6))
    await sync_dialogs(tg, db, cfg)  # dialoglarda eng yangi = 7
    del tg.history[chat][6], tg.history[chat][7]  # forward'dan oldin o'chirildi
    assert [job.kind for job in await importer.plan()] == [JobKind.FORWARD]
    await importer.run_until_idle()

    assert (await state(db, chat)).high_water_id == 7
    assert await importer.plan() == []
