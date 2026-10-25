"""Fayl tizimi xavfsizligi: maxfiy kataloglar va ruxsatlarni tekshirish.

Linux'da jarayon boshida ``umask 077`` o'rnatiladi (cli.py), shunda yangi yaratilgan
session, baza va media fayllari faqat egasi uchun ochiq bo'ladi. Windows'da (lokal
ishlab chiqish) POSIX ruxsatlari qo'llanmaydi va tekshiruvlar o'tkazib yuboriladi.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

IS_POSIX = os.name == "posix"


def set_private_umask() -> None:
    if IS_POSIX:
        os.umask(0o077)


def ensure_private_dir(path: Path) -> Path:
    """Katalogni yaratadi va unga faqat egasi kira oladigan qiladi (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    if IS_POSIX:
        os.chmod(path, 0o700)
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
