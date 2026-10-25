from __future__ import annotations

from typing import Any

import pytest

from factories import CHANNEL, GROUP, PRIVATE
from tgarchive.config import ArchiverSettings
from tgarchive.db.enums import ArchiveMode, ChatType
from tgarchive.db.store import ChatInfo
from tgarchive.policy import decide_policy

VALID_HASH = "0123456789abcdef0123456789abcdef"
BOT_CHAT = ChatInfo(chat_id=42, chat_type=ChatType.BOT, title="Helper")
SAVED = ChatInfo(chat_id=111, chat_type=ChatType.SAVED, title="Saved Messages")
SUPERGROUP = ChatInfo(chat_id=-1009, chat_type=ChatType.SUPERGROUP, title="Jamoa")


def settings(**overrides: Any) -> ArchiverSettings:
    return ArchiverSettings(_env_file=None, tg_api_id=1, tg_api_hash=VALID_HASH, **overrides)


@pytest.mark.parametrize("chat", [PRIVATE, SAVED, GROUP, SUPERGROUP])
def test_personal_chats_are_archived_fully_and_permanently(chat: ChatInfo) -> None:
    policy = decide_policy(chat, settings())
    assert policy.mode is ArchiveMode.FULL and policy.keep_last is None
    assert policy.media is True and policy.comments is False


def test_channels_keep_last_20_posts_and_every_comment() -> None:
    policy = decide_policy(CHANNEL, settings())
    assert policy.mode is ArchiveMode.WINDOW
    assert policy.keep_last == 20
    assert policy.comments is True
    assert policy.media is False and policy.media_reason == "MEDIA_IN_CHANNELS=false"


def test_channel_full_history_when_window_disabled() -> None:
    policy = decide_policy(CHANNEL, settings(channel_keep_posts=0, channel_save_comments=False))
    assert (policy.mode, policy.comments) == (ArchiveMode.FULL, False)


def test_bots_are_skipped_by_default() -> None:
    policy = decide_policy(BOT_CHAT, settings())
    assert policy.mode is ArchiveMode.SKIP and policy.reason == "ARCHIVE_BOTS=false"


def test_denylist_beats_everything() -> None:
    policy = decide_policy(PRIVATE, settings(chat_denylist=[PRIVATE.chat_id]), archive_override=True)
    assert policy.mode is ArchiveMode.SKIP and "DENYLIST" in policy.reason


def test_allowlist_limits_archive() -> None:
    cfg = settings(chat_allowlist=[GROUP.chat_id])
    assert decide_policy(GROUP, cfg).archived
    assert decide_policy(PRIVATE, cfg).reason == "CHAT_ALLOWLIST da yo'q"


def test_owner_override() -> None:
    assert decide_policy(BOT_CHAT, settings(), archive_override=True).mode is ArchiveMode.FULL
    assert decide_policy(PRIVATE, settings(chat_allowlist=[1]), archive_override=True).archived
    assert decide_policy(PRIVATE, settings(), archive_override=False).mode is ArchiveMode.SKIP


def test_media_rules() -> None:
    assert decide_policy(PRIVATE, settings(media_enabled=False)).media is False
    assert decide_policy(CHANNEL, settings(media_in_channels=True)).media is True
    assert decide_policy(PRIVATE, settings(media_enabled=False), media_override=True).media is True


def test_protected_chat_never_downloads_media() -> None:
    protected = ChatInfo(chat_id=5, chat_type=ChatType.PRIVATE, title="Yopiq", is_protected=True)
    policy = decide_policy(protected, settings(), media_override=True)
    assert policy.archived and policy.media is False
    assert policy.media_reason == "himoyalangan kontent"
