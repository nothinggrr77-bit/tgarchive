# 6-bosqich. Tarix importi va checkpoint mexanizmi

## Maqsad

Akkaunt kira oladigan barcha dialoglarni egasi qarorlariga muvofiq arxivlash:

| Chat turi | Nima saqlanadi |
|---|---|
| Shaxsiy chat, Saved Messages, guruh, superguruh | **Butun tarix**, doimiy |
| Kanal | Kanal ma’lumoti va **oxirgi 20 ta post** (aylanma oyna) |
| Kanal postlariga izohlar | **Har biri**, doimiy (muhokama guruhi chati ostida) |
| Reply qilingan xabar | Arxivda bo‘lmasa, Telegram'dan alohida olinadi (boshqa chatda yoki oynadan tashqarida bo‘lsa ham) |
| Bot bilan chat | Arxivlanmaydi (`ARCHIVE_BOTS=false`) |

Xizmat uzluksiz ishlaydi: `tgarchive run` (Docker'da avtomatik).

## Yaratilgan va o‘zgargan fayllar

| Fayl | Mazmuni |
|---|---|
| [archiver/history.py](../src/tgarchive/archiver/history.py) | `HistoryImporter`: rejalashtirish, backfill, oldinga sinxronlash, kanal oynasi, izohlar, reply nishonlari, xatolarni boshqarish |
| [archiver/service.py](../src/tgarchive/archiver/service.py) | `ArchiverService`: doimiy aylanish, heartbeat, qayta ulanish, session muammosi, toza to‘xtash |
| [archiver/dialogs.py](../src/tgarchive/archiver/dialogs.py) | `sync_dialogs`: chatlar va checkpoint'larni yangilash |
| [db/status.py](../src/tgarchive/db/status.py) | heartbeat, import statistikasi |
| [db/store.py](../src/tgarchive/db/store.py) | `prune_channel_window` |
| [migratsiya 0003](../src/tgarchive/migrations/versions/20261025_0003_history_sync.py) | yangi maydonlar va indeks |
| [cli.py](../src/tgarchive/cli.py) | `run`, `import`, `status`, `health` |
| [exitcodes.py](../src/tgarchive/exitcodes.py) | chiqish kodlari |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.gitattributes` | deploy — [deploy.md](deploy.md) |
| tests: `fake_telegram.py`, `test_history.py`, `test_service.py` | xotiradagi soxta Telegram va testlar |

## Ish turlari

Har bir aylanishda (tick) ishlar shu tartibda bajariladi — yangi xabarlar katta importni kutib qolmaydi:

1. **forward** — to‘liq chatlarda `(high_water_id, eng yangi]` oralig‘i eskidan yangiga (`min_id`, `reverse=True`). Har sahifadan keyin `high_water_id` oshadi.
2. **window** — kanallar: oxirgi 20 ta post, ularning izohlari, oynadan chiqqanlarni olib tashlash.
3. **backfill** — to‘liq chatlarda eng yangidan eskiga (`offset_id`), har chatga bir tick'da ko‘pi bilan 20 sahifa (2000 xabar), keyin navbat keyingi chatga o‘tadi.

Chatlar tartibi: Saved Messages → shaxsiy → guruh → superguruh → kanal.

```
tgarchive run:
  har SYNC_INTERVAL_S (10 daqiqa): dialoglar ro'yxati -> chats + chat_sync_state yangilanadi
  tick: forward ishlari -> window ishlari -> backfill bo'laklari
  ish qolmasa: keyingi sinxrongacha kutish
  har 30 s: heartbeat (holat, hozirgi ish)
```

## Checkpoint va "yo‘qolmaslik / takrorlanmaslik"

- Sahifadagi xabarlar va yangi cursor (`backfill_cursor_id` yoki `high_water_id`) **bitta tranzaksiyada** yoziladi. Jarayon istalgan joyda uzilsa — keyingi ishga tushishda aynan shu joydan davom etadi (test: `test_stop_during_import_keeps_checkpoint_and_resumes`).
- Chat birinchi ko‘rilganda `high_water_id` = o‘sha paytdagi eng yangi ID. Undan **eskisi** backfill bilan, **yangisi** forward bilan olinadi — oraliqda bo‘shliq qolmaydi. Ustma-ust tushgan xabarlar `(chat_id, message_id)` noyobligi tufayli takrorlanmaydi.
- Backfill `done` faqat Telegram **bo‘sh sahifa** qaytarganda (yoki `HISTORY_MAX_AGE_DAYS` chegarasida). Xato bo‘lsa holat `failed`, `next_retry_at` bilan.
- RAM: xotirada bir vaqtda bitta sahifa (≤100 xabar).

## Xatolar

| Holat | Nima bo‘ladi |
|---|---|
| `FloodWait X` | Faqat shu chat X sekundga qoldiriladi (urinish hisoblanmaydi), ishchi ko‘pi bilan `MAX_FLOOD_WAIT_S` kutadi — limit akkaunt bo‘yicha |
| Chatga kirish yo‘q (chiqib ketilgan, ban, maxfiy kanal) | Shu chat 24 soatga qoldiriladi, boshqalar davom etadi |
| Boshqa kutilmagan xato | Shu chat: 1, 2, 4, … daqiqadan keyin (6 soatgacha) qayta urinish |
| Tarmoq uzildi | Butun xizmat 10 s → 5 daqiqagacha oshib boruvchi pauza bilan qayta ulanadi; chat aybdor deb belgilanmaydi |
| Session bekor qilindi (Devices'dan o‘chirildi) | Holat `auth_error`, healthcheck `unhealthy`, xizmat Telegram'ga so‘rov yubormay kutadi — `tgarchive login` kerak |
| Baza boshqa akkauntga tegishli | `auth_error`, hech narsa yozilmaydi |
| `SIGTERM` (`docker compose stop`) | Joriy sahifa yozib bo‘linadi, toza to‘xtaydi |

## Kanal oynasi

- Har yangilanishda `get_messages(limit=20)` — butun kanal tarixi **o‘qilmaydi**.
- Oynadan chiqqan postlar arxivdan olib tashlanadi; `audit_log` ga faqat ID'lar yoziladi (`window_pruned`).
- **Istisno:** arxivdagi biror xabar (masalan, shaxsiy chatdagi) yoki oynadagi post reply qilgan postlar saqlanadi.
- Oyna ichida arxivda bor, lekin Telegram qaytarmagan post — **o‘chirilgan, tasdiqlangan** (`confirmed`, manba `verification`). Matn nusxasi qoladi.
- Yangi post bo‘lmasa ham oyna har `CHANNEL_REFRESH_INTERVAL_S` (1 soat) da tekshiriladi: yangi izohlar va o‘chirishlar uchun.

## Izohlar

- `post.replies.comments` bo‘lgan postlar uchun avval muhokama guruhidagi post nusxasi (`messages.getDiscussionMessage`), keyin izohlar (`iter_messages(reply_to=post)`).
- Muhokama guruhi `chats` jadvaliga `archive_mode = NULL` bilan yoziladi: uning to‘liq tarixi import **qilinmaydi**, faqat saqlangan postlarning izohlari.
- Keyingi safar faqat yangi izohlar olinadi (`comments_synced_id`).
- Izohlar ichidagi reply zanjiri (`izoh → izoh → post`) to‘liq tiklanadi.

## Buyruqlar

```bash
tgarchive import        # tarixni bir marta oxirigacha import qilib, statistikani chiqaradi
tgarchive run           # doimiy xizmat (Docker'da avtomatik)
tgarchive status        # holat va statistika
tgarchive health        # heartbeat yangi bo'lsa 0, aks holda 1 (Docker healthcheck)
```

Kutiladigan natija (`tgarchive status`):

```
Archiver: importing (oxirgi signal 12 s oldin, v0.1.0), hozir: backfill Oila
Chatlar (rejim): full: 41, skip: 6, window: 23, -: 4
Tarix importi (holat): done: 52, running: 1, pending: 11
Xabarlar: jami 184203, tarixdan import qilingan 183950, o'chirilgan deb belgilangan 3
```

`-: 4` — muhokama guruhlari va reply nishonlari olingan, lekin o‘zi arxivlanmaydigan chatlar.

## Testlash

```bash
pytest tests/test_history.py tests/test_service.py -v
```

Tekshiriladi: sahifalab import, uzilishdan keyin davom etish va "tugadi" deb noto‘g‘ri belgilamaslik, katta chatni bo‘laklab import, `HISTORY_MAX_AGE_DAYS`, `HISTORY_ENABLED=false`, oldinga sinxronlash va uning davom etishi, FloodWait, kirish yo‘q chat boshqalarni to‘smasligi, session bekor qilinishi, tarmoq uzilishi, kanal oynasini aylantirish, reply qilingan postni saqlash, o‘chirilgan postni tasdiqlash, izohlar va ularning zanjiri, a’zo bo‘lmagan chatdagi reply nishoni, rejalashtirish tartibi, toza to‘xtash.

Haqiqiy akkaunt bilan ixtiyoriy test: `TGARCHIVE_INTEGRATION=1 pytest tests/integration` (alohida test akkaunt bilan).

## Ma’lum cheklovlar

- Real vaqt hodisalari (yangi xabar darhol, tahrirlar, o‘chirishlar) — [07-realtime.md](07-realtime.md). Bu bosqichdagi so‘rovli sinxron endi o‘tkazib yuborilgan hodisalar uchun solishtirish vazifasini bajaradi.
- Tarix importi fayllarni o‘zi yuklamaydi — har bir fayl uchun navbat qatori yaratadi, uni media worker yuklaydi ([08-media.md](08-media.md)).
- Kanal oynasi yangi postlar har tekshiruvda 20 tadan ko‘p kelsa, oraliqdagi postlar umuman saqlanmaydi — bu "oxirgi 20 ta" qoidasining natijasi.
- Arxivdan chiqib ketgan (`left`) yoki o‘chirilgan chatlar dialoglar ro‘yxatidan yo‘qoladi; ularning oldin arxivlangan xabarlari qoladi.
- Izohlar ochiq muhokama guruhida bo‘lmasa (a’zolik talab qilinsa) — olinmaydi, log'ga `comments_unavailable` yoziladi.
