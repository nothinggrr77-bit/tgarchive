"""Bazaga ulanish, SQLite sozlamalari va tranzaksiya qoidalari.

Concurrency modeli (SQLite WAL):

* Bir vaqtning o'zida ko'p o'quvchi va bitta yozuvchi bo'lishi mumkin.
* ``write()`` tranzaksiyani ``BEGIN IMMEDIATE`` bilan ochadi: yozish qulfi boshida olinadi,
  shuning uchun "o'qib, keyin yozish" paytida ``SQLITE_BUSY_SNAPSHOT`` xatosi bo'lmaydi.
  Boshqa jarayon (masalan, bot) yozayotgan bo'lsa, ``busy_timeout`` gacha kutiladi.
* Bitta jarayon ichida yozuvchilar ``asyncio.Lock`` bilan navbatga qo'yiladi.
* ``read()`` oddiy (DEFERRED) tranzaksiya — yozuvchini to'smaydi.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_BEGIN_MODES = frozenset({"DEFERRED", "IMMEDIATE", "EXCLUSIVE"})
_BEGIN_OPTION = "sqlite_begin"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _install_sqlite_hooks(engine: AsyncEngine, busy_timeout_ms: int) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        # Drayverning o'z BEGIN'ini o'chiramiz — tranzaksiyani _on_begin boshqaradi.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        mode = conn.get_execution_options().get(_BEGIN_OPTION, "DEFERRED")
        if mode not in _BEGIN_MODES:
            raise ValueError(f"noto'g'ri BEGIN rejimi: {mode}")
        conn.exec_driver_sql(f"BEGIN {mode}")


class Database:
    def __init__(self, url: str | URL, *, busy_timeout_ms: int = 10_000, echo: bool = False) -> None:
        self.url = make_url(url)
        self.is_sqlite = self.url.get_backend_name() == "sqlite"
        self.engine = create_async_engine(self.url, echo=echo, json_serializer=_json_dumps)
        if self.is_sqlite:
            _install_sqlite_hooks(self.engine, busy_timeout_ms)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        """Faqat o'qish uchun sessiya. Chiqishda tranzaksiya bekor qilinadi."""
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """Yozish uchun sessiya: muvaffaqiyatli bo'lsa commit, xato bo'lsa rollback."""
        async with self._write_lock, self._sessions() as session:
            if self.is_sqlite:
                await session.connection(execution_options={_BEGIN_OPTION: "IMMEDIATE"})
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def ping(self) -> None:
        async with self.read() as session:
            await session.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
