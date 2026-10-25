"""``tgarchive doctor``: tarmoq so'rovlari soxta probe'lar bilan almashtiriladi."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tgarchive.cli import main
from tgarchive.config import ArchiverSettings, BotSettings
from tgarchive.db.engine import Database
from tgarchive.db.meta import bind_account
from tgarchive.db.status import ARCHIVER, beat
from tgarchive.db.types import utcnow
from tgarchive.doctor import Report, run_doctor
from tgarchive.exitcodes import EXIT_OK, EXIT_RUNTIME

VALID_HASH = "0123456789abcdef0123456789abcdef"
TOKEN = "123456789:" + "A" * 35
ACCOUNT = 111


def write_env(tmp_path: Path, **values: Any) -> Path:
    base = {"DATA_DIR": f"{tmp_path.as_posix()}/data"} | values
    env = tmp_path / "test.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in base.items()), encoding="utf-8")
    return env


FULL = {
    "TG_API_ID": "1",
    "TG_API_HASH": VALID_HASH,
    "BOT_TOKEN": TOKEN,
    "OWNER_ID": "424242",
    "BACKUP_AGE_RECIPIENTS": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
}


class Probes:
    def __init__(self, api=("ok", "Telegram tasdiqladi"), session=("ok", "Men (id 111)", ACCOUNT), bot=None) -> None:
        self.api, self.session = api, session
        self.bot = bot or [("BOT_TOKEN", "ok", "@arxiv_bot"), ("OWNER_ID", "ok", "424242 — /start yuborgan")]
        self.calls: list[str] = []

    async def api_probe(self, api_id: int, api_hash: str):
        self.calls.append("api")
        return self.api

    async def session_probe(self, settings: ArchiverSettings, workdir: Path):
        self.calls.append("session")
        assert workdir.exists() and workdir != settings.session_path.parent  # nusxa bilan ishlaydi
        return self.session

    async def bot_probe(self, settings: BotSettings):
        self.calls.append("bot")
        return self.bot

    def kwargs(self) -> dict[str, Any]:
        return {"api_probe": self.api_probe, "session_probe": self.session_probe, "bot_probe": self.bot_probe}


def prepare(tmp_path: Path, env: Path, *, session: bool = True, account: int | None = ACCOUNT) -> None:
    assert main(["--env-file", str(env), "db-upgrade"]) == EXIT_OK
    data = tmp_path / "data"
    if session:
        (data / "session").mkdir(parents=True, exist_ok=True)
        sqlite3.connect(data / "session" / "archiver.session").close()
    if account is not None:

        async def bind() -> None:
            db = Database(f"sqlite+aiosqlite:///{(data / 'db' / 'archive.sqlite3').as_posix()}")
            async with db.write() as s:
                await bind_account(s, account)
            await db.dispose()

        asyncio.run(bind())


def statuses(report: Report) -> dict[tuple[str, str], str]:
    return {(c.section, c.name): c.status for c in report.checks}


async def _run(env: Path, probes: Probes, **kwargs: Any) -> Report:
    return await run_doctor(env, **probes.kwargs(), **kwargs)


def test_everything_ok(tmp_path: Path) -> None:
    env = write_env(tmp_path, **FULL)
    prepare(tmp_path, env)
    report = asyncio.run(_run(env, Probes()))
    assert report.failures == 0 and report.warnings == 0, report.render()
    s = statuses(report)
    assert s[("archiver", "TG_API_ID/TG_API_HASH")] == "ok" and s[("archiver", "Session")] == "ok"
    assert s[("bot", "BOT_TOKEN")] == "ok" and s[("umumiy", "Baza")] == "ok"


def test_archiver_container_skips_bot_and_vice_versa(tmp_path: Path) -> None:
    archiver_env = write_env(tmp_path, TG_API_ID="1", TG_API_HASH=VALID_HASH, BOT_TOKEN="")
    prepare(tmp_path, archiver_env)
    probes = Probes()
    report = asyncio.run(_run(archiver_env, probes))
    assert statuses(report)[("bot", "Sozlamalar")] == "skip" and "bot" not in probes.calls

    bot_env = write_env(tmp_path, BOT_TOKEN=TOKEN, OWNER_ID="424242", TG_API_HASH="")
    probes = Probes()
    report = asyncio.run(_run(bot_env, probes))
    assert statuses(report)[("archiver", "Sozlamalar")] == "skip" and probes.calls == ["bot"]


def test_wrong_values_are_failures_with_hints(tmp_path: Path) -> None:
    env = write_env(tmp_path, **(FULL | {"OWNER_ID": "abc"}))
    prepare(tmp_path, env)
    probes = Probes(api=("fail", "Telegram rad etdi"), session=("fail", "session yaroqsiz", None))
    report = asyncio.run(_run(env, probes))
    s = statuses(report)
    assert s[("archiver", "TG_API_ID/TG_API_HASH")] == "fail" and s[("archiver", "Session")] == "fail"
    assert s[("bot", "Sozlamalar")] == "fail" and "OWNER_ID" in report.render()
    assert report.failures == 3


def test_missing_session_and_unencrypted_backup(tmp_path: Path) -> None:
    env = write_env(tmp_path, **(FULL | {"BACKUP_AGE_RECIPIENTS": ""}))
    prepare(tmp_path, env, session=False)
    probes = Probes()
    report = asyncio.run(_run(env, probes))
    s = statuses(report)
    assert s[("archiver", "Session")] == "fail" and "session" not in probes.calls
    assert s[("archiver", "Zaxira")] == "warn" and "login" in report.render()


def test_session_of_another_account(tmp_path: Path) -> None:
    env = write_env(tmp_path, **FULL)
    prepare(tmp_path, env, account=999)
    report = asyncio.run(_run(env, Probes()))
    assert statuses(report)[("archiver", "Session")] == "fail" and "999" in report.render()


def test_running_archiver_session_is_not_touched(tmp_path: Path) -> None:
    env = write_env(tmp_path, **FULL)
    prepare(tmp_path, env)

    async def running() -> None:
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'db' / 'archive.sqlite3').as_posix()}")
        async with db.write() as s:
            await beat(s, ARCHIVER, "idle", started_at=utcnow())
        await db.dispose()

    asyncio.run(running())
    probes = Probes()
    report = asyncio.run(_run(env, probes))
    assert "session" not in probes.calls
    assert statuses(report)[("archiver", "Session")] == "ok" and "ishlayapti" in report.render()


def test_owner_equal_to_archived_account_and_owner_not_started(tmp_path: Path) -> None:
    env = write_env(tmp_path, **(FULL | {"OWNER_ID": str(ACCOUNT)}))
    prepare(tmp_path, env)
    probes = Probes(bot=[("BOT_TOKEN", "ok", "@arxiv_bot"), ("OWNER_ID", "warn", "botga hali /start yubormagan")])
    report = asyncio.run(_run(env, probes))
    owner_checks = [c for c in report.checks if c.name == "OWNER_ID"]
    assert [c.status for c in owner_checks] == ["warn", "warn"]
    assert report.failures == 0


def test_offline_and_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = write_env(tmp_path, **FULL)
    prepare(tmp_path, env)
    assert main(["--env-file", str(env), "doctor", "--offline"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "➖ TG_API_ID/TG_API_HASH: --offline" in out and "Natija: 0 xato" in out

    bad = write_env(tmp_path, TG_API_ID="1", TG_API_HASH="noto'g'ri")
    assert main(["--env-file", str(bad), "doctor", "--offline", "--component", "archiver"]) == EXIT_RUNTIME
    assert "TG_API_HASH" in capsys.readouterr().out
