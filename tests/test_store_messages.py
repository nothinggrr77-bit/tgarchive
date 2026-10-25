from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from factories import CHANNEL, GROUP, PRIVATE, add_chats, at, record
from tgarchive.db.engine import Database
from tgarchive.db.enums import MediaStatus, MediaType, MessageSource
from tgarchive.db.models import Media, Message, MessageVersion
from tgarchive.db.store import (
    MediaInfo,
    SaveOutcome,
    get_message,
    get_replies,
    get_reply_chain,
    get_reply_parent,
    get_versions,
    save_message,
    save_messages,
)


async def _count(db: Database, model: type) -> int:
    async with db.read() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _save(db: Database, rec, source: MessageSource = MessageSource.REALTIME) -> SaveOutcome:
    async with db.write() as session:
        return await save_message(session, rec, source)


async def _get(db: Database, chat_id: int, message_id: int) -> Message:
    async with db.read() as session:
        message = await get_message(session, chat_id, message_id)
    assert message is not None
    return message


# ------------------------------------------------------------------ yozish


async def test_new_message_is_saved(db: Database) -> None:
    await add_chats(db, PRIVATE)
    rec = record(text="Salom", sender_id=7, sender_name="Ali", is_outgoing=False, grouped_id=99)
    assert await _save(db, rec, MessageSource.HISTORY) is SaveOutcome.CREATED

    message = await _get(db, PRIVATE.chat_id, 1)
    assert message.text == "Salom"
    assert message.sender_id == 7 and message.sender_name == "Ali"
    assert message.date == at(1) and message.date.tzinfo is not None
    assert message.source is MessageSource.HISTORY
    assert message.edit_count == 0 and message.deletion_status is None
    assert message.grouped_id == 99


async def test_empty_text_is_stored_as_null(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(text="", media=MediaInfo(MediaType.PHOTO, tg_media_id=1)))
    assert (await _get(db, PRIVATE.chat_id, 1)).text is None


async def test_forward_metadata_is_kept(db: Database) -> None:
    await add_chats(db, PRIVATE)
    rec = record(fwd_from_id=CHANNEL.chat_id, fwd_date=at(-60), fwd_channel_post_id=321, fwd_from_name=None)
    await _save(db, rec)
    message = await _get(db, PRIVATE.chat_id, 1)
    assert (message.fwd_from_id, message.fwd_channel_post_id, message.fwd_date) == (CHANNEL.chat_id, 321, at(-60))


async def test_message_requires_known_chat(db: Database) -> None:
    with pytest.raises(IntegrityError):
        await _save(db, record(chat_id=999))


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        record(date=datetime(2026, 1, 1))


# -------------------------------------------------------------- dublikatlar


async def test_same_message_twice_is_not_duplicated(db: Database) -> None:
    await add_chats(db, PRIVATE)
    assert await _save(db, record(), MessageSource.HISTORY) is SaveOutcome.CREATED
    assert await _save(db, record(), MessageSource.REALTIME) is SaveOutcome.UNCHANGED
    assert await _count(db, Message) == 1
    assert (await _get(db, PRIVATE.chat_id, 1)).source is MessageSource.HISTORY  # birinchi manba saqlanadi


async def test_duplicates_inside_one_batch(db: Database) -> None:
    await add_chats(db, PRIVATE)
    async with db.write() as session:
        outcomes = await save_messages(session, [record(), record(), record(message_id=2)], MessageSource.HISTORY)
    assert outcomes == [SaveOutcome.CREATED, SaveOutcome.UNCHANGED, SaveOutcome.CREATED]
    assert await _count(db, Message) == 2


async def test_same_message_id_in_different_chats_are_different_messages(db: Database) -> None:
    await add_chats(db, PRIVATE, CHANNEL)
    await _save(db, record(chat_id=PRIVATE.chat_id, message_id=5))
    await _save(db, record(chat_id=CHANNEL.chat_id, message_id=5))
    assert await _count(db, Message) == 2


async def test_sender_name_is_filled_when_learned_later(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(sender_id=7))
    assert await _save(db, record(sender_id=7, sender_name="Ali")) is SaveOutcome.UNCHANGED
    assert (await _get(db, PRIVATE.chat_id, 1)).sender_name == "Ali"


# ------------------------------------------------------------- tahrirlar


async def test_edits_keep_every_previous_version(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(text="birinchi"))
    assert await _save(db, record(text="ikkinchi", edit_date=at(5))) is SaveOutcome.EDITED
    assert await _save(db, record(text="uchinchi", edit_date=at(10))) is SaveOutcome.EDITED

    message = await _get(db, PRIVATE.chat_id, 1)
    assert message.text == "uchinchi"
    assert message.edit_count == 2
    assert message.edit_date == at(10)

    async with db.read() as session:
        versions = await get_versions(session, message.id)
    assert [(v.version_no, v.text, v.edit_date) for v in versions] == [
        (1, "birinchi", None),
        (2, "ikkinchi", at(5)),
    ]
    assert versions[0].observed_at == message.archived_at
    assert versions[1].observed_at == versions[0].replaced_at


async def test_stale_snapshot_does_not_overwrite_newer_edit(db: Database) -> None:
    """Tahrirdan oldin olingan tarix sahifasi tahrirdan keyin yozilsa — e'tiborsiz qoldiriladi."""
    await add_chats(db, PRIVATE)
    await _save(db, record(text="asl"))
    await _save(db, record(text="tahrirlangan", edit_date=at(5)))
    assert await _save(db, record(text="asl"), MessageSource.HISTORY) is SaveOutcome.STALE

    message = await _get(db, PRIVATE.chat_id, 1)
    assert message.text == "tahrirlangan"
    assert await _count(db, MessageVersion) == 1


