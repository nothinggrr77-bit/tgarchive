"""Ixtiyoriy integratsion test: haqiqiy session bilan FAQAT O'QISH.

Ishga tushirish (alohida test akkaunt tavsiya etiladi):

    tgarchive --env-file .env.test login
    TGARCHIVE_INTEGRATION=1 TGARCHIVE_ENV_FILE=.env.test pytest -m integration

Hech narsa yuborilmaydi, o'chirilmaydi va bazaga yozilmaydi.
"""

from __future__ import annotations

import os

import pytest

from tgarchive.archiver.client import create_client
from tgarchive.archiver.convert import message_to_record
from tgarchive.archiver.dialogs import fetch_dialogs
from tgarchive.config import ArchiverSettings, load_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def live_settings() -> ArchiverSettings:
    settings = load_settings(ArchiverSettings, os.environ.get("TGARCHIVE_ENV_FILE", ".env"))
    if not settings.session_path.exists():
        pytest.skip("session yo'q: avval tgarchive login")
    return settings


async def test_dialogs_and_messages_convert(live_settings: ArchiverSettings) -> None:
    client = create_client(live_settings)
    try:
        await client.connect()
        assert await client.is_user_authorized()
        me = await client.get_me()
        dialogs = await fetch_dialogs(client, limit=5)
        assert dialogs
        for dialog in dialogs[:3]:
            async for msg in client.iter_messages(dialog.chat.chat_id, limit=5):
                record = message_to_record(msg, self_id=me.id)
                if record is not None:
                    assert record.chat_id == dialog.chat.chat_id
    finally:
        await client.disconnect()
