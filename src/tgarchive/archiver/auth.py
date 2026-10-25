"""Asosiy akkauntga bir martalik interaktiv kirish va chiqish.

Telefon raqam, kod va 2FA parol hech qayerga yozilmaydi (log, baza, fayl). Kirishdan
keyin Telegram'da faqat session fayli (auth key) qoladi — u akkauntga to'liq kirish
huquqini beradi, shuning uchun 0600 ruxsat bilan saqlanadi.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from telethon import errors
from telethon.tl import types

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
MAX_ATTEMPTS = 3


class LoginError(Exception):
    """Kirish amalga oshmadi. Xabarda maxfiy qiymatlar bo'lmaydi."""


class Prompter(Protocol):
    def ask_phone(self) -> str: ...
    def ask_code(self) -> str: ...
    def ask_password(self) -> str: ...
    def notify(self, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: types.User
    new_session: bool  # shu buyruqda yangi session yaratildimi


def normalize_phone(raw: str) -> str:
    phone = re.sub(r"[\s\-()]", "", raw)
    if not _PHONE_RE.match(phone):
        raise LoginError("telefon raqam formati noto'g'ri (masalan: +998901234567)")
    return phone if phone.startswith("+") else "+" + phone


async def interactive_login(client: Any, prompter: Prompter, *, max_attempts: int = MAX_ATTEMPTS) -> LoginResult:
    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        return LoginResult(await client.get_me(), new_session=False)

    phone = normalize_phone(prompter.ask_phone())
    try:
        sent = await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        raise LoginError("Telegram bu raqamni qabul qilmadi") from None
    except errors.PhoneNumberBannedError:
        raise LoginError("bu raqam Telegram tomonidan bloklangan") from None
    except errors.FloodWaitError as exc:
        raise LoginError(f"juda ko'p urinish: {exc.seconds} soniyadan keyin qayta urinib ko'ring") from None

    prompter.notify("Kod Telegram ilovangizga yuborildi. Uni hech kimga yubormang va forward qilmang.")
    for _ in range(max_attempts):
        code = re.sub(r"\s", "", prompter.ask_code())
        try:
            user = await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.PhoneCodeInvalidError:
            prompter.notify("Kod noto'g'ri, qayta kiriting.")
            continue
        except errors.PhoneCodeExpiredError:
            raise LoginError("kod eskirgan, buyruqni qayta ishga tushiring") from None
        except errors.PhoneNumberUnoccupiedError:
            raise LoginError("bu raqamda Telegram akkaunt yo'q") from None
        except errors.SessionPasswordNeededError:
            user = await _password_step(client, prompter, max_attempts)
        except errors.FloodWaitError as exc:
            raise LoginError(f"juda ko'p urinish: {exc.seconds} soniyadan keyin qayta urinib ko'ring") from None
        return LoginResult(_require_user(user), new_session=True)
    raise LoginError("kod urinishlari soni tugadi")


async def _password_step(client: Any, prompter: Prompter, max_attempts: int) -> Any:
    prompter.notify("Akkauntda ikki bosqichli himoya (2FA) yoqilgan.")
    for _ in range(max_attempts):
        try:
            return await client.sign_in(password=prompter.ask_password())
        except errors.PasswordHashInvalidError:
            prompter.notify("Parol noto'g'ri.")
        except errors.FloodWaitError as exc:
            raise LoginError(f"juda ko'p urinish: {exc.seconds} soniyadan keyin qayta urinib ko'ring") from None
    raise LoginError("parol urinishlari soni tugadi")


def _require_user(user: Any) -> types.User:
    if not isinstance(user, types.User):
        raise LoginError("Telegram kutilmagan javob qaytardi")
    if user.bot:
        raise LoginError("bot akkaunt bilan arxivlab bo'lmaydi — shaxsiy akkaunt kerak")
    return user


async def logout(client: Any) -> bool:
    """Session'ni Telegram serverida tugatadi va lokal faylni o'chiradi."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        return False
    return bool(await client.log_out())
