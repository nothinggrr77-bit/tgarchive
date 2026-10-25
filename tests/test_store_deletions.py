from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from factories import CHANNEL, GROUP, PRIVATE, PRIVATE_2, add_chats, record
from tgarchive.db.engine import Database
from tgarchive.db.enums import DeletionResolution, DeletionSource, DeletionStatus, MessageSource
from tgarchive.db.models import DeletionEvent, Message
from tgarchive.db.store import apply_deletions, get_message, save_message
from tgarchive.db.types import utcnow


async def _save(db: Database, rec) -> None:
    async with db.write() as session:
        await save_message(session, rec, MessageSource.HISTORY)


async def _delete(db: Database, ids: list[int], chat_id: int | None, source=DeletionSource.EVENT):
    async with db.write() as session:
        return [e.resolution for e in await apply_deletions(session, ids, chat_id=chat_id, source=source)]


async def _get(db: Database, chat_id: int, message_id: int) -> Message:
    async with db.read() as session:
        message = await get_message(session, chat_id, message_id)
    assert message is not None
    return message


async def test_channel_deletion_is_confirmed_and_row_is_kept(db: Database) -> None:
    await add_chats(db, CHANNEL)
    await _save(db, record(CHANNEL.chat_id, 1, text="o'chiriladigan post"))
    await _save(db, record(CHANNEL.chat_id, 2))

    assert await _delete(db, [1], chat_id=CHANNEL.chat_id) == [DeletionResolution.CONFIRMED]

    deleted = await _get(db, CHANNEL.chat_id, 1)
    assert deleted.deletion_status is DeletionStatus.CONFIRMED
    assert deleted.deleted_at is not None
    assert deleted.text == "o'chiriladigan post"  # nusxa saqlanib qoladi
    assert (await _get(db, CHANNEL.chat_id, 2)).deletion_status is None


async def test_common_box_event_with_unique_match_is_probable(db: Database) -> None:
    """Shaxsiy chatdagi o'chirish: Telegram chatni aytmaydi, faqat ID beradi."""
    await add_chats(db, PRIVATE, CHANNEL)
    await _save(db, record(PRIVATE.chat_id, 50))
    await _save(db, record(CHANNEL.chat_id, 50))  # kanal ID ketma-ketligi alohida

    assert await _delete(db, [50], chat_id=None) == [DeletionResolution.PROBABLE]
    assert (await _get(db, PRIVATE.chat_id, 50)).deletion_status is DeletionStatus.PROBABLE
    assert (await _get(db, CHANNEL.chat_id, 50)).deletion_status is None


async def test_common_box_covers_basic_groups(db: Database) -> None:
    await add_chats(db, GROUP)
    await _save(db, record(GROUP.chat_id, 60))
    assert await _delete(db, [60], chat_id=None) == [DeletionResolution.PROBABLE]


async def test_ambiguous_match_marks_nothing(db: Database) -> None:
    """Bir nechta nomzod bo'lsa, taxmin qilinmaydi."""
    await add_chats(db, PRIVATE, PRIVATE_2)
    await _save(db, record(PRIVATE.chat_id, 70))
    await _save(db, record(PRIVATE_2.chat_id, 70))

    assert await _delete(db, [70], chat_id=None) == [DeletionResolution.AMBIGUOUS]
    assert (await _get(db, PRIVATE.chat_id, 70)).deletion_status is None
    assert (await _get(db, PRIVATE_2.chat_id, 70)).deletion_status is None
    async with db.read() as session:
        event = await session.scalar(select(DeletionEvent))
    assert event.message_pk is None and event.note == "2 ta nomzod"


async def test_deletion_of_unarchived_message_is_only_logged(db: Database) -> None:
    await add_chats(db, CHANNEL)
    assert await _delete(db, [12345], chat_id=CHANNEL.chat_id) == [DeletionResolution.NOT_ARCHIVED]
    async with db.read() as session:
        event = await session.scalar(select(DeletionEvent))
    assert (event.message_id, event.chat_id, event.message_pk) == (12345, CHANNEL.chat_id, None)


async def test_verification_upgrades_probable_to_confirmed(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(PRIVATE.chat_id, 80))
    await _delete(db, [80], chat_id=None)
    first_detected = (await _get(db, PRIVATE.chat_id, 80)).deleted_at

    result = await _delete(db, [80], chat_id=PRIVATE.chat_id, source=DeletionSource.VERIFICATION)
    assert result == [DeletionResolution.CONFIRMED]
    message = await _get(db, PRIVATE.chat_id, 80)
    assert message.deletion_status is DeletionStatus.CONFIRMED
    assert message.deleted_at == first_detected


async def test_reappearing_message_revokes_deletion_mark(db: Database) -> None:
    await add_chats(db, PRIVATE)
    await _save(db, record(PRIVATE.chat_id, 90))
    await _delete(db, [90], chat_id=None)

    async with db.write() as session:
        await save_message(
            session, record(PRIVATE.chat_id, 90), MessageSource.GAPFILL, observed_at=utcnow() + timedelta(seconds=1)
        )

    message = await _get(db, PRIVATE.chat_id, 90)
    assert message.deletion_status is None and message.deleted_at is None
    async with db.read() as session:
        resolutions = list(await session.scalars(select(DeletionEvent.resolution).order_by(DeletionEvent.id)))
    assert resolutions == [DeletionResolution.PROBABLE, DeletionResolution.REVOKED]


async def test_snapshot_older_than_deletion_does_not_revoke(db: Database) -> None:
    """O'chirishdan OLDIN olingan sahifa keyinroq yozilsa, belgi saqlanadi."""
    await add_chats(db, PRIVATE)
    await _save(db, record(PRIVATE.chat_id, 91))
    await _delete(db, [91], chat_id=None)

    async with db.write() as session:
        await save_message(
            session, record(PRIVATE.chat_id, 91), MessageSource.HISTORY, observed_at=datetime(2000, 1, 1, tzinfo=UTC)
        )
    assert (await _get(db, PRIVATE.chat_id, 91)).deletion_status is DeletionStatus.PROBABLE


async def test_each_deleted_id_is_audited(db: Database) -> None:
    await add_chats(db, CHANNEL)
    for i in (1, 2):
        await _save(db, record(CHANNEL.chat_id, i))
    result = await _delete(db, [3, 1, 2, 1], chat_id=CHANNEL.chat_id)
    assert result == [DeletionResolution.CONFIRMED, DeletionResolution.CONFIRMED, DeletionResolution.NOT_ARCHIVED]
    async with db.read() as session:
        assert await session.scalar(select(func.count()).select_from(DeletionEvent)) == 3
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
