"""Inline tugmalar ma'lumoti. Telegram ``callback_data`` ni 64 bayt bilan cheklaydi —
shuning uchun qidiruv so'rovining o'zi emas, faqat qisqa token yuboriladi."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Menu(CallbackData, prefix="mn"):
    action: str


class SearchPage(CallbackData, prefix="sp"):
    token: str
    offset: int


class OpenMessage(CallbackData, prefix="m"):
    pk: int


class FullText(CallbackData, prefix="ft"):
    pk: int


class Versions(CallbackData, prefix="v"):
    pk: int


class SendFile(CallbackData, prefix="f"):
    media_id: int


class FetchFile(CallbackData, prefix="fx"):
    media_id: int


class ChatsPage(CallbackData, prefix="cp"):
    offset: int


class OpenChat(CallbackData, prefix="c"):
    chat_id: int


class Conversation(CallbackData, prefix="r"):
    """Chatni Telegram'dagidek o'qish: xabarlar oynasi (offset — oynaning boshlanishi)."""

    chat_id: int
    offset: int = 0


class ChatMessages(CallbackData, prefix="cm"):
    chat_id: int
    deleted: bool = False


class LargePage(CallbackData, prefix="lp"):
    offset: int


class MediaKind(CallbackData, prefix="mt"):
    kind: str
