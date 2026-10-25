"""SQLite FTS5 to'liq matnli qidiruv.

``messages_fts`` — contentless FTS5 jadvali (matn nusxasi saqlanmaydi, faqat indeks).
U ``messages`` dagi triggerlar orqali avtomatik yangilanadi (migratsiya 0001).

O'zbek tilidagi tutuq belgisi turlicha yoziladi: ``o'``, ``o‘``, ``oʻ``, ``o’``.
Indeks va so'rov bir xil normallashtiriladi — barchasi ASCII ``'`` ga aylanadi.
Python'dagi ``normalize_search_text`` va SQL triggerlardagi ``replace()`` zanjiri
bir xil natija berishi shart; buni tests/test_fts.py tekshiradi.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

FTS_TABLE = "messages_fts"

APOSTROPHES = ("‘", "’", "ʻ", "ʼ", "`", "´")
_TRANSLATION = str.maketrans({ch: "'" for ch in APOSTROPHES})
_HAS_TOKEN = re.compile(r"[^\W_]", re.UNICODE)
MAX_TERMS = 16


def normalize_search_text(value: str) -> str:
    return value.translate(_TRANSLATION)


def sql_normalize(expr: str) -> str:
    """``normalize_search_text`` ning SQL ekvivalenti (trigger va qayta indekslash uchun)."""
    out = expr
    for ch in APOSTROPHES:
        out = f"replace({out}, char({ord(ch)}), '''')"
    return out


def build_match_query(user_query: str, *, prefix: bool = True) -> str | None:
    """Foydalanuvchi matnidan xavfsiz FTS5 MATCH ifodasini yasaydi.

    Har bir so'z qo'shtirnoq ichiga olinadi, shuning uchun ``AND``/``OR``/``NEAR``,
    qavslar va ``*`` operator sifatida emas, oddiy matn sifatida qabul qilinadi.
    ``prefix=True`` da so'z boshi bo'yicha qidiriladi: ``kitob`` -> ``kitoblar``.
    So'zlar orasida mantiqiy AND.
    """
    phrases: list[str] = []
    for word in normalize_search_text(user_query).split():
        if not _HAS_TOKEN.search(word):
            continue
        phrase = '"' + word.replace('"', '""') + '"'
        phrases.append(phrase + "*" if prefix else phrase)
        if len(phrases) >= MAX_TERMS:
            break
    return " ".join(phrases) or None


async def match_message_pks(session: AsyncSession, user_query: str, *, limit: int = 50) -> list[int]:
    """Mos keladigan ``messages.id`` lar, eng yangisi birinchi."""
    match = build_match_query(user_query)
    if match is None:
        return []
    rows = await session.execute(
        text(f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q ORDER BY rowid DESC LIMIT :limit"),
        {"q": match, "limit": limit},
    )
    return [row[0] for row in rows]


async def rebuild_index(session: AsyncSession) -> None:
    """Indeksni ``messages`` dan to'liq qayta quradi (texnik xizmat uchun)."""
    await session.execute(text(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('delete-all')"))
    await session.execute(
        text(
            f"INSERT INTO {FTS_TABLE}(rowid, body) "
            f"SELECT id, {sql_normalize('text')} FROM messages WHERE text IS NOT NULL AND text <> ''"
        )
    )
