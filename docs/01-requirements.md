# 1-bosqich. Talablar tahlili va texnik cheklovlar

## 1.1. Maqsad va doira

| Rol | Kim | Vazifasi |
|---|---|---|
| **Asosiy akkaunt (A)** | Sizning Telegram akkauntingiz | Arxivlanadi. MTProto orqali, sizning session'ingiz bilan ishlaydi. |
| **Ikkinchi akkaunt (B)** | Sizning boshqa akkauntingiz | Faqat admin bot orqali arxivni boshqaradi. A akkaunt session'i unga **hech qachon** berilmaydi. |
| **Admin bot** | @BotFather'da yaratilgan bot | Faqat `OWNER_ID` (= B akkaunt ID'si) uchun ishlaydi. |

**Doira ichida:** A akkaunt ko‘ra oladigan bulutli (cloud) chatlar — shaxsiy chatlar, botlar bilan chatlar, Saved Messages, oddiy guruhlar, superguruhlar (forumlar bilan), kanallar.

**Doiradan tashqarida (ataylab):**

- **Secret chat'lar** — uchidan-uchiga shifrlangan, faqat qurilmada saqlanadi, bulut tarixi API orqali berilmaydi.
- **View-once media** (bir marta ko‘riladigan rasm/video) — yuboruvchi niyatiga hurmat sifatida hech qachon yuklanmaydi.
- Story'lar, rejalashtirilgan xabarlar, qo‘ng‘iroqlar.
- A akkaunt a’zo bo‘lmagan kanal/guruhlar, boshqa odamlarning akkauntlari.
- Autentifikatsiyani chetlab o‘tish, himoyalangan kontentni "ochish" kabi har qanday urinish.

## 1.2. Funksional talablar

| ID | Talab | Bosqich | Holat |
|---|---|---|---|
| FR-01 | A akkaunt kira oladigan dialoglarni aniqlash | 5–6 | — |
| FR-02 | Har bir chat tarixini sahifalab import qilish | 6 | — |
| FR-03 | Har bir chat uchun checkpoint, uzilishdan keyin davom ettirish | 6 | model ✅ (3) |
| FR-04 | Yangi kiruvchi/chiquvchi xabarlarni real vaqtda kuzatish | 7 | — |
| FR-05 | Tahrirlarni qayd etish, eski versiyalarni saqlash | 7 | model + mantiq ✅ (3) |
| FR-06 | Reply va forward metama’lumotlari, reply zanjiri | 5 | model + mantiq ✅ (3) |
| FR-07 | O‘chirilganlikni tasdiqlangan / ehtimoliy / aniqlanmagan holatlarga ajratish | 7 | model + mantiq ✅ (3) |
| FR-08 | Media arxivi: navbat, qayta urinish, checksum, disk nazorati | 8 | model ✅ (3) |
| FR-09 | Admin bot: `/start /search /chats /recent /stats /media /deleted /export /status /help` | 9 | — |
| FR-10 | Qidiruv: matn, chat, sana, muallif, reply, media turi, tahrirlangan, o‘chirilgan | 9 | FTS5 indeks ✅ (3) |
| FR-11 | Chat yoki vaqt oralig‘i bo‘yicha eksport | 10 | — |
| FR-12 | Shifrlangan zaxira va tiklash | 10 | — |
| FR-13 | Saqlash muddati (retention) va o‘chirish siyosati | 10 | sozlama + model ✅ (3–4) |
| FR-14 | Komponentlar holati (`/status`, healthcheck) | 7, 13 | jadval ✅ (3) |

## 1.3. Nofunksional talablar

