"""Telethon -> MessageRecord / ChatInfo adapteri. Haqiqiy TL obyektlari, tarmoqsiz."""

from __future__ import annotations

import pytest
from telethon.tl import types

from factories import add_chats, at
from tgarchive.archiver.convert import (
    DELETED_ACCOUNT_TITLE,
    SAVED_MESSAGES_TITLE,
    chat_info_from_entity,
    entities_to_json,
    media_info,
    message_to_record,
)
from tgarchive.db.engine import Database
from tgarchive.db.enums import ChatType, MediaType, MessageSource
from tgarchive.db.store import get_message, get_reply_chain, save_messages
from tl_objects import (
    ALI_ID,
    CHANNEL_ID,
    CHANNEL_RAW_ID,
    GROUP_ID,
    GROUP_RAW_ID,
    ME_ID,
    channel,
    document_media,
    group,
    me,
    message,
    photo_media,
    user,
)

# --------------------------------------------------------------------- chatlar


def test_private_user() -> None:
    info = chat_info_from_entity(user(first_name="Ali", last_name="Valiyev", username="ali"))
    assert (info.chat_id, info.chat_type, info.title, info.username) == (ALI_ID, ChatType.PRIVATE, "Ali Valiyev", "ali")


def test_bot_saved_and_deleted_users() -> None:
    assert chat_info_from_entity(user(9, bot=True, first_name="Helper")).chat_type is ChatType.BOT
    saved = chat_info_from_entity(me())
    assert (saved.chat_type, saved.title, saved.chat_id) == (ChatType.SAVED, SAVED_MESSAGES_TITLE, ME_ID)
    deleted = chat_info_from_entity(types.User(id=10, deleted=True))
    assert deleted.title == DELETED_ACCOUNT_TITLE


def test_basic_group_with_migration() -> None:
    entity = group(migrated_to=types.InputChannel(channel_id=777, access_hash=1), noforwards=True)
    info = chat_info_from_entity(entity)
    assert (info.chat_id, info.chat_type, info.title) == (GROUP_ID, ChatType.GROUP, "Oila")
    assert info.migrated_to_chat_id == -1000000000777
    assert info.is_protected is True
    assert chat_info_from_entity(types.ChatForbidden(id=GROUP_RAW_ID, title="Eski")).chat_type is ChatType.GROUP


def test_channel_and_supergroup() -> None:
    info = chat_info_from_entity(channel(username="news", noforwards=True))
    assert (info.chat_id, info.chat_type, info.username, info.is_protected) == (CHANNEL_ID, ChatType.CHANNEL, "news", True)
    assert chat_info_from_entity(channel(broadcast=False, forum=True)).chat_type is ChatType.SUPERGROUP
    forbidden = types.ChannelForbidden(id=CHANNEL_RAW_ID, access_hash=1, title="Yopiq", broadcast=True)
    assert chat_info_from_entity(forbidden).chat_type is ChatType.CHANNEL


def test_username_from_usernames_list() -> None:
    entity = channel(usernames=[types.Username(username="old", active=False), types.Username(username="new", active=True)])
    assert chat_info_from_entity(entity).username == "new"


def test_unknown_entity_raises() -> None:
    with pytest.raises(TypeError):
        chat_info_from_entity(types.PeerUser(1))


# ------------------------------------------------------------------ xabarlar


def test_incoming_private_message_sender_is_the_chat() -> None:
    record = message_to_record(message(5, message="Salom", out=False), self_id=ME_ID, sender=user())
    assert record is not None
    assert (record.chat_id, record.message_id, record.sender_id, record.sender_name) == (ALI_ID, 5, ALI_ID, "Ali")
    assert record.text == "Salom" and record.is_outgoing is False and record.date == at(5)


def test_outgoing_private_message_sender_is_me() -> None:
    record = message_to_record(message(6, out=True), self_id=ME_ID)
    assert record.sender_id == ME_ID and record.is_outgoing is True


def test_channel_post_sender_is_channel() -> None:
    msg = message(3, peer=types.PeerChannel(CHANNEL_RAW_ID), post=True, views=10)
    record = message_to_record(msg, self_id=ME_ID, sender=channel())
    assert (record.chat_id, record.sender_id, record.sender_name) == (CHANNEL_ID, CHANNEL_ID, "Yangiliklar")


