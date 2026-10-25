"""Fayl tizimi xavfsizligi: maxfiy kataloglar va ruxsatlarni tekshirish.

Linux'da jarayon boshida ``umask 077`` o'rnatiladi (cli.py), shunda yangi yaratilgan
session, baza va media fayllari faqat egasi uchun ochiq bo'ladi. Windows'da (lokal
ishlab chiqish) POSIX ruxsatlari qo'llanmaydi va tekshiruvlar o'tkazib yuboriladi.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path

IS_POSIX = os.name == "posix"
PRIVATE_DIR_MODE = 0o700
# Docker'da /data — faqat o'qiladigan fayl tizimining qismi (volume'lar uning ichida); bunday
# katalogning ruxsatini o'zgartirib bo'lmaydi, lekin u image'da allaqachon 0700.
_UNCHANGEABLE = (errno.EROFS, errno.EPERM)


def set_private_umask() -> None:
    if IS_POSIX:
        os.umask(0o077)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def ensure_private_dir(path: Path) -> Path:
    """Katalogni yaratadi va unga faqat egasi kira oladigan qiladi (0700).

    Ruxsat allaqachon to'g'ri bo'lsa, ``chmod`` chaqirilmaydi. Katalog faqat o'qiladigan
    fayl tizimida yoki boshqa foydalanuvchiniki bo'lsa (EROFS/EPERM) — ishlash to'xtamaydi.
    """
    path.mkdir(parents=True, exist_ok=True)
    if IS_POSIX and _mode(path) != PRIVATE_DIR_MODE:
        try:
            os.chmod(path, PRIVATE_DIR_MODE)
        except OSError as exc:
            if exc.errno not in _UNCHANGEABLE:
                raise
    return path


def permission_warnings(paths: Iterable[Path]) -> list[str]:
    """Guruh yoki boshqa foydalanuvchilar o'qiy oladigan fayl/kataloglar haqida ogohlantirish."""
    if not IS_POSIX:
        return []
    warnings = []
    for path in paths:
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            warnings.append(f"{path}: ruxsatlar {oct(mode)} — faqat egasi uchun bo'lishi kerak (chmod go-rwx)")
    return warnings