| ID | Talab | Qanday ta’minlanadi |
|---|---|---|
| NFR-01 | 24/7 ishlash | Docker `restart: unless-stopped`, avtomatik qayta ulanish, heartbeat |
| NFR-02 | Xabar yo‘qolmasin, dublikat bo‘lmasin | "Avval tinglovchi" strategiyasi + `(chat_id, message_id)` noyobligi + idempotent saqlash ([02-architecture.md](02-architecture.md#5-sinxronlash-strategiyasi)) |
| NFR-03 | RAM nazorati | Tarix 100 talik sahifalarda, har sahifa alohida tranzaksiya; media to‘g‘ridan-to‘g‘ri diskka oqim bilan |
| NFR-04 | Telegram limitlariga rioya | FloodWait'ni kutish, sahifalar orasida pauza, chatlar ketma-ket ([telegram-limits.md](telegram-limits.md)) |
| NFR-05 | Xavfsizlik | Maxfiy fayllar 0600, bot jarayonida session yo‘q, loglarda maxfiy qiymatlar yashiriladi, shifrlangan zaxira |
| NFR-06 | Kuzatiluvchanlik | JSON structured log (structlog), audit jurnali |
| NFR-07 | Testlanuvchanlik | Saqlash qatlami Telethon'ga bog‘liq emas; unit testlar Telegram'siz ishlaydi |
| NFR-08 | Soddalik | Bitta VPS, SQLite, ikkita jarayon, tashqi navbat/kesh xizmatlarisiz |

## 1.4. Nima kafolatlanadi va nima kafolatlanmaydi

Bu bo‘lim ataylab ochiq yozilgan: Telegram API imkoniyatlari cheklangan va ularni "yashirib" bo‘lmaydi.

### Kafolatlanadi

- Arxivga bir marta tushgan xabar tizim tomonidan **o‘chirilmaydi** (faqat siz belgilagan retention siyosati bo‘yicha).
- Tahrir aniqlanganda eski matn `message_versions` jadvalida qoladi.
- O‘chirish hodisasi kelsa, yozuv o‘chirilmaydi — faqat holati yangilanadi va audit jurnaliga yoziladi.
- Bir xil xabar necha marta kelmasin, bazada bitta yozuv bo‘ladi.
- Import uzilsa, oxirgi muvaffaqiyatli sahifadan davom etadi; to‘liq tugamagan import "tugadi" deb belgilanmaydi.

### Kafolatlanmaydi (texnik jihatdan imkonsiz yoki Telegram'ga bog‘liq)

| Holat | Sabab |
|---|---|
| **Dastur ishga tushishidan oldin o‘chirilgan xabarlarni tiklash** | Telegram o‘chirilgan xabarni serverda hech kimga bermaydi. Tarix importi faqat hozir mavjud xabarlarni oladi. |
| O‘chirilgan xabar matnini Telegram'dan olish | O‘chirish hodisasida faqat ID'lar keladi. Matn faqat oldindan arxivlangan bo‘lsa bor. |
| Dastur o‘chiq paytdagi o‘chirishlar | Qayta ulanganda `getDifference` ularni yetkazishi mumkin, lekin uzilish uzun bo‘lsa (`differenceTooLong`) hodisalar yo‘qoladi. |
| Shaxsiy chat va oddiy guruhdagi o‘chirish qaysi chatga tegishliligi | Telegram bu hodisada chatni ko‘rsatmaydi; ID bo‘yicha aniqlanadi va **ehtimoliy** deb belgilanadi ([03-data-model.md](03-data-model.md#5-ochirilganlik-holatlari)). |
| Katta kanallardagi barcha real vaqt hodisalari | Telegram ba’zi kanallar uchun yangilanishlarni kamaytirishi mumkin. Yangi xabarlar davriy solishtirish bilan to‘ldiriladi, lekin o‘chirishlar sezilmay qolishi mumkin. |
| Arxivlashdan oldingi tahrirlar | Biz faqat xabarning hozirgi holatini va `edit_date` ni ko‘ramiz. |
| Avtomatik o‘chish taymeri (`ttl_period`) bilan o‘chgan xabarlar uchun hodisa | Server o‘chiradi; hodisa kelishi kafolatlanmagan. |
| Telegram API yoki foydalanish shartlari o‘zgarmasligi | Tashqi omil. |

## 1.5. Qabul qilingan taxminlar

Quyidagilar yashirin emas — barchasi `.env` orqali o‘zgartiriladi.

| # | Taxmin | Sukut qiymati | Sozlama |
|---|---|---|---|
| A1 | Bitta bazada **faqat bitta** asosiy akkaunt. Shaxsiy chat ID'larining noyobligi shunga tayanadi. | — | ikkinchi akkaunt uchun alohida `DATA_DIR` |
| A2 | Botlar bilan chatlar arxivlanmaydi (shovqin ko‘p) | `false` | `ARCHIVE_BOTS` |
| A3 | Media: shaxsiy chat va guruhlarda rasm, ovozli va video xabarlar, 20 MB gacha | `true`; `photo,voice,video_note` | `MEDIA_ENABLED`, `MEDIA_TYPES`, `MEDIA_MAX_FILE_MB` |
| A4 | Shaxsiy ma’lumotlar doimiy saqlanadi | bo‘sh | `RETENTION_DAYS` |
| A5 | Himoyalangan (noforwards) chatlarda matn arxivlanadi, media **hech qachon** yuklanmaydi (sozlab bo‘lmaydi) | — | — |
| A6 | Bazada vaqt UTC, botda Toshkent vaqti | `Asia/Tashkent` | `DISPLAY_TIMEZONE` |
| A7 | Shaxsiy chat va guruhlarda butun tarix import qilinadi | bo‘sh | `HISTORY_MAX_AGE_DAYS` |
| A8 | Takeout ishlatilmaydi | `false` | `HISTORY_USE_TAKEOUT` |
| A9 | VPS: 1–2 vCPU, 1–2 GB RAM, Python 3.12 | — | — |
| A10 | Kanallar: faqat ma’lumoti va oxirgi 20 ta post; postlarga izohlar har biri saqlanadi | `20`, `true` | `CHANNEL_KEEP_POSTS`, `CHANNEL_SAVE_COMMENTS` |

## 1.6. Egasi qarorlari (2026-10-25)

| Savol | Qaror | Qayerda qo‘llanadi |
|---|---|---|
| Archiver qanday ko‘rinadi | Asosiy akkauntda oddiy **ikkinchi sessiya** (qurilma nomi `tgarchive`); ma’lumot oddiy arxivga yoziladi | 5-bosqich, `client.py` |
| Takeout | Ishlatilmaydi. Kanallar faqat 20 ta post bo‘lgani uchun takeout'ning ommaviy eksport afzalligi kichik | `HISTORY_USE_TAKEOUT=false` |
| Kanallar | Kanal ma’lumoti + **oxirgi 20 ta post** (aylanma oyna: yangisi kelganda eng eskisi arxivdan chiqadi) | `policy.py` — `WINDOW` rejimi; 6–7-bosqich |
| Reply'lar | **Har biri saqlanadi**: (1) saqlangan postlarga yozilgan izohlar, (2) reply qilingan xabar — oyna tashqarisida bo‘lsa ham | 6–7-bosqich |
| Shaxsiy ma’lumotlar | **Doimiy** saqlanadi (retention yo‘q) | `RETENTION_DAYS` bo‘sh |
| Media | Shaxsiy chat va guruhlarda kichik shaxsiy media; kanallarda media yo‘q | A3, `MEDIA_IN_CHANNELS=false` |
| Deploy | Docker Compose | 12-bosqich |
| Zaxira | Kunlik, `age` bilan shifrlangan, `BACKUP_DIR` da 7 ta nusxa; tashqi nusxa ixtiyoriy | 10-bosqich |

Izohlar Telegram'da kanal emas, kanalga bog‘langan muhokama guruhida saqlanadi. Shuning uchun
arxivda ular o‘sha guruh chati ostida yoziladi va kanal oynasi ularga ta’sir qilmaydi.
Muhokama guruhida kanal postining nusxasi ham bor, shu sababli eski post oynadan chiqsa ham izohlar konteksti saqlanadi.

## 1.7. Huquqiy va axloqiy chegaralar

- Tizim faqat **o‘zingizga tegishli** akkaunt va siz a’zo bo‘lgan chatlar bilan ishlaydi.
- Arxivda suhbatdoshlaringizning shaxsiy ma’lumotlari ham bo‘ladi. Ularni himoyalash, kerak bo‘lsa retention orqali o‘chirish — egasining mas’uliyati.
- Himoyalangan kontent va view-once media cheklovlari hurmat qilinadi.
- Telegram API foydalanish shartlariga rioya qilinadi: o‘z `api_id` ingiz (my.telegram.org), sekin va bir maromdagi so‘rovlar. Agressiv avtomatlashtirish akkaunt cheklanishiga olib kelishi mumkin.

Batafsil Telegram cheklovlari: [telegram-limits.md](telegram-limits.md).
