"""Bot API so'rovlarini tarmoqsiz yozib oluvchi aiogram session va update yasovchilar."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

OWNER = 424242
STRANGER = 666
TOKEN = "123456789:" + "A" * 35
_ids = count(1)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.failures: dict[type, Callable[[TelegramMethod[Any]], Exception]] = {}

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        factory = self.failures.pop(type(method), None)
        if factory is not None:
            raise factory(method)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Arxiv", username="arxiv_test_bot")
        if method.__returning__ is bool:
            return True
        chat_id = getattr(method, "chat_id", 0) or 0
        return Message(
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=int(chat_id), type="private"),
            text=getattr(method, "text", None),
        )

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    def of(self, kind: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, kind)]


def user(user_id: int = OWNER) -> User:
    return User(id=user_id, is_bot=False, first_name="Egasi" if user_id == OWNER else "Begona", username="u")


def text_update(text: str, *, user_id: int = OWNER, chat_id: int | None = None, chat_type: str = "private") -> Update:
    uid = next(_ids)
    return Update(
        update_id=uid,
        message=Message(
            message_id=uid,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
            from_user=user(user_id),
            text=text,
        ),
    )


def callback_update(data: str, *, user_id: int = OWNER, message_id: int = 900) -> Update:
    uid = next(_ids)
    return Update(
        update_id=uid,
        callback_query=CallbackQuery(
            id=f"cb{uid}",
            from_user=user(user_id),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=message_id, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), text="eski"
            ),
        ),
    )
