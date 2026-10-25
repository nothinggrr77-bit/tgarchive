"""Telethon client yaratish va session faylini himoyalash.

Archiver asosiy akkauntda oddiy qo'shimcha sessiya (qurilma) sifatida ko'rinadi:
Telegram -> Settings -> Devices ro'yxatida nomi ``DEVICE_MODEL``.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from telethon import TelegramClient

from tgarchive import __version__
from tgarchive.config import ArchiverSettings
from tgarchive.fs import IS_POSIX, ensure_private_dir

DEVICE_MODEL = "tgarchive"
REQUEST_RETRIES = 5
CONNECTION_RETRIES = 10
RETRY_DELAY_S = 5


def session_files(session_path: Path) -> list[Path]:
    """Session fayli va SQLite yordamchi fayli."""
    return [session_path, session_path.with_name(session_path.name + "-journal")]


def secure_session_files(session_path: Path) -> None:
    """Session fayllarini faqat egasi o'qiy oladigan qiladi (0600)."""
    if not IS_POSIX:
        return
    for path in session_files(session_path):
        if path.exists():
            os.chmod(path, 0o600)


def create_client(settings: ArchiverSettings, *, receive_updates: bool = False) -> TelegramClient:
    """Client yaratadi (ulanmaydi). Session fayli shu payt yaratiladi, agar yo'q bo'lsa.

    ``receive_updates=False`` — CLI buyruqlari uchun (login, dialogs): real vaqt
    hodisalari kerak emas. Archiver xizmati ``REALTIME_ENABLED`` bo'lsa ``True`` bilan ishlaydi:
    ``catch_up`` — qayta ulanganda o'tkazib yuborilgan hodisalarni so'rab olish,
    ``sequential_updates`` — hodisalar kelish tartibida qayta ishlanadi (yangi xabar tahriridan oldin).
    """
    ensure_private_dir(settings.paths.sessions)
    client = TelegramClient(
        str(settings.session_path),
        settings.tg_api_id,
        settings.tg_api_hash.get_secret_value(),
        device_model=DEVICE_MODEL,
        system_version=f"{platform.system()} {platform.release()}".strip(),
        app_version=__version__,
        flood_sleep_threshold=settings.flood_sleep_threshold_s,
        request_retries=REQUEST_RETRIES,
        connection_retries=CONNECTION_RETRIES,
        retry_delay=RETRY_DELAY_S,
        auto_reconnect=True,
        receive_updates=receive_updates,
        catch_up=receive_updates,
        sequential_updates=receive_updates,
        base_logger="telethon",
    )
    secure_session_files(settings.session_path)
    return client
