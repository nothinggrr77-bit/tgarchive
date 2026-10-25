"""age shifrlash (pyrage) uchun yordamchilar: kalitlarni o'qish va yaratish.

Zaxira **ochiq kalit** (recipient, ``age1...`` yoki ``ssh-ed25519 ...``) bilan shifrlanadi.
Maxfiy kalit (``AGE-SECRET-KEY-1...``) serverda saqlanmaydi — u faqat tiklashda beriladi.
Fayl formati standart: istalgan kompyuterda rasmiy ``age`` dasturi bilan ham ochiladi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pyrage import ssh, x25519

AGE_PREFIX = "age1"
SECRET_PREFIX = "AGE-SECRET-KEY-1"


class CryptoKeyError(ValueError):
    """Kalit noto'g'ri. Xabarda maxfiy kalitning o'zi bo'lmaydi."""


def parse_recipient(value: str) -> Any:
    value = value.strip()
    try:
        if value.startswith(AGE_PREFIX):
            return x25519.Recipient.from_str(value)
        if value.startswith("ssh-"):
            return ssh.Recipient.from_str(value)
    except Exception:
        pass
    raise CryptoKeyError(f"noto'g'ri age ochiq kaliti: {value[:12]}… (age1... yoki ssh-ed25519 ... bo'lishi kerak)")


def parse_recipients(values: list[str]) -> list[Any]:
    return [parse_recipient(v) for v in values if v.strip()]


def load_identities(path: Path) -> list[Any]:
    """age identity fayli (``age-keygen`` chiqaradigan format): ``#`` izohlar va AGE-SECRET-KEY-1 qatorlari."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CryptoKeyError(f"kalit faylini o'qib bo'lmadi: {path} ({exc.strerror})") from None
    return parse_identities(text, str(path))


def parse_identities(text: str, source: str = "stdin") -> list[Any]:
    identities = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(SECRET_PREFIX):
            raise CryptoKeyError(f"{source}: AGE-SECRET-KEY-1 bilan boshlanmaydigan qator bor")
        try:
            identities.append(x25519.Identity.from_str(line))
        except Exception:
            raise CryptoKeyError(f"{source}: maxfiy kalit buzilgan") from None
    if not identities:
        raise CryptoKeyError(f"{source}: maxfiy kalit topilmadi")
    return identities


def generate_keypair() -> tuple[str, str]:
    """(ochiq kalit, maxfiy kalit). Maxfiy kalitni serverda saqlamang."""
    identity = x25519.Identity.generate()
    return str(identity.to_public()), str(identity)
