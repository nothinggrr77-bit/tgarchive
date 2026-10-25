from __future__ import annotations

import pytest

from tgarchive.db.engine import Database
from tgarchive.db.meta import AccountMismatchError, bind_account, bound_account_id


async def test_first_binding_then_same_account(db: Database) -> None:
    async with db.write() as session:
        assert await bind_account(session, 111) is True
    async with db.write() as session:
        assert await bind_account(session, 111) is False
    async with db.read() as session:
        assert await bound_account_id(session) == 111


async def test_other_account_is_refused(db: Database) -> None:
    async with db.write() as session:
        await bind_account(session, 111)
    with pytest.raises(AccountMismatchError, match="DATA_DIR"):
        async with db.write() as session:
            await bind_account(session, 222)
    async with db.read() as session:
        assert await bound_account_id(session) == 111
