"""Telethon TL obyektlarini tarmoqsiz yasash (adapter testlari uchun)."""

from __future__ import annotations

from typing import Any

from telethon.tl import types

from factories import at

ME_ID = 111
ALI_ID = 7
CHANNEL_RAW_ID = 1234567890
CHANNEL_ID = -1001234567890
GROUP_RAW_ID = 555
GROUP_ID = -555


def user(user_id: int = ALI_ID, **fields: Any) -> types.User:
    fields.setdefault("first_name", "Ali")
    return types.User(id=user_id, **fields)


def me() -> types.User:
    return types.User(id=ME_ID, is_self=True, first_name="Men", username="men")


def channel(channel_id: int = CHANNEL_RAW_ID, *, broadcast: bool = True, **fields: Any) -> types.Channel:
    fields.setdefault("title", "Yangiliklar")
    return types.Channel(
        id=channel_id,
        photo=types.ChatPhotoEmpty(),
        date=at(),
        broadcast=broadcast,
        megagroup=not broadcast,
        **fields,
    )


def group(chat_id: int = GROUP_RAW_ID, **fields: Any) -> types.Chat:
    fields.setdefault("title", "Oila")
    return types.Chat(
        id=chat_id, photo=types.ChatPhotoEmpty(), participants_count=3, date=at(), version=1, **fields
    )


def message(message_id: int = 1, peer: Any = None, **fields: Any) -> types.Message:
    fields.setdefault("date", at(message_id))
    fields.setdefault("message", f"xabar {message_id}")
    return types.Message(id=message_id, peer_id=peer or types.PeerUser(ALI_ID), **fields)


def photo_media(**fields: Any) -> types.MessageMediaPhoto:
    photo = types.Photo(
        id=100,
        access_hash=200,
        file_reference=b"",
        date=at(),
        sizes=[
            types.PhotoStrippedSize(type="i", bytes=b"\x01\x02"),
            types.PhotoSize(type="m", w=320, h=240, size=15_000),
            types.PhotoSizeProgressive(type="y", w=1280, h=960, sizes=[1_000, 50_000, 120_000]),
        ],
        dc_id=2,
    )
    return types.MessageMediaPhoto(photo=photo, **fields)


def document_media(attributes: list[Any], *, mime: str = "application/octet-stream", **flags: Any):
    doc = types.Document(
        id=300,
        access_hash=400,
        file_reference=b"",
        date=at(),
        mime_type=mime,
        size=4096,
        dc_id=4,
        attributes=attributes,
    )
    return types.MessageMediaDocument(document=doc, **flags)
