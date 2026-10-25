# Yo‘l xaritasi va bosqichlar holati

| # | Bosqich | Holat | Asosiy natija |
|---|---|---|---|
| 1 | Talablar tahlili va texnik cheklovlar | ✅ | [01-requirements.md](01-requirements.md), [telegram-limits.md](telegram-limits.md) |
| 2 | Arxitektura va texnologiyalar tanlovi | ✅ | [02-architecture.md](02-architecture.md) |
| 3 | Papkalar tuzilishi va ma’lumotlar modeli | ✅ | modellar, migratsiya 0001, FTS5, saqlash mantiqi — [03-data-model.md](03-data-model.md) |
| 4 | Muhit va konfiguratsiya | ✅ | `config.py`, `.env.example`, structured logging, CLI, lock fayllar |
| 5 | Foydalanuvchi akkauntini ulash | ✅ | `login/whoami/dialogs/logout`, siyosat, Telethon adapter, akkauntga bog‘lash — [05-account.md](05-account.md) |
| 6 | Tarix importi va checkpoint | ✅ | backfill, oldinga sinxronlash, kanal oynasi (20 post), izohlar, reply nishonlari, FloodWait, per-chat retry, `run/import/status/health` — [06-history.md](06-history.md) |
| 7 | Real vaqt hodisalari | ✅ | yangi xabar / tahrir / o‘chirish handler'lari, navbat, `catch_up`, o‘chirishni tekshirish (`probable` → `confirmed` / `revoked`), kanal oynasini real vaqtda aylantirish — [07-realtime.md](07-realtime.md) |
| 8 | Media arxivi | ⏭ keyingi | navbat, siyosat, disk nazorati, checksum, yetishmayotgan fayllar |
| 9 | Admin bot va qidiruv | ⏳ | aiogram, OWNER_ID middleware, buyruqlar, filtrlar, pagination |
| 10 | Audit, eksport, backup | ⏳ | eksport (JSON/HTML), kunlik shifrlangan (age) zaxira, tiklash |
| 11 | Testlar va xatolarni tuzatish | ⏳ | integratsion testlar (alohida test akkaunt), xato ssenariylari |
| 12 | Deploy | 🟡 archiver tayyor | Dockerfile, docker-compose.yml, [deploy.md](deploy.md); bot xizmati 9-bosqichda qo‘shiladi |
| 13 | Monitoring, xavfsizlik auditi, yakuniy hujjatlar | ⏳ | ogohlantirishlar, xavfsizlik checklist (healthcheck allaqachon bor) |

Egasi qarorlari: [01-requirements.md, 1.6](01-requirements.md#16-egasi-qarorlari-2026-10-25).

## Tayyor fayllar (1–7-bosqich + deploy)

```
.gitignore  .gitattributes  .dockerignore  .env.example  pyproject.toml  requirements.txt  requirements-dev.txt
alembic.ini  README.md  Dockerfile  docker-compose.yml
docs/       00-roadmap.md  01-requirements.md  02-architecture.md  03-data-model.md  05-account.md  06-history.md
            07-realtime.md  deploy.md  telegram-limits.md
src/tgarchive/
  __init__.py  __main__.py  cli.py  config.py  exitcodes.py  fs.py  logging_setup.py  policy.py
  archiver/    __init__.py  auth.py  client.py  convert.py  dialogs.py  history.py  realtime.py  service.py
  db/          __init__.py  base.py  engine.py  enums.py  fts.py  meta.py  migrate.py  models.py  status.py
               store.py  types.py
  migrations/  __init__.py  env.py  script.py.mako
               versions/20261025_0001_initial_schema.py  versions/20261025_0002_account_binding.py
               versions/20261025_0003_history_sync.py
tests/
  conftest.py  factories.py  tl_objects.py  fake_telegram.py
  test_account_binding.py  test_auth.py  test_config.py  test_convert.py  test_dialogs.py  test_engine.py
  test_fts.py  test_history.py  test_logging.py  test_migrations.py  test_policy.py  test_realtime.py  test_service.py
  test_store_deletions.py  test_store_messages.py
  integration/test_live_account.py
```

## Ma’lum cheklovlar (hozirgi holat)

- Archiver o‘chiq paytidagi o‘chirish va tahrirlar faqat Telegram qayta ulanishda (`catch_up`) bergan bo‘lsa qayd etiladi; arxivga tushmasdan oldin o‘chirilgan xabarlarni tiklab bo‘lmaydi ([07-realtime.md](07-realtime.md)).
- Servis xabarlari (pin, qo‘shilish, qo‘ng‘iroq) real vaqtda emas, 10 daqiqalik sinxron bilan keladi.
- Media fayllar hali yuklanmaydi (8-bosqich) — navbat yig‘iladi.
- Qidiruv hozir faqat matn bo‘yicha (`fts.match_message_pks`); filtrlar 9-bosqichda.
- FTS'da stemming yo‘q, faqat so‘z boshi bo‘yicha qidiruv.
- Faqat SQLite qo‘llab-quvvatlanadi (PostgreSQL'ga o‘tish mezonlari — 02-architecture.md, 3.3).
- POSIX ruxsatlari (0700/0600, umask) faqat Linux'da qo‘llanadi va testlanadi.
- Docker image bu ishlab chiqish kompyuterida build qilinmagan (Docker yo‘q); birinchi haqiqiy build serverda bo‘ladi.
