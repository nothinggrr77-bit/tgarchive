from __future__ import annotations

import pytest
from sqlalchemy import text

from factories import PRIVATE, add_chats, record
from tgarchive.db.engine import Database
from tgarchive.db.enums import MessageSource
from tgarchive.db.fts import (
    APOSTROPHES,
    MAX_TERMS,
    build_match_query,
    match_message_pks,
    normalize_search_text,
    rebuild_index,
    sql_normalize,
)
from tgarchive.db.store import apply_deletions, get_message, save_message


async def _save_texts(db: Database, *texts: str) -> list[int]:
    await add_chats(db, PRIVATE)
    pks = []
    async with db.write() as session:
        for i, body in enumerate(texts, start=1):
            await save_message(session, record(message_id=i, text=body), MessageSource.HISTORY)
            pks.append((await get_message(session, PRIVATE.chat_id, i)).id)
    return pks


async def _search(db: Database, query: str) -> list[int]:
    async with db.read() as session:
        return await match_message_pks(session, query)


def test_all_apostrophe_forms_are_normalized() -> None:
    for ch in APOSTROPHES:
        assert normalize_search_text(f"o{ch}zbek") == "o'zbek"


def test_match_query_quotes_every_word() -> None:
    assert build_match_query("salom dunyo") == '"salom"* "dunyo"*'
    assert build_match_query("kitob", prefix=False) == '"kitob"'
    assert build_match_query('a"b') == '"a""b"*'
    assert build_match_query("NOT x OR y") == '"NOT"* "x"* "OR"* "y"*'


def test_match_query_ignores_empty_and_punctuation() -> None:
    assert build_match_query("   ") is None
    assert build_match_query("!!! --- ***") is None


def test_match_query_limits_terms() -> None:
    query = build_match_query(" ".join(f"soz{i}" for i in range(40)))
    assert query is not None and query.count('"*') == MAX_TERMS


async def test_python_and_sql_normalization_agree(db: Database) -> None:
    sample = "o‘zbek gʻalla o’gʼil `x´"
    async with db.read() as session:
        result = await session.scalar(text(f"SELECT {sql_normalize(':value')}"), {"value": sample})
    assert result == normalize_search_text(sample)


async def test_uzbek_text_found_with_any_apostrophe(db: Database) -> None:
    [pk] = await _save_texts(db, "Oʻzbekiston poytaxti Toshkent")
    assert await _search(db, "o'zbekiston") == [pk]
    assert await _search(db, "o‘zbek") == [pk]  # so'z boshi bo'yicha
    assert await _search(db, "toshk") == [pk]
    assert await _search(db, "samarqand") == []


async def test_prefix_search_handles_suffixes(db: Database) -> None:
    [pk] = await _save_texts(db, "Kitoblarimizni olib keldim")
    assert await _search(db, "kitob") == [pk]


async def test_cyrillic_is_case_insensitive(db: Database) -> None:
    [pk] = await _save_texts(db, "Салом Дунё")
    assert await _search(db, "салом") == [pk]
    assert await _search(db, "ДУНЁ") == [pk]


async def test_all_words_must_match(db: Database) -> None:
    first, second = await _save_texts(db, "qizil olma", "yashil olma")
    assert sorted(await _search(db, "olma")) == sorted([first, second])
    assert await _search(db, "qizil olma") == [first]


@pytest.mark.parametrize(
    "hostile",
    ['"', "NOT", "a OR", "NEAR(", "*", "(", "'; DROP TABLE messages; --", "body:x", "^start", "a AND (b"],
)
async def test_hostile_input_is_safe(db: Database, hostile: str) -> None:
    await _save_texts(db, "oddiy matn")
    await _search(db, hostile)  # xato bermasligi kerak
    async with db.read() as session:
        assert await session.scalar(text("SELECT count(*) FROM messages")) == 1


async def test_index_follows_edits(db: Database) -> None:
    [pk] = await _save_texts(db, "eski matn")
    async with db.write() as session:
        await save_message(
            session, record(message_id=1, text="yangi matn", edit_date=record().date), MessageSource.REALTIME
        )
    assert await _search(db, "eski") == []
    assert await _search(db, "yangi") == [pk]


async def test_deleted_messages_remain_searchable(db: Database) -> None:
    [pk] = await _save_texts(db, "muhim eslatma")
    async with db.write() as session:
        await apply_deletions(session, [1], chat_id=None)
    assert await _search(db, "eslatma") == [pk]


async def test_rebuild_index(db: Database) -> None:
    [pk] = await _save_texts(db, "qayta qurish tekshiruvi")
    async with db.write() as session:
        await rebuild_index(session)
    assert await _search(db, "tekshiruv") == [pk]
