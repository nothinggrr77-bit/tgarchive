"""Telethon TL obyektlarini kutubxonadan mustaqil yozuvlarga aylantirish.

Telethon tuzilmalarini biladigan yagona joy: ``ChatInfo``, ``MessageRecord``, ``MediaInfo``.
Kutubxona almashsa, faqat shu modul qayta yoziladi.
"""

from __future__ import annotations

import re
from typing import Any

from telethon import utils
from telethon.tl import types

from tgarchive.db.enums import ChatType, MediaType
from tgarchive.db.store import ChatInfo, MediaInfo, MessageRecord

SAVED_MESSAGES_TITLE = "Saved Messages"
DELETED_ACCOUNT_TITLE = "O'chirilgan akkaunt"

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

_SIMPLE_MEDIA: dict[type, MediaType] = {
    types.MessageMediaWebPage: MediaType.WEBPAGE,
    types.MessageMediaContact: MediaType.CONTACT,
    types.MessageMediaGeo: MediaType.LOCATION,
    types.MessageMediaVenue: MediaType.LOCATION,
    types.MessageMediaGeoLive: MediaType.LOCATION,
    types.MessageMediaPoll: MediaType.POLL,
    types.MessageMediaDice: MediaType.DICE,
    types.MessageMediaGame: MediaType.GAME,
    types.MessageMediaInvoice: MediaType.INVOICE,
    types.MessageMediaStory: MediaType.STORY,
}


def _snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


# ------------------------------------------------------------------- chats


def _username(entity: Any) -> str | None:
    if getattr(entity, "username", None):
        return entity.username
    for item in getattr(entity, "usernames", None) or []:
        if getattr(item, "active", False):
            return item.username
    return None


def chat_info_from_entity(entity: Any) -> ChatInfo:
    """User / Chat / Channel (va ularning Forbidden ko'rinishlari) -> ``ChatInfo``."""
    chat_id = utils.get_peer_id(entity)
    if isinstance(entity, types.User):
        if entity.is_self:
            return ChatInfo(chat_id, ChatType.SAVED, title=SAVED_MESSAGES_TITLE, username=_username(entity))
        title = utils.get_display_name(entity) or (DELETED_ACCOUNT_TITLE if entity.deleted else None)
        chat_type = ChatType.BOT if entity.bot else ChatType.PRIVATE
        return ChatInfo(chat_id, chat_type, title=title, username=_username(entity))
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        migrated = getattr(entity, "migrated_to", None)
        channel_id = getattr(migrated, "channel_id", None)
        return ChatInfo(
            chat_id,
            ChatType.GROUP,
            title=entity.title,
            is_protected=bool(getattr(entity, "noforwards", False)),
            migrated_to_chat_id=utils.get_peer_id(types.PeerChannel(channel_id)) if channel_id else None,
        )
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return ChatInfo(
            chat_id,
            ChatType.CHANNEL if entity.broadcast else ChatType.SUPERGROUP,
            title=entity.title,
            username=_username(entity),
            is_protected=bool(getattr(entity, "noforwards", False)),
        )
    raise TypeError(f"qo'llab-quvvatlanmaydigan entity: {type(entity).__name__}")


# ---------------------------------------------------------------- messages


def message_to_record(msg: Any, *, self_id: int, sender: Any = None) -> MessageRecord | None:
    """Telethon xabari -> ``MessageRecord``. ``MessageEmpty`` uchun ``None``.

    ``sender`` berilmasa, Telethon keshidagi ``msg.sender`` ishlatiladi (tarix
    so'rovlarida Telethon uni o'zi to'ldiradi). Chiquvchi shaxsiy xabarlarda Telegram
    yuboruvchini ko'rsatmaydi — u holda yuboruvchi ``self_id``.
    """
    if msg is None or isinstance(msg, types.MessageEmpty):
        return None

    sender_id = msg.sender_id
    if sender_id is None and msg.out:
        sender_id = self_id
    if sender is None:
        sender = getattr(msg, "sender", None)

    is_service = isinstance(msg, types.MessageService)
    reply_msg_id, reply_peer_id, reply_top_id = _reply_fields(msg.reply_to)
    fwd_from_id, fwd_from_name, fwd_date, fwd_channel_post_id = _forward_fields(msg.fwd_from)

    return MessageRecord(
        chat_id=utils.get_peer_id(msg.peer_id),
        message_id=msg.id,
        date=msg.date,
        sender_id=sender_id,
        sender_name=(utils.get_display_name(sender) or None) if sender is not None else None,
        is_outgoing=bool(msg.out),
        text=None if is_service else (msg.message or None),
        entities=None if is_service else entities_to_json(msg.entities),
        edit_date=msg.edit_date,
        reply_to_msg_id=reply_msg_id,
        reply_to_peer_id=reply_peer_id,
        reply_to_top_id=reply_top_id,
        fwd_from_id=fwd_from_id,
        fwd_from_name=fwd_from_name,
        fwd_date=fwd_date,
        fwd_channel_post_id=fwd_channel_post_id,
        grouped_id=msg.grouped_id,
        action=_snake(type(msg.action).__name__.removeprefix("MessageAction")) if is_service and msg.action else None,
        noforwards=bool(msg.noforwards),
        media=None if is_service else media_info(msg.media),
    )


