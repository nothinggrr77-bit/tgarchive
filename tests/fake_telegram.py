"""Xotiradagi soxta Telegram: tarix, izohlar, dialoglar va xato kiritish.

Telethon'ning faqat archiver ishlatadigan qismi taqlid qilinadi; semantikasi Telethon
hujjatiga mos (``offset_id`` — shu ID'dan kichiklar, ``reverse=True`` da ``min_id`` —
pastki chegara, natija eskidan yangiga).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon import utils
from telethon.tl import functions, types

from tl_objects import me


@dataclass
class FakeDialog:
    entity: Any
    message: Any
    archived: bool = False
    unread_count: int = 0


class FakeTelegram:
    def __init__(self) -> None:
        self.entities: dict[int, Any] = {}
        self.history: dict[int, dict[int, Any]] = defaultdict(dict)
        self.comments: dict[tuple[int, int], list[Any]] = defaultdict(list)
        self.roots: dict[tuple[int, int], tuple[Any, Any]] = {}
        self.hidden: set[int] = set()  # get_input_entity topa olmaydigan chatlar
        self.not_in_dialogs: set[int] = set()  # mavjud, lekin dialoglar ro'yxatida yo'q (a'zo emas)
        self.me = me()
        self.authorized = True
        self.connected = False
        self.calls: list[tuple[str, int, dict[str, Any]]] = []
        self.handlers: list[tuple[Any, Any]] = []
        self.on_connect: list[tuple[type, Any]] = []  # ulanishda yuboriladigan hodisalar (catch_up)
        self._errors: dict[str, list[tuple[int, BaseException]]] = defaultdict(list)
        self._counts: dict[str, int] = defaultdict(int)

    # --------------------------------------------------------------- tayyorlash

    def add_chat(self, entity: Any, *messages: Any, dialog: bool = True) -> int:
        chat_id = utils.get_peer_id(entity)
        self.entities[chat_id] = entity
        if not dialog:
            self.not_in_dialogs.add(chat_id)
        for msg in messages:
            self.history[chat_id][msg.id] = msg
        return chat_id

    def add(self, chat_id: int, *messages: Any) -> None:
        for msg in messages:
            self.history[chat_id][msg.id] = msg

    def fail(self, method: str, exc: BaseException, *, on_call: int = 1) -> None:
        """``method`` ning ``on_call``-chi chaqiruvi (shu paytdan sanab) ``exc`` beradi."""
        self._errors[method].append((self._counts[method] + on_call, exc))

    def _tick(self, method: str) -> None:
        self._counts[method] += 1
        for item in list(self._errors[method]):
            if item[0] == self._counts[method]:
                self._errors[method].remove(item)
                raise item[1]

    # ------------------------------------------------------------- client API

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.calls.append(("connect", 0, {}))
        self.connected = True
        for builder_type, event in self.on_connect:
            await self.emit(builder_type, event)
        self.on_connect = []

    # ------------------------------------------------------------- hodisalar

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.calls.append(("add_event_handler", 0, {"event": type(event).__name__}))
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any = None) -> int:
        before = len(self.handlers)
        self.handlers = [
            (cb, ev) for cb, ev in self.handlers if not (cb == callback and (event is None or ev is event))
        ]
        return before - len(self.handlers)

    async def emit(self, builder_type: type, event: Any) -> None:
        """Telethon kabi: faqat shu turdagi builder bilan ro'yxatdan o'tgan handler'lar chaqiriladi."""
        for callback, builder in list(self.handlers):
            if type(builder) is builder_type:
                await callback(event)

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return self.me

    async def get_input_entity(self, peer: int) -> int:
        if peer in self.hidden or peer not in self.entities:
            raise ValueError(f"Could not find the input entity for {peer}")
        return peer

    async def get_entity(self, peer: int) -> Any:
        if peer not in self.entities:
            raise ValueError(peer)
        return self.entities[peer]

    async def get_messages(self, entity: int, limit: int | None = None, **kwargs: Any) -> list[Any]:
        self.calls.append(("get_messages", entity, kwargs | {"limit": limit}))
        self._tick("get_messages")
        chat = self.history[entity]
        if kwargs.get("ids") is not None:
            return [chat.get(i) for i in kwargs["ids"]]
        reverse = kwargs.get("reverse", False)
        items = sorted(chat.values(), key=lambda m: m.id, reverse=not reverse)
        if kwargs.get("offset_id"):
            items = [m for m in items if m.id < kwargs["offset_id"]]
        if kwargs.get("min_id"):
            items = [m for m in items if m.id > kwargs["min_id"]]
        return items[:limit] if limit else items

    async def iter_messages(self, entity: int, limit: int | None = None, **kwargs: Any):
        self.calls.append(("iter_messages", entity, kwargs))
        self._tick("iter_messages")
        items = sorted(self.comments[(entity, kwargs["reply_to"])], key=lambda m: m.id, reverse=True)
        for msg in items:
            if msg.id > kwargs.get("min_id", 0):
                yield msg

    async def iter_dialogs(self, limit: int | None = None):
        for chat_id, entity in list(self.entities.items()):
            if chat_id in self.hidden or chat_id in self.not_in_dialogs:
                continue
            chat = self.history.get(chat_id) or {}
            top = chat[max(chat)] if chat else None
            yield FakeDialog(entity, top)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(("request", 0, {"type": type(request).__name__}))
        self._tick("request")
        if isinstance(request, functions.messages.GetDiscussionMessageRequest):
            root, chat_entity = self.roots[(request.peer, request.msg_id)]
            return types.messages.DiscussionMessage(
                messages=[root], unread_count=0, chats=[chat_entity], users=[], max_id=None
            )
        raise NotImplementedError(type(request).__name__)

    def count(self, method: str, **match: Any) -> int:
        return sum(
            1 for name, _chat, kw in self.calls if name == method and all(kw.get(k) == v for k, v in match.items())
        )


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingSleep:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)
