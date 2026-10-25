# Telegram Personal Archive (`tgarchive`)

O‘zingizga tegishli Telegram akkauntidagi mavjud va yangi xabarlarni arxivlaydigan, ikkinchi
akkauntingiz orqali admin bot bilan boshqariladigan, Linux VPS'da 24/7 ishlaydigan tizim.

- **archiver** — asosiy akkaunt bilan MTProto (Telethon) orqali ishlaydi: tarix importi, real vaqt, tahrirlar, o‘chirishlar, media.
- **bot** — aiogram 3 admin bot; faqat `OWNER_ID` uchun. Bot jarayonida asosiy akkaunt session'i **yo‘q**.

> **Holat:** 1–6-bosqichlar tayyor va archiver serverda doimiy ishlay oladi (Docker Compose).
> Tarix importi ishlaydi; yangi xabarlar har 10 daqiqada olinadi. Real vaqt hodisalari, tahrirlar
> va o‘chirishlarni kuzatish — 7-bosqich. Batafsil: [docs/00-roadmap.md](docs/00-roadmap.md).
>
> Arxivlash siyosati: shaxsiy chat va guruhlar — to‘liq va doimiy; kanallar — ma’lumoti va
> oxirgi 20 ta post; postlarga izohlar (reply'lar) — har biri saqlanadi.

## Serverga o‘rnatish (tavsiya etilgan yo‘l)

To‘liq yo‘riqnoma: **[docs/deploy.md](docs/deploy.md)** (GitHub → server → doimiy ishlash). Qisqasi:

```bash
git clone git@github-tgarchive:<LOGIN>/tgarchive.git ~/tgarchive && cd ~/tgarchive
cp .env.example .env && chmod 600 .env && nano .env      # TG_API_ID, TG_API_HASH
docker compose build
docker compose run --rm archiver login                   # bir marta
docker compose up -d                                     # doimiy, server qayta yoqilganda ham
docker compose exec archiver python -m tgarchive status
```

## Hujjatlar

| Hujjat | Mazmuni |
|---|---|
| [deploy.md](docs/deploy.md) | GitHub, server, Docker, yangilash, zaxira, muammolar |
| [01-requirements.md](docs/01-requirements.md) | Talablar, kafolatlanadigan va kafolatlanmaydigan narsalar, taxminlar, egasi qarorlari |
| [telegram-limits.md](docs/telegram-limits.md) | Telegram API cheklovlari ro‘yxati (chat turlari bo‘yicha) |
| [02-architecture.md](docs/02-architecture.md) | Diagramma, stack tanlovi, SQLite vs PostgreSQL, sinxronlash strategiyasi |
| [03-data-model.md](docs/03-data-model.md) | Jadvallar, versiyalash, o‘chirilganlik holatlari, FTS, indekslar |
| [05-account.md](docs/05-account.md) | 5-bosqich: akkauntni ulash, dialoglar, adapter |
| [06-history.md](docs/06-history.md) | 6-bosqich: tarix importi, checkpoint, kanal oynasi, izohlar, xizmat |

## Lokal o‘rnatish (Docker'siz)

Talab: Python 3.12.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.txt
pip install --no-deps -e .

cp .env.example .env
chmod 600 .env
# .env ni tahrirlang: TG_API_ID, TG_API_HASH (my.telegram.org)
```

Windows (faqat ishlab chiqish va testlar uchun, PowerShell). Bu papka OneDrive ichida bo‘lsa,
haqiqiy `.env` va session'ni bu yerda yaratmang — ular bulutga sinxronlanadi.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements-dev.txt
pip install --no-deps -e .
```

## Buyruqlar

```bash
tgarchive check-config [--component archiver|bot]   # sozlamalarni tekshirish (maxfiy qiymatlar ko'rsatilmaydi)
tgarchive db-upgrade              # bazani yaratish / migratsiyalarni qo'llash
tgarchive db-info                 # baza holati: SQLite, WAL, revision, akkaunt, jadval hajmlari

tgarchive login                   # asosiy akkauntga bir martalik kirish (telefon, kod, 2FA)
tgarchive whoami                  # session qaysi akkauntga tegishli
tgarchive dialogs [--save]        # dialoglar va har biriga qo'llanadigan siyosat
tgarchive logout                  # session'ni Telegram'da tugatish va faylini o'chirish

tgarchive run                     # doimiy xizmat (Docker'da avtomatik)
tgarchive import                  # tarixni bir marta oxirigacha import qilish
tgarchive status                  # holat va import statistikasi
tgarchive health                  # healthcheck: 0 — sog'lom, 1 — nosoz
```

`tgarchive` o‘rniga `python -m tgarchive` ham ishlaydi. Boshqa `.env` fayl: `tgarchive --env-file /yo'l/.env ...`.
Chiqish kodlari: 0 — muvaffaqiyat, 1 — ish vaqtidagi muammo, 2 — konfiguratsiya xatosi, 3 — avtorizatsiya muammosi.

## Testlar

Unit testlar haqiqiy Telegram akkauntiga **ulanmaydi** (xotiradagi soxta Telegram ishlatiladi).

```bash
pip install --require-hashes -r requirements-dev.txt
pip install --no-deps -e .
pytest
```

Integratsion testlar `@pytest.mark.integration` bilan belgilangan va faqat `TGARCHIVE_INTEGRATION=1`
bo‘lganda ishlaydi. Ular uchun **alohida test akkaunt** ishlating.

## Bog‘liqliklarni yangilash

To‘g‘ridan-to‘g‘ri bog‘liqliklar [pyproject.toml](pyproject.toml) da aniq versiyada. Lock fayllarni qayta yaratish:

```bash
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.txt
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.txt
```

Yangilagandan keyin `pytest` ni albatta ishga tushiring. Eslatma: `aiogram 3.31.0` `pydantic<2.14` talab qiladi.

## Xavfsizlik — hozirdanoq

- GitHub repozitoriysi **Private** bo‘lsin. `.env` va `data/` Git'ga qo‘shilmaydi ([.gitignore](.gitignore)), Docker image'ga ham tushmaydi ([.dockerignore](.dockerignore)).
- **Session fayli = akkauntingizga to‘liq kirish.** Uni hech kimga yubormang, bot orqali ham. O‘g‘irlik shubhasi bo‘lsa: Telegram → *Settings → Devices* → session'ni tugating.
- Konteyner root'siz, faqat o‘qiladigan fayl tizimi bilan, barcha Linux capability'larisiz ishlaydi; hech qanday port ochilmaydi.
- Dastur `umask 077` bilan ishlaydi: yangi fayllarni faqat egasi o‘qiy oladi.
- Loglarda `TG_API_HASH`, `BOT_TOKEN` va token ko‘rinishidagi qatorlar avtomatik `***` bilan almashtiriladi.

## Papka tuzilishi

```
src/tgarchive/              ilova kodi (config, logging, CLI, policy)
src/tgarchive/archiver/     Telethon: client, login, adapter, dialoglar, tarix importi, xizmat
src/tgarchive/db/           modellar, saqlash mantiqi, FTS qidiruv, holat
src/tgarchive/migrations/   Alembic migratsiyalari
tests/                      pytest testlari
docs/                       loyiha hujjatlari
Dockerfile, docker-compose.yml   deploy
data/                       lokal ish ma'lumotlari (Git'da emas); Docker'da named volume'lar
```