def test_group_message_with_from_id() -> None:
    msg = message(4, peer=types.PeerChat(GROUP_RAW_ID), from_id=types.PeerUser(ALI_ID))
    record = message_to_record(msg, self_id=ME_ID)
    assert (record.chat_id, record.sender_id) == (GROUP_ID, ALI_ID)


def test_empty_text_and_message_empty() -> None:
    assert message_to_record(types.MessageEmpty(id=1, peer_id=None), self_id=ME_ID) is None
    assert message_to_record(message(1, message=""), self_id=ME_ID).text is None


def test_entities_are_serialized() -> None:
    entities = [
        types.MessageEntityBold(offset=0, length=4),
        types.MessageEntityTextUrl(offset=5, length=6, url="https://example.org"),
        types.MessageEntityMentionName(offset=12, length=3, user_id=ALI_ID),
    ]
    assert entities_to_json(entities) == [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "text_url", "offset": 5, "length": 6, "url": "https://example.org"},
        {"type": "mention_name", "offset": 12, "length": 3, "user_id": ALI_ID},
    ]
    assert entities_to_json(None) is None


def test_edit_date_grouped_id_and_noforwards() -> None:
    record = message_to_record(message(2, edit_date=at(30), grouped_id=999, noforwards=True), self_id=ME_ID)
    assert (record.edit_date, record.grouped_id, record.noforwards) == (at(30), 999, True)


def test_plain_reply() -> None:
    msg = message(10, reply_to=types.MessageReplyHeader(reply_to_msg_id=8))
    record = message_to_record(msg, self_id=ME_ID)
    assert (record.reply_to_msg_id, record.reply_to_peer_id, record.reply_to_top_id) == (8, None, None)


def test_cross_chat_reply_keeps_peer() -> None:
    header = types.MessageReplyHeader(reply_to_msg_id=50, reply_to_peer_id=types.PeerChannel(CHANNEL_RAW_ID))
    record = message_to_record(message(11, reply_to=header), self_id=ME_ID)
    assert (record.reply_to_msg_id, record.reply_to_peer_id) == (50, CHANNEL_ID)


def test_forum_topic_message_is_not_a_reply() -> None:
    header = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=77)
    record = message_to_record(message(12, peer=types.PeerChannel(CHANNEL_RAW_ID), reply_to=header), self_id=ME_ID)
    assert (record.reply_to_msg_id, record.reply_to_top_id) == (None, 77)


def test_reply_inside_forum_topic() -> None:
    header = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=90, reply_to_top_id=77)
    record = message_to_record(message(13, peer=types.PeerChannel(CHANNEL_RAW_ID), reply_to=header), self_id=ME_ID)
    assert (record.reply_to_msg_id, record.reply_to_top_id) == (90, 77)


def test_story_reply_is_ignored() -> None:
    header = types.MessageReplyStoryHeader(peer=types.PeerUser(ALI_ID), story_id=1)
    assert message_to_record(message(14, reply_to=header), self_id=ME_ID).reply_to_msg_id is None


def test_forward_from_channel_post() -> None:
    fwd = types.MessageFwdHeader(date=at(-60), from_id=types.PeerChannel(CHANNEL_RAW_ID), channel_post=321)
    record = message_to_record(message(15, fwd_from=fwd), self_id=ME_ID)
    assert (record.fwd_from_id, record.fwd_date, record.fwd_channel_post_id) == (CHANNEL_ID, at(-60), 321)


def test_hidden_forward_keeps_only_name() -> None:
    fwd = types.MessageFwdHeader(date=at(-5), from_name="Yashirin odam")
    record = message_to_record(message(16, fwd_from=fwd), self_id=ME_ID)
    assert (record.fwd_from_id, record.fwd_from_name) == (None, "Yashirin odam")


def test_service_message() -> None:
    msg = types.MessageService(
        id=17, peer_id=types.PeerChat(GROUP_RAW_ID), date=at(17), action=types.MessageActionChatAddUser(users=[ALI_ID])
    )
    record = message_to_record(msg, self_id=ME_ID)
    assert (record.action, record.text, record.media) == ("chat_add_user", None, None)


# --------------------------------------------------------------------- media


