"""Bazaning o'ziga oid kalit-qiymat ma'lumotlari va akkauntga bog'lash."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tgarchive.db.models import AppMeta

ACCOUNT_ID_KEY = "account_id"


class AccountMismatchError(Exception):
    """Baza boshqa Telegram akkauntiga tegishli."""

    def __init__(self, bound_id: int, actual_id: int) -> None:
        super().__init__(
            f"Bu baza boshqa akkauntga bog'langan (id={bound_id}), hozirgi akkaunt id={actual_id}. "
            "Har bir akkaunt uchun alohida DATA_DIR ishlating."
        )
        self.bound_id = bound_id
        self.actual_id = actual_id


async def get_meta(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppMeta, key)
    return row.value if row else None


async def set_meta(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppMeta, key)
    if row is None:
        session.add(AppMeta(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def bound_account_id(session: AsyncSession) -> int | None:
    value = await get_meta(session, ACCOUNT_ID_KEY)
    return int(value) if value is not None else None


async def bind_account(session: AsyncSession, account_id: int) -> bool:
    """Bazani akkauntga bog'laydi. Yangi bog'langan bo'lsa ``True``.

    Bitta bazada bitta akkaunt: shaxsiy chat xabar ID'lari (common box) faqat
    akkaunt doirasida noyob, o'chirish hodisalarini talqin qilish shunga tayanadi.
    """
    bound = await bound_account_id(session)
    if bound is None:
        await set_meta(session, ACCOUNT_ID_KEY, str(account_id))
        return True
    if bound != account_id:
        raise AccountMismatchError(bound, account_id)
    return False