async def test_non_content_update_is_not_an_edit(db: Database) -> None:
    """Reaksiya yoki ko'rishlar soni o'zgarishi: mazmun bir xil — versiya yaratilmaydi."""
    await add_chats(db, PRIVATE)
    await _save(db, record(text="matn"))
    assert await _save(db, record(text="matn")) is SaveOutcome.UNCHANGED
    assert await _count(db, MessageVersion) == 0


async def test_entities_change_is_an_edit(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(text="havola"))
    entities = [{"type": "text_url", "offset": 0, "length": 6, "url": "https://example.org"}]
    assert await _save(db, record(text="havola", entities=entities, edit_date=at(2))) is SaveOutcome.EDITED
    assert (await _get(db, PRIVATE.chat_id, 1)).entities == entities


# ------------------------------------------------------------------ media


async def test_file_media_creates_queue_row(db: Database) -> None:
    await add_chats(db, PRIVATE)
    media = MediaInfo(MediaType.VOICE, tg_media_id=10, mime_type="audio/ogg", size=2048, duration=3.5)
    await _save(db, record(text=None, media=media))
    async with db.read() as session:
        row = await session.scalar(select(Media))
    assert row is not None
    assert (row.media_type, row.tg_media_id, row.status) == (MediaType.VOICE, 10, MediaStatus.PENDING)
    assert (row.expected_size, row.duration, row.local_path) == (2048, 3.5, None)


async def test_non_file_media_has_no_media_row(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(text="https://example.org", media=MediaInfo(MediaType.WEBPAGE)))
    assert (await _get(db, PRIVATE.chat_id, 1)).media_type is MediaType.WEBPAGE
    assert await _count(db, Media) == 0


async def test_replaced_media_keeps_old_row(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(media=MediaInfo(MediaType.PHOTO, tg_media_id=10)))
    edited = record(media=MediaInfo(MediaType.PHOTO, tg_media_id=11), edit_date=at(3))
    assert await _save(db, edited) is SaveOutcome.EDITED
    assert await _save(db, edited) is SaveOutcome.UNCHANGED

    async with db.read() as session:
        rows = list(await session.scalars(select(Media).order_by(Media.id)))
    assert [(r.tg_media_id, r.superseded_at is None) for r in rows] == [(10, False), (11, True)]


# ------------------------------------------------------------ reply zanjiri


async def test_reply_chain_and_replies(db: Database) -> None:
    await add_chats(db, GROUP)
    chat = GROUP.chat_id
    async with db.write() as session:
        await save_messages(
            session,
            [
                record(chat, 1, text="savol"),
                record(chat, 2, text="javob", reply_to_msg_id=1),
                record(chat, 3, text="javobga javob", reply_to_msg_id=2),
                record(chat, 4, text="yana javob", reply_to_msg_id=1),
            ],
            MessageSource.HISTORY,
        )
    async with db.read() as session:
        m1, m2, m3 = [await get_message(session, chat, i) for i in (1, 2, 3)]
        assert (await get_reply_parent(session, m2)).id == m1.id
        assert [m.message_id for m in await get_reply_chain(session, m3)] == [2, 1]
        assert [m.message_id for m in await get_replies(session, m1)] == [2, 4]
        assert await get_reply_parent(session, m1) is None


async def test_reply_to_missing_message(db: Database) -> None:
    """Reply qilingan xabar arxivda yo'q (hali import qilinmagan yoki o'chirilgan)."""
    await add_chats(db, PRIVATE)
    await _save(db, record(message_id=5, reply_to_msg_id=999))
    async with db.read() as session:
        message = await get_message(session, PRIVATE.chat_id, 5)
        assert message.reply_to_msg_id == 999
        assert await get_reply_parent(session, message) is None
        assert await get_reply_chain(session, message) == []


async def test_reply_resolves_when_parent_arrives_later(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(message_id=2, reply_to_msg_id=1))  # tarix yangidan eskiga import qilinadi
    await _save(db, record(message_id=1), MessageSource.HISTORY)
    async with db.read() as session:
        child = await get_message(session, PRIVATE.chat_id, 2)
        assert (await get_reply_parent(session, child)).message_id == 1


async def test_cross_chat_reply(db: Database) -> None:
    await add_chats(db, GROUP, CHANNEL)
    await _save(db, record(CHANNEL.chat_id, 50, text="post"))
    await _save(db, record(GROUP.chat_id, 7, reply_to_msg_id=50, reply_to_peer_id=CHANNEL.chat_id))
    async with db.read() as session:
        reply = await get_message(session, GROUP.chat_id, 7)
        parent = await get_reply_parent(session, reply)
    assert parent is not None and parent.chat_id == CHANNEL.chat_id


async def test_reply_peer_equal_to_own_chat_is_normalized(db: Database) -> None:
    await add_chats(db, GROUP)
    await _save(db, record(GROUP.chat_id, 1))
    await _save(db, record(GROUP.chat_id, 2, reply_to_msg_id=1, reply_to_peer_id=GROUP.chat_id))
    async with db.read() as session:
        parent = await get_message(session, GROUP.chat_id, 1)
        replies = await get_replies(session, parent)
    assert [m.message_id for m in replies] == [2]
    assert replies[0].reply_to_peer_id is None