def test_photo_uses_largest_size() -> None:
    info = media_info(photo_media())
    assert info.media_type is MediaType.PHOTO
    assert (info.tg_media_id, info.tg_access_hash, info.tg_dc_id) == (100, 200, 2)
    assert (info.width, info.height, info.size) == (1280, 960, 120_000)
    assert info.ttl_seconds is None


def test_view_once_photo_keeps_ttl() -> None:
    assert media_info(photo_media(ttl_seconds=10)).ttl_seconds == 10
    expired = media_info(types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1), ttl_seconds=10))
    assert (expired.media_type, expired.tg_media_id, expired.ttl_seconds) == (MediaType.PHOTO, None, 10)


@pytest.mark.parametrize(
    ("attributes", "flags", "expected"),
    [
        ([types.DocumentAttributeAudio(duration=7, voice=True)], {"voice": True}, MediaType.VOICE),
        ([types.DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)], {"round": True}, MediaType.VIDEO_NOTE),
        ([types.DocumentAttributeSticker(alt=":)", stickerset=types.InputStickerSetEmpty()),
          types.DocumentAttributeImageSize(w=512, h=512)], {}, MediaType.STICKER),
        ([types.DocumentAttributeVideo(duration=3, w=320, h=240), types.DocumentAttributeAnimated()], {}, MediaType.ANIMATION),
        ([types.DocumentAttributeVideo(duration=60, w=1920, h=1080)], {"video": True}, MediaType.VIDEO),
        ([types.DocumentAttributeAudio(duration=200, title="Qo'shiq", performer="X")], {}, MediaType.AUDIO),
        ([types.DocumentAttributeFilename(file_name="hisobot.pdf")], {}, MediaType.DOCUMENT),
    ],
)
def test_document_kinds(attributes, flags, expected) -> None:
    assert media_info(document_media(attributes, **flags)).media_type is expected


def test_document_metadata() -> None:
    attrs = [types.DocumentAttributeVideo(duration=12.5, w=640, h=360), types.DocumentAttributeFilename(file_name="v.mp4")]
    info = media_info(document_media(attrs, mime="video/mp4", video=True))
    assert (info.tg_media_id, info.mime_type, info.file_name, info.size) == (300, "video/mp4", "v.mp4", 4096)
    assert (info.duration, info.width, info.height) == (12.5, 640, 360)


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        (types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)), MediaType.WEBPAGE),
        (types.MessageMediaGeo(geo=types.GeoPointEmpty()), MediaType.LOCATION),
        (types.MessageMediaContact(phone_number="", first_name="A", last_name="", vcard="", user_id=0), MediaType.CONTACT),
        (types.MessageMediaDice(value=3, emoticon="🎲"), MediaType.DICE),
        (types.MessageMediaUnsupported(), MediaType.OTHER),
    ],
)
def test_non_file_media(media, expected) -> None:
    info = media_info(media)
    assert info.media_type is expected and not info.media_type.is_file


def test_no_media() -> None:
    assert media_info(None) is None
    assert media_info(types.MessageMediaEmpty()) is None


# --------------------------------------------- adapter + saqlash qatlami birga


async def test_converted_messages_are_stored_with_reply_chain(db: Database) -> None:
    group_info = chat_info_from_entity(group())
    await add_chats(db, group_info)
    peer = types.PeerChat(GROUP_RAW_ID)
    raw = [
        message(1, peer=peer, from_id=types.PeerUser(ALI_ID), message="Savol"),
        message(2, peer=peer, out=True, message="Javob", reply_to=types.MessageReplyHeader(reply_to_msg_id=1)),
        message(3, peer=peer, from_id=types.PeerUser(ALI_ID), message="Rahmat",
                reply_to=types.MessageReplyHeader(reply_to_msg_id=2), media=photo_media()),
    ]
    records = [message_to_record(m, self_id=ME_ID) for m in raw]
    async with db.write() as session:
        await save_messages(session, records, MessageSource.HISTORY)
    async with db.read() as session:
        last = await get_message(session, GROUP_ID, 3)
        chain = await get_reply_chain(session, last)
    assert [m.message_id for m in chain] == [2, 1]
    assert chain[0].sender_id == ME_ID
    assert last.media_type is MediaType.PHOTO
