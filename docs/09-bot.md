# 9-bosqich. Admin bot va qidiruv

## Maqsad

Ikkinchi akkauntingizdan Telegram bot orqali arxivni ko‘rish: qidiruv, chatlar, so‘nggi va
o‘chirilgan xabarlar, media, katta fayllarni yuklatish, statistika va holat.
Bot **faqat `OWNER_ID`** ga va **faqat u bilan shaxsiy chatda** javob beradi.

## Yaratilgan va o‘zgargan fayllar

| Fayl | Mazmuni |
|---|---|
| [db/search.py](../src/tgarchive/db/search.py) | Qidiruv sintaksisi, FTS + filtrlar, chatlar ro‘yxati, xabar tafsiloti, statistika |
| [bot/views.py](../src/tgarchive/bot/views.py) | Barcha ekranlar (matn + tugmalar) — Telegram'ga bog‘liq emas, alohida testlanadi |
| [bot/handlers.py](../src/tgarchive/bot/handlers.py) | Buyruqlar va inline tugmalar |
| [bot/middleware.py](../src/tgarchive/bot/middleware.py) | Faqat egasi: barcha update'lar uchun |
| [bot/format.py](../src/tgarchive/bot/format.py) | HTML-escape, vaqt mintaqasi, 4096 belgi chegarasi |
| [bot/callbacks.py](../src/tgarchive/bot/callbacks.py) | Tugma ma’lumotlari (≤ 64 bayt) |
| [bot/app.py](../src/tgarchive/bot/app.py) | Ishga tushirish, sxemani kutish, heartbeat |
| [mediafiles.py](../src/tgarchive/mediafiles.py) | Fayl yo‘llari (archiver va bot uchun umumiy, Telethon'siz) |
| `cli.py`, `docker-compose.yml`, `.env.example` | `tgarchive bot`, `bot` xizmati |
| tests: `test_search.py`, `test_bot.py`, `archive_fixture.py`, `fake_botapi.py` | 45 ta test, tarmoqsiz |

## O‘rnatish

### 1. Bot yaratish

Telegram'da **@BotFather**:

1. `/newbot` → nom va username → **token** (`123456789:AA...`) — uni `.env` dagi `BOT_TOKEN` ga yozing.
2. `/setjoingroups` → botingiz → **Disable** (botni guruhga qo‘shib bo‘lmasin).
3. Token hech kimga ko‘rsatilmasin. Sizib chiqsa: `/revoke` va yangi tokenni `.env` ga yozing.

### 2. OWNER_ID ni aniqlash

`OWNER_ID` — **ikkinchi** (boshqaruvchi) akkauntingizning raqamli ID'si. Uchinchi tomon botlarisiz ikki usul:

- **Archiver orqali:** ikkinchi akkaunt asosiy akkauntingizga kamida bir marta yozgan bo‘lsa, u bilan
  shaxsiy chatning ID'si — aynan ikkinchi akkauntning ID'si. Session'ni ikki jarayon bir vaqtda
  ishlatmasligi uchun archiver'ni qisqa to‘xtatib, dialoglarni ko‘ring:
  ```bash
  docker compose stop archiver
  docker compose run --rm archiver dialogs     # shu akkaunt qatoridagi CHAT ID (musbat raqam)
  docker compose up -d
  ```
- **Bot log'i orqali:** `OWNER_ID=1` qo‘yib botni ishga tushiring, ikkinchi akkauntdan botga `/start` yuboring.
  Bot javob bermaydi, lekin `docker compose logs bot | grep bot_unauthorized` da `user_id` ko‘rinadi —
  shu sizning ID'ingiz. Uni `OWNER_ID` ga yozib, `docker compose up -d` qiling.

### 3. Ishga tushirish

```bash
nano .env                       # BOT_TOKEN va OWNER_ID
docker compose up -d --build    # endi ikkita xizmat: archiver va bot
docker compose ps               # ikkalasi ham (healthy)
docker compose logs -f bot
```

Ikkinchi akkauntdan botga **`/start`** yuboring — menyu chiqadi va buyruqlar ro‘yxati faqat sizning chatingizda paydo bo‘ladi.

## Buyruqlar

| Buyruq | Nima qiladi |
|---|---|
| `/start` | Menyu (tugmalar bilan) |
| `/search matn [filtrlar]` | Qidiruv. **Oddiy matn yuborish ham qidiruv** |
| `/recent` | So‘nggi xabarlar (barcha chatlar) |
| `/chats` | Arxivdagi chatlar; chatni bosing → **suhbatni Telegram'dagidek o‘qiysiz** (pastda) |
| `/deleted` | O‘chirilganligi qayd etilgan xabarlar (o‘chirilish vaqti bo‘yicha) |
| `/media` | Media statistikasi, turlar bo‘yicha ro‘yxat, **📦 Katta fayllar**; `/media nom` — fayl nomi bo‘yicha |
| `/stats` | Arxiv statistikasi, eng faol chatlar, baza hajmi |
| `/status` | Archiver holati (real vaqt, import, media worker), disk, bot ishlash vaqti |
| `/help` | Buyruqlar va qidiruv sintaksisi |
| `/export` | Eksport (zip: HTML yoki JSON) — pastda |
| `/audit` | Audit jurnali va o‘chirish hodisalari |

Natijalar sahifalarga bo‘lingan (`BOT_PAGE_SIZE`, sukut 10): raqamli tugma → xabarni ochish, ◀️ ▶️ — sahifalar.

## Suhbatni o‘qish (Telegram'dagidek)

`/chats` dan chatni tanlaganingizda suhbat ochiladi: eski xabarlar tepada, yangilari pastda —
telefondagi chat kabi. Har bir kun **sana ajratkichi** bilan ajraladi, har xabarda yuboruvchi va
soat ko‘rinadi, media «🖼 rasm», javob «↳ #123», tahrir «✏️», o‘chirilgan «🗑» belgilari bilan.

- **⬆️ Eskiroq / ⬇️ Yangiroq** — oynani tarixga suradi yoki qaytaradi (bitta ekran o‘zgarib boradi).
- **① ② …** — xabarni to‘liq ochadi: butun matn, media («📎 Faylni olish»), javob zanjiri, versiyalar.
- **ℹ️ Chat haqida** — chat statistikasi; **🗑 O‘chirilgan** — shu chatning o‘chirilganlari; **💬 Chatlar** — ro‘yxatga qaytish.

Uzun xabarlar oynada qisqartiriladi — to‘lig‘ini ① tugmasi orqali ko‘rasiz.

## Qidiruv sintaksisi

```
/search shartnoma from:7 after:2026-09-01 has:media
```

| Filtr | Ma’nosi |
|---|---|
| `chat:-1001234` | Faqat shu chat (ID — `/chats` da) |
| `from:7` / `from:me` | Yuboruvchi ID / mening xabarlarim |
| `after:2026-01-01`, `before:2026-02-01`, `on:2026-01-15` | Sana (`DISPLAY_TIMEZONE`, sukut Toshkent, kun boshi bo‘yicha) |
| `type:photo` | Media turi: photo, video, voice, audio, document, animation, sticker, video_note |
| `has:media`, `has:reply` | Media bor / reply |
| `is:edited`, `is:deleted` | Tahrirlangan / o‘chirilgan |
| `name:hisobot` | Fayl nomida shu qism bor |

Matn — so‘z boshi bo‘yicha, barcha so‘zlar bo‘lishi shart; o‘zbekcha tutuq belgisining barcha shakllari
bir xil (`o'`, `o‘`, `oʻ`). Qidiruv xabarning **joriy** matni bo‘yicha; eski versiyalar xabar ichida
"✏️ Versiyalar" tugmasida.

## Xabarni ochganda

```
💬 Oila (guruh) · xabar #58211
👤 Ali (id 7) · 2026-10-21 18:02
↩️ Javob: Vali: ertaga soat nechada?
✏️ Tahrirlangan: 2 marta, oxirgi 2026-10-21 18:05
🗑 o'chirilgan (tasdiqlangan): 2026-10-22 09:14
📎 🎬 video: tugilgan_kun.mp4, 312.4 MB — katta fayl, yuklanmagan

matn...
[↩️ Asl xabar] [✏️ Versiyalar (2)] [⬇️ Yuklab olish] [💬 Chat]
```

- **📎 Faylni olish** — yuklangan faylni bot yuboradi (rasm — rasm, ovozli — ovozli; bo‘lmasa hujjat sifatida).
- **⬇️ Yuklab olish** — katta (yoki siyosat bo‘yicha yuklanmagan) faylni archiver navbatiga qo‘yadi (`audit_log` ga yoziladi). Himoyalangan va view-once media uchun tugma chiqmaydi.
- **📃 To‘liq matn** — 3900 belgidan uzun xabar bo‘laklarga bo‘linib yuboriladi.

## Xavfsizlik

| Choralar | Qanday |
|---|---|
| Faqat egasi | Har bir update uchun tekshiruv: foydalanuvchi = `OWNER_ID` **va** chat = egasining shaxsiy chati |
| Begonaga javob yo‘q | Bot borligini ham tasdiqlamaydi; urinish `audit_log` ga (**matnsiz**), har foydalanuvchiga 10 daqiqada bir marta |
| Guruhlar | Bot guruhga qo‘shilsa ham, u yerda egasi yozsa ham — javob yo‘q (arxiv boshqalarga ko‘rinmaydi) |
| Buyruqlar ro‘yxati | Faqat egasining chatida (`BotCommandScopeChat`) |
| Bot konteyneri | Asosiy akkaunt session'i ham, `TG_API_HASH` ham **yo‘q**; media volume — faqat o‘qish |
| Kiritma | Qidiruv matni FTS uchun qo‘shtirnoqqa olinadi, qolgani bound parametr — SQL injection yo‘q; barcha matn HTML-escape |
| Fayl yo‘li | Faqat `MEDIA_DIR` ichidan, qayta tekshiriladi |
| Bot token sizsa | Boshqa odam bot nomidan ish ko‘ra oladi, lekin arxivga kira olmaydi (javoblar faqat `OWNER_ID` ga). @BotFather → `/revoke` |

## Testlash

```bash
pytest tests/test_search.py tests/test_bot.py -v
```

Bot testlari haqiqiy Telegram'ga ulanmaydi: aiogram'ning o‘z `Dispatcher.feed_update` i orqali haqiqiy
update'lar beriladi, Bot API so‘rovlari esa yozib olinadi. Tekshiriladi: qidiruv sintaksisi va xatolari,
filtrlar, sahifalash, HTML-escape (`<b>` matn sifatida), 4096 chegarasi va to‘liq matn, reply/versiya/o‘chirish/
media ko‘rinishlari, katta faylni yuklatish, fayl yuborish va hujjatga o‘tish, har bir buyruq, begona
foydalanuvchi va guruh (javob yo‘q + audit, bir marta), eskirgan sahifa tugmasi, ichki xato xabari,
sxemani kutish, healthcheck. Egani tekshirish olib tashlansa yoki guruh tekshiruvi buzilsa — testlar
yiqilishi alohida tekshirilgan.

## Ma’lum cheklovlar

- Sahifalash tokenlari xotirada: bot qayta ishga tushsa, eski "▶️" tugmalar "eskirgan" deydi — qidiruvni qayta yuboring.
- Bot API orqali 50 MB dan katta fayl yuborilmaydi — bot faylning serverdagi yo‘lini ko‘rsatadi.
- `name:` filtri lotin harflarida katta-kichik harfni farqlamaydi, kirill harflarida farqlaydi (SQLite `LIKE`).
- Juda katta arxivda (o‘n millionlab xabar) `/chats` va `/stats` bir necha soniya o‘ylashi mumkin.
- Eksport — 10-bosqich.
