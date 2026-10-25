from __future__ import annotations

from pathlib import Path

import pytest

from tgarchive.config import ArchiverSettings, BaseAppSettings, BotSettings, ConfigError, load_settings
from tgarchive.db.enums import MediaType

VALID_HASH = "0123456789abcdef0123456789abcdef"
VALID_TOKEN = "123456789:AAH" + "x" * 32


def test_channel_and_media_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Egasi qarori: kanallar — oxirgi 20 ta post + har bir izoh; media — shaxsiy chatlarda."""
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", VALID_HASH)
    settings = load_settings(ArchiverSettings, env_file=None)
    assert settings.channel_keep_posts == 20
    assert settings.channel_save_comments is True
    assert settings.media_enabled is True and settings.media_in_channels is False
    assert not hasattr(settings, "history_use_takeout")  # takeout ishlatilmaydi
    assert settings.retention_days is None


def test_defaults_and_derived_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = load_settings(BaseAppSettings, env_file=None)

    data = (tmp_path / "data").resolve()
    assert settings.paths.data == data
    assert settings.paths.db == data / "db" / "archive.sqlite3"
    assert settings.paths.media == data / "media"
    assert settings.paths.sessions == data / "session"
    assert settings.database_url.drivername == "sqlite+aiosqlite"
    assert settings.database_url.database == str(data / "db" / "archive.sqlite3")
    assert settings.retention_days is None


def test_explicit_paths_override_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "other" / "x.sqlite3"))
    settings = load_settings(BaseAppSettings, env_file=None)
    assert settings.paths.db == (tmp_path / "other" / "x.sqlite3").resolve()


def test_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"TG_API_ID=12345\nTG_API_HASH={VALID_HASH}\nLOG_LEVEL=debug\n", encoding="utf-8")
    settings = load_settings(ArchiverSettings, env_file=env)
    assert settings.tg_api_id == 12345
    assert settings.log_level == "DEBUG"
    assert settings.session_path.name == "archiver.session"


def test_lists_and_media_types_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", VALID_HASH)
    monkeypatch.setenv("CHAT_ALLOWLIST", "1001, -1001234567890")
    monkeypatch.setenv("CHAT_DENYLIST", "")
    monkeypatch.setenv("MEDIA_TYPES", "Photo,voice")
    settings = load_settings(ArchiverSettings, env_file=None)
    assert settings.chat_allowlist == [1001, -1001234567890]
    assert settings.chat_denylist == []
    assert settings.media_types == {MediaType.PHOTO, MediaType.VOICE}


def test_non_file_media_type_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", VALID_HASH)
    monkeypatch.setenv("MEDIA_TYPES", "photo,poll")
    with pytest.raises(ConfigError, match="MEDIA_TYPES.*poll"):
        load_settings(ArchiverSettings, env_file=None)


def test_missing_required_fields_are_listed() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(ArchiverSettings, env_file=None)
    message = str(info.value)
    assert "TG_API_ID: majburiy" in message
    assert "TG_API_HASH: majburiy" in message


def test_errors_do_not_leak_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "SuperSecretButInvalidHash")
    with pytest.raises(ConfigError) as info:
        load_settings(ArchiverSettings, env_file=None)
    assert "SuperSecretButInvalidHash" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__

    monkeypatch.setenv("BOT_TOKEN", "not-a-real-secret-token")
    monkeypatch.setenv("OWNER_ID", "42")
    with pytest.raises(ConfigError) as info:
        load_settings(BotSettings, env_file=None)
    assert "not-a-real-secret-token" not in str(info.value)
    assert "BOT_TOKEN" in str(info.value)


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", VALID_HASH)
    settings = load_settings(ArchiverSettings, env_file=None)
    assert VALID_HASH not in repr(settings)
    assert str(settings.tg_api_hash) == "**********"
    assert settings.secret_values() == [VALID_HASH]


def test_bot_does_not_need_archiver_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("OWNER_ID", "424242")
    settings = load_settings(BotSettings, env_file=None)
    assert settings.owner_id == 424242
    assert not hasattr(settings, "tg_api_hash")


def test_owner_id_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("OWNER_ID", "0")
    with pytest.raises(ConfigError, match="OWNER_ID"):
        load_settings(BotSettings, env_file=None)


def test_invalid_timezone_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError, match="DISPLAY_TIMEZONE"):
        load_settings(BaseAppSettings, env_file=None)


def test_history_page_size_respects_telegram_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", VALID_HASH)
    monkeypatch.setenv("HISTORY_PAGE_SIZE", "500")
    with pytest.raises(ConfigError, match="HISTORY_PAGE_SIZE"):
        load_settings(ArchiverSettings, env_file=None)