def _reply_fields(reply_to: Any) -> tuple[int | None, int | None, int | None]:
    """(reply_to_msg_id, reply_to_peer_id, reply_to_top_id).

    Forum mavzusidagi oddiy xabarda Telegram ``reply_to_msg_id`` ga mavzu ID'sini
    yozadi (``forum_topic=True``, ``reply_to_top_id`` bo'sh) — bu haqiqiy reply emas.
    Story'ga javob (``MessageReplyStoryHeader``) xabar reply'i hisoblanmaydi.
    """
    if not isinstance(reply_to, types.MessageReplyHeader):
        return None, None, None
    peer_id = utils.get_peer_id(reply_to.reply_to_peer_id) if reply_to.reply_to_peer_id else None
    if reply_to.forum_topic and reply_to.reply_to_top_id is None:
        return None, peer_id, reply_to.reply_to_msg_id
    return reply_to.reply_to_msg_id, peer_id, reply_to.reply_to_top_id


def _forward_fields(fwd: Any) -> tuple[int | None, str | None, Any, int | None]:
    if fwd is None:
        return None, None, None, None
    from_id = utils.get_peer_id(fwd.from_id) if fwd.from_id else None
    return from_id, fwd.from_name, fwd.date, fwd.channel_post


def entities_to_json(entities: Any) -> list[dict[str, Any]] | None:
    """Formatlash entity'lari -> ``[{"type": "text_url", "offset": 0, "length": 5, "url": ...}]``."""
    if not entities:
        return None
    result = []
    for entity in entities:
        item: dict[str, Any] = {"type": _snake(type(entity).__name__.removeprefix("MessageEntity"))}
        for key, value in entity.to_dict().items():
            if key == "_" or value is None:
                continue
            item[key] = value if isinstance(value, (bool, int, float, str)) else str(value)
        result.append(item)
    return result


# ------------------------------------------------------------------- media


def media_info(media: Any) -> MediaInfo | None:
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return None
    if isinstance(media, types.MessageMediaPhoto):
        return _photo_info(media)
    if isinstance(media, types.MessageMediaDocument):
        return _document_info(media)
    return MediaInfo(_SIMPLE_MEDIA.get(type(media), MediaType.OTHER))


def _photo_info(media: types.MessageMediaPhoto) -> MediaInfo:
    photo = media.photo
    if not isinstance(photo, types.Photo):  # PhotoEmpty: masalan, muddati o'tgan view-once
        return MediaInfo(MediaType.PHOTO, ttl_seconds=media.ttl_seconds)
    width, height, size = _largest_photo_size(photo.sizes)
    return MediaInfo(
        MediaType.PHOTO,
        tg_media_id=photo.id,
        tg_access_hash=photo.access_hash,
        tg_dc_id=photo.dc_id,
        mime_type="image/jpeg",
        size=size,
        width=width,
        height=height,
        ttl_seconds=media.ttl_seconds,
    )


def _largest_photo_size(sizes: Any) -> tuple[int | None, int | None, int | None]:
    best: tuple[int | None, int | None, int | None] = (None, None, None)
    best_bytes = -1
    for size in sizes or []:
        if isinstance(size, types.PhotoSize):
            nbytes = size.size
        elif isinstance(size, types.PhotoSizeProgressive):
            nbytes = max(size.sizes) if size.sizes else 0
        elif isinstance(size, types.PhotoCachedSize):
            nbytes = len(size.bytes)
        else:  # stripped / path — kichik preview, fayl emas
            continue
        if nbytes > best_bytes:
            best_bytes = nbytes
            best = (size.w, size.h, nbytes)
    return best


def _document_info(media: types.MessageMediaDocument) -> MediaInfo:
    doc = media.document
    if not isinstance(doc, types.Document):
        return MediaInfo(MediaType.DOCUMENT, ttl_seconds=media.ttl_seconds)

    attrs = {type(attr): attr for attr in doc.attributes or []}
    audio = attrs.get(types.DocumentAttributeAudio)
    video = attrs.get(types.DocumentAttributeVideo)
    image = attrs.get(types.DocumentAttributeImageSize)
    filename = attrs.get(types.DocumentAttributeFilename)

    if media.round or (video is not None and video.round_message):
        kind = MediaType.VIDEO_NOTE
    elif media.voice or (audio is not None and audio.voice):
        kind = MediaType.VOICE
    elif types.DocumentAttributeSticker in attrs:
        kind = MediaType.STICKER
    elif types.DocumentAttributeAnimated in attrs:
        kind = MediaType.ANIMATION
    elif video is not None or media.video:
        kind = MediaType.VIDEO
    elif audio is not None:
        kind = MediaType.AUDIO
    else:
        kind = MediaType.DOCUMENT

    timed = video or audio
    sized = video or image
    return MediaInfo(
        kind,
        tg_media_id=doc.id,
        tg_access_hash=doc.access_hash,
        tg_dc_id=doc.dc_id,
        mime_type=doc.mime_type,
        file_name=filename.file_name if filename else None,
        size=doc.size,
        duration=float(timed.duration) if timed is not None and timed.duration is not None else None,
        width=sized.w if sized is not None else None,
        height=sized.h if sized is not None else None,
        ttl_seconds=media.ttl_seconds,
    )
