"""Umumiy fixture'lar. Testlar haqiqiy Telegram'ga ulanmaydi.

Har bir test migratsiya qilingan shablon bazaning nusxasi bilan ishlaydi:
migratsiya sessiyada bir marta bajariladi, keyin fayl nusxalanadi (tez va izolyatsiyalangan).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from factories import sqlite_url
from tgarchive.config import ArchiverSettings, BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.migrate import upgrade

INTEGRATION = os.environ.get("TGARCHIVE_INTEGRATION") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if INTEGRATION:
        return
    skip = pytest.mark.skip(reason="integratsion test: TGARCHIVE_INTEGRATION=1 bilan ishga tushiring")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def clean_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dasturchi kompyuteridagi muhit o'zgaruvchilari unit testlarga ta'sir qilmasin."""
    if "integration" in request.keywords:
        return
    for cls in (ArchiverSettings, BotSettings):
        for name in cls.model_fields:
            monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "template.sqlite3"
    upgrade(sqlite_url(path))
    return path


@pytest.fixture
def db_path(tmp_path: Path, migrated_template: Path) -> Path:
    path = tmp_path / "archive.sqlite3"
    shutil.copyfile(migrated_template, path)
    return path


@pytest.fixture
async def db(db_path: Path) -> AsyncIterator[Database]:
    database = Database(sqlite_url(db_path), busy_timeout_ms=2000)
    yield database
    await database.dispose()
