from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from tgarchive.logging_setup import get_logger, setup_logging


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    yield buffer
    logging.getLogger().handlers = []
    structlog.reset_defaults()


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_json_output_with_context_and_key_redaction(stream: io.StringIO) -> None:
    setup_logging("INFO", "json", stream=stream)
    get_logger("test").info("page_imported", chat_id=5, count=100, api_hash="raw-hash", bot_token="raw-token")

    line = _json_lines(stream)[-1]
    assert line["event"] == "page_imported"
    assert line["chat_id"] == 5 and line["count"] == 100
    assert line["level"] == "info" and line["logger"] == "test"
    assert line["timestamp"].endswith("Z")
    assert line["api_hash"] == "***" and line["bot_token"] == "***"


def test_registered_secret_is_removed_everywhere(stream: io.StringIO) -> None:
    secret = "0123456789abcdef0123456789abcdef"
    setup_logging("INFO", "json", secrets=[secret], stream=stream)

    get_logger("test").warning("connecting", detail=f"hash={secret}")
    logging.getLogger("telethon.network").warning("auth with %s", secret)
    try:
        raise RuntimeError(f"request failed for {secret}")
    except RuntimeError:
        get_logger("test").exception("request_failed")

    output = stream.getvalue()
    assert secret not in output
    assert output.count("***") >= 3
    assert "RuntimeError" in output  # traceback saqlanadi, faqat maxfiy qiymat yo'qoladi


def test_bot_token_shape_is_redacted_without_registration(stream: io.StringIO) -> None:
    setup_logging("INFO", "json", stream=stream)
    token = "123456789:" + "A" * 35
    logging.getLogger("aiogram").error("GET https://api.telegram.org/bot%s/getMe failed", token)
    output = stream.getvalue()
    assert token not in output
    assert "api.telegram.org/bot***/getMe" in output


def test_library_loggers_are_quieter(stream: io.StringIO) -> None:
    setup_logging("DEBUG", "json", stream=stream)
    logging.getLogger("telethon.network").info("noisy details")
    logging.getLogger("tgarchive.x").debug("ours")
    output = stream.getvalue()
    assert "noisy details" not in output
    assert "ours" in output


def test_console_format(stream: io.StringIO) -> None:
    setup_logging("INFO", "console", stream=stream)
    get_logger("test").info("started", component="archiver")
    output = stream.getvalue()
    assert "started" in output and "component=archiver" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.splitlines()[-1])
