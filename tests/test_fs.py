"""Fayl tizimi: Docker'dagi faqat o'qiladigan /data (Linux xatti-harakati soxtalashtiriladi)."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from tgarchive import fs
from tgarchive.cli import _prepare_database
from tgarchive.config import BaseAppSettings


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Windows'da ham POSIX yo'lini sinash: chmod chaqiruvlari yozib olinadi."""
    calls: list[Path] = []
    monkeypatch.setattr(fs, "IS_POSIX", True)
    monkeypatch.setattr(fs.os, "chmod", lambda path, mode: calls.append(Path(path)))
    return calls


def test_correct_mode_is_not_touched(posix: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_mode", lambda path: 0o700)
    fs.ensure_private_dir(tmp_path / "a")
    assert posix == []  # ruxsat to'g'ri — chmod yo'q (read-only /data shu holat)


def test_wrong_mode_is_fixed(posix: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_mode", lambda path: 0o755)
    fs.ensure_private_dir(tmp_path / "b")
    assert posix == [tmp_path / "b"]


@pytest.mark.parametrize("code", [errno.EROFS, errno.EPERM])
def test_read_only_or_foreign_dir_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    monkeypatch.setattr(fs, "IS_POSIX", True)
    monkeypatch.setattr(fs, "_mode", lambda path: 0o755)

    def chmod(path, mode):
        raise OSError(code, "Read-only file system", str(path))

    monkeypatch.setattr(fs.os, "chmod", chmod)
    assert fs.ensure_private_dir(tmp_path / "c").is_dir()


def test_other_errors_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "IS_POSIX", True)
    monkeypatch.setattr(fs, "_mode", lambda path: 0o755)

    def chmod(path, mode):
        raise OSError(errno.EIO, "I/O error", str(path))

    monkeypatch.setattr(fs.os, "chmod", chmod)
    with pytest.raises(OSError):
        fs.ensure_private_dir(tmp_path / "d")


def test_login_and_run_prepare_database_on_read_only_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serverda topilgan xato: `login` va `run` /data ga chmod qilib, EROFS bilan yiqilardi."""
    data = tmp_path / "data"
    monkeypatch.setattr(fs, "IS_POSIX", True)
    monkeypatch.setattr(fs, "_mode", lambda path: 0o755)  # volume'larni ham "o'zgartirish kerak" deb ko'ramiz

    def chmod(path, mode):
        if Path(path) == data:
            raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(fs.os, "chmod", chmod)
    settings = BaseAppSettings(_env_file=None, data_dir=data)
    _prepare_database(settings)
    assert settings.paths.db.exists()
