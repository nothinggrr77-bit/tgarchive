"""Umumiy SQLAlchemy turlari."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, Dialect, Enum, Integer
from sqlalchemy.types import TypeDecorator

# SQLite'da INTEGER PRIMARY KEY rowid bilan bir xil (autoincrement), PostgreSQL'da BIGINT.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Vaqtni har doim UTC'da saqlaydi va timezone-aware qaytaradi.

    SQLite timezone saqlamaydi, shuning uchun bazaga naive UTC yoziladi. Naive
    (timezone'siz) qiymat xato hisoblanadi — u qaysi mintaqada ekanini bilib bo'lmaydi.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime qabul qilinmaydi: UTC timezone bilan bering")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    """Enum'ni VARCHAR(32) sifatida, qiymati (nomi emas) bilan saqlaydi.

    CHECK constraint yaratilmaydi: SQLite'da yangi qiymat qo'shish butun jadvalni
    qayta yaratishni talab qiladi. Qiymatlar Python tomonida tekshiriladi.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
