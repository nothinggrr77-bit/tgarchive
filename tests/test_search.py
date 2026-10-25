"""Qidiruv sintaksisi, filtrlar, ro'yxatlar va statistika (db/search.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from archive_fixture import Archive, build_archive
from tgarchive.db.engine import Database
from tgarchive.db.enums import MediaType
from tgarchive.db.search import (
    SearchQuery,
    archive_stats,
    chat_detail,
    list_chats,
    message_detail,
    parse_search,
    search_messages,
)
from tl_objects import ALI_ID, CHANNEL_ID

TASHKENT = ZoneInfo("Asia/Tashkent")


# ------------------------------------------------------------------ sintaksis


def test_parse_words_and_filters() -> None:
    query, errors = parse_search(
        "shartnoma imzo chat:-1001234 from:7 type:photo has:reply is:edited is:deleted name:hisobot", TASHKENT
    )
    assert errors == []
    assert query == SearchQuery(
        text="shartnoma imzo",
        chat_id=-1001234,
        sender_id=7,
        media_type=MediaType.PHOTO,
        has_reply=True,
        edited=True,
        deleted=True,
        file_name="hisobot",
    )


def test_dates_are_local_days_converted_to_utc() -> None:
    query, errors = parse_search("after:2026-01-01 before:2026-02-01", TASHKENT)
    assert errors == []
    assert query.date_from == datetime(2025, 12, 31, 19, 0, tzinfo=UTC)  # Toshkent UTC+5
    assert query.date_to == datetime(2026, 1, 31, 19, 0, tzinfo=UTC)
    day, _ = parse_search("on:2026-03-08", TASHKENT)
    assert (day.date_to - day.date_from).days == 1


def test_from_me_and_text_with_colons() -> None:
    query, _ = parse_search("from:me https://example.com vaqt:12:30 has:media", TASHKENT)
    assert query.outgoing is True and query.has_media is True
    assert query.text == "https://example.com vaqt:12:30"  # noma'lum kalit — oddiy matn


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("chat:abc", "son bo'lishi kerak"),
        ("from:x1", "son bo'lishi kerak"),
        ("after:2026-13-01", "YYYY-MM-DD"),
        ("type:kartinka", "noma'lum tur"),
        ("has:link", "has:media yoki has:reply"),
        ("is:pinned", "is:edited yoki is:deleted"),
        ("after:2026-02-01 before:2026-01-01", "sana oralig'i bo'sh"),
    ],
)
def test_parse_errors(raw: str, fragment: str) -> None:
    _, errors = parse_search(raw, TASHKENT)
    assert len(errors) == 1 and fragment in errors[0]


def test_describe() -> None:
    assert SearchQuery().describe() == "barcha xabarlar" and SearchQuery().is_empty
    assert parse_search("salom is:deleted", TASHKENT)[0].describe() == "«salom» is:deleted"


# --------------------------------------------------------------------- filtrlar


@pytest.fixture
async def archive(db: Database, tmp_path: Path) -> Archive:
    return await build_archive(tmp_path, db)


async def find(db: Database, query: SearchQuery, **kwargs) -> list[int]:
    async with db.read() as session:
        page = await search_messages(session, query, limit=kwargs.pop("limit", 50), **kwargs)
    return [row.message_id for row in page.rows if row.chat_id == ALI_ID]


async def test_text_search_uses_current_version_only(db: Database, archive: Archive) -> None:
    assert await find(db, SearchQuery(text="salom")) == [5, 4, 3]  # eng yangisi birinchi
    assert await find(db, SearchQuery(text="eski")) == []  # eski versiya qidiruvda emas
    assert await find(db, SearchQuery(text="xavfli")) == [3]
    assert await find(db, SearchQuery(text="!!!")) == []


async def test_filters(db: Database, archive: Archive) -> None:
    assert await find(db, SearchQuery(outgoing=True)) == [8]
    assert await find(db, SearchQuery(has_reply=True)) == [4]
    assert await find(db, SearchQuery(edited=True)) == [5]
    assert await find(db, SearchQuery(deleted=True)) == [6]
    assert await find(db, SearchQuery(media_type=MediaType.PHOTO)) == [1]
    assert await find(db, SearchQuery(has_media=True)) == [2, 1]
    assert await find(db, SearchQuery(file_name="KINO")) == [2]  # katta-kichik harf farqsiz
    assert await find(db, SearchQuery(file_name="%")) == []  # LIKE belgilari escape qilinadi
    assert await find(db, SearchQuery(sender_id=ALI_ID, text="salom")) == [5, 4, 3]
    async with db.read() as session:
        channel_rows = (await search_messages(session, SearchQuery(chat_id=CHANNEL_ID))).rows
    assert [r.message_id for r in channel_rows] == [2, 1]


async def test_date_filters(db: Database, archive: Archive) -> None:
    async with db.read() as session:
        all_rows = (await search_messages(session, SearchQuery(chat_id=ALI_ID), limit=50)).rows
    pivot = next(r.date for r in all_rows if r.message_id == 4)
    assert await find(db, SearchQuery(chat_id=ALI_ID, date_from=pivot)) == [8, 7, 6, 5, 4]
    assert await find(db, SearchQuery(chat_id=ALI_ID, date_to=pivot)) == [3, 2, 1]


async def test_pagination(db: Database, archive: Archive) -> None:
    async with db.read() as session:
        first = await search_messages(session, SearchQuery(text="salom"), limit=2)
        second = await search_messages(session, SearchQuery(text="salom"), limit=2, offset=2)
    assert [r.message_id for r in first.rows] == [5, 4] and first.has_next
    assert [r.message_id for r in second.rows] == [3] and not second.has_next


# --------------------------------------------------------- chatlar va xabar


async def test_chat_list_and_detail(db: Database, archive: Archive) -> None:
    async with db.read() as session:
        page = await list_chats(session)
        detail = await chat_detail(session, ALI_ID)
        assert await chat_detail(session, 12345) is None
    assert {(r.chat_id, r.message_count) for r in page.rows} == {(ALI_ID, 8), (CHANNEL_ID, 2)}
    assert (detail.message_count, detail.deleted_count, detail.edited_count, detail.media_done) == (8, 1, 1, 1)


async def test_message_detail(db: Database, archive: Archive) -> None:
    async with db.read() as session:
        reply = await message_detail(session, archive.pk[4])
        original = await message_detail(session, archive.pk[3])
        edited = await message_detail(session, archive.pk[5])
        photo = await message_detail(session, archive.pk[1])
        assert await message_detail(session, 999_999) is None
    assert reply.parent.message_id == 3 and original.reply_count == 1
    assert edited.version_count == 1
    assert photo.media is not None and photo.media.media_type is MediaType.PHOTO


async def test_archive_stats(db: Database, archive: Archive) -> None:
    async with db.read() as session:
        stats = await archive_stats(session)
    assert (stats.total_messages, stats.edited_messages, stats.versions) == (10, 1, 1)
    assert (stats.deleted_confirmed, stats.deleted_probable) == (1, 0)
    assert stats.chats_by_mode == {"full": 1, "window": 1}
    assert stats.top_chats[0].chat_id == ALI_ID
