"""Interaktiv kirish oqimi: soxta client bilan, Telegram'ga ulanmasdan."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import pytest
from telethon import errors
from telethon.tl import types

from tgarchive.archiver.auth import LoginError, interactive_login, logout, normalize_phone

PHONE, CODE, PASSWORD = "+998901234567", "12345", "juda-maxfiy-parol"
ME = types.User(id=111, first_name="Men")


class FakeClient:
    def __init__(
        self,
        *,
        authorized: bool = False,
        needs_password: bool = False,
        send_error: Exception | None = None,
        user: types.User = ME,
    ) -> None:
        self.authorized = authorized
        self.needs_password = needs_password
        self.send_error = send_error
        self.user = user
        self.connected = False
        self.sign_in_calls = 0
        self.logged_out = False

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> types.User:
        return self.user

    async def send_code_request(self, phone: str) -> types.auth.SentCode:
        if self.send_error:
            raise self.send_error
        assert phone == PHONE
        return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="hash")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):  # noqa: ANN001
        self.sign_in_calls += 1
        if password is not None:
            if password != PASSWORD:
                raise errors.PasswordHashInvalidError(request=None)
        else:
            assert phone_code_hash == "hash"
            if code != CODE:
                raise errors.PhoneCodeInvalidError(request=None)
            if self.needs_password:
                raise errors.SessionPasswordNeededError(request=None)
        self.authorized = True
        return self.user

    async def log_out(self) -> bool:
        self.logged_out = True
        self.authorized = False
        return True


class ScriptedPrompter:
    def __init__(self, phone: str = PHONE, codes: Iterable[str] = (CODE,), passwords: Iterable[str] = ()) -> None:
        self.phone = phone
        self.codes = list(codes)
        self.passwords = list(passwords)
        self.notes: list[str] = []

    def ask_phone(self) -> str:
        return self.phone

    def ask_code(self) -> str:
        return self.codes.pop(0)

    def ask_password(self) -> str:
        return self.passwords.pop(0)

    def notify(self, text: str) -> None:
        self.notes.append(text)


def test_normalize_phone() -> None:
    assert normalize_phone("+998 (90) 123-45-67") == PHONE
    assert normalize_phone("998901234567") == PHONE
    with pytest.raises(LoginError):
        normalize_phone("salom")


async def test_already_authorized_session_is_reused() -> None:
    client = FakeClient(authorized=True)
    result = await interactive_login(client, ScriptedPrompter(codes=[]))
    assert result.user.id == ME.id and result.new_session is False
    assert client.sign_in_calls == 0


async def test_login_with_code() -> None:
    result = await interactive_login(FakeClient(), ScriptedPrompter())
    assert result.new_session is True and result.user.id == ME.id


async def test_wrong_code_then_correct() -> None:
    prompter = ScriptedPrompter(codes=["00000", " 123 45 "])
    result = await interactive_login(FakeClient(), prompter)
    assert result.new_session
    assert "Kod noto'g'ri, qayta kiriting." in prompter.notes


async def test_code_attempts_exhausted() -> None:
    with pytest.raises(LoginError, match="urinishlari soni tugadi"):
        await interactive_login(FakeClient(), ScriptedPrompter(codes=["1", "2", "3"]))


async def test_two_factor_password() -> None:
    prompter = ScriptedPrompter(passwords=["xato", PASSWORD])
    result = await interactive_login(FakeClient(needs_password=True), prompter)
    assert result.new_session
    assert any("2FA" in note for note in prompter.notes)
    assert "Parol noto'g'ri." in prompter.notes


async def test_flood_wait_on_send_code() -> None:
    client = FakeClient(send_error=errors.FloodWaitError(request=None, capture=120))
    with pytest.raises(LoginError, match="120 soniyadan"):
        await interactive_login(client, ScriptedPrompter())


async def test_bot_account_is_rejected() -> None:
    client = FakeClient(user=types.User(id=5, bot=True, first_name="Bot"))
    with pytest.raises(LoginError, match="bot akkaunt"):
        await interactive_login(client, ScriptedPrompter())


async def test_secrets_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    prompter = ScriptedPrompter(passwords=[PASSWORD])
    await interactive_login(FakeClient(needs_password=True), prompter)
    for secret in (PHONE, CODE, PASSWORD):
        assert secret not in caplog.text
        assert all(secret not in note for note in prompter.notes)


async def test_logout() -> None:
    client = FakeClient(authorized=True)
    assert await logout(client) is True and client.logged_out
    assert await logout(FakeClient(authorized=False)) is False
