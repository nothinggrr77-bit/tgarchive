"""Alembic migratsiyalarini dastur ichidan qo'llash (alembic.ini kerak emas)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, Connection

SCRIPT_LOCATION = "tgarchive:migrations"


def alembic_config(database_url: str | URL) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    # URL'ni ini orqali emas, attributes orqali uzatamiz: '%' belgilari va maxfiylik uchun.
    cfg.attributes["database_url"] = database_url
    return cfg


def _outside_event_loop(fn: Callable[[], None]) -> None:
    """env.py ``asyncio.run()`` ishlatadi; ishlab turgan loop ichidan chaqirilsa — alohida thread'da."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        fn()
        return
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(fn).result()


def upgrade(database_url: str | URL, revision: str = "head") -> None:
    _outside_event_loop(lambda: command.upgrade(alembic_config(database_url), revision))


def downgrade(database_url: str | URL, revision: str) -> None:
    _outside_event_loop(lambda: command.downgrade(alembic_config(database_url), revision))


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def current_revision(sync_conn: Connection) -> str | None:
    return MigrationContext.configure(sync_conn).get_current_revision()
