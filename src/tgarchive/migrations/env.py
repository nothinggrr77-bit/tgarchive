"""Alembic muhiti.

Baza manzili: ``config.attributes["database_url"]`` (dastur ichidan, migrate.py),
aks holda .env dagi DATA_DIR / DB_PATH (``alembic`` CLI orqali ishlatilganda).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tgarchive.db import models  # noqa: F401  (modellarni metadata'ga ro'yxatdan o'tkazadi)
from tgarchive.db.base import Base
from tgarchive.db.fts import FTS_TABLE

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> URL:
    url = config.attributes.get("database_url")
    if url is None:
        from tgarchive.config import BaseAppSettings

        url = BaseAppSettings().database_url
    return make_url(url)


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # FTS5 virtual jadvali va uning ichki jadvallari migratsiyada qo'lda boshqariladi.
    if type_ == "table" and name is not None and name.startswith(FTS_TABLE):
        return False
    return True


def _configure(**kwargs) -> None:  # noqa: ANN003
    context.configure(
        target_metadata=Base.metadata,
        render_as_batch=True,
        include_object=include_object,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:  # noqa: ANN001
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
