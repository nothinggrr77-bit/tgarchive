# 5-bosqich. Foydalanuvchi akkauntini ulash

## Maqsad

Asosiy akkauntni xavfsiz ulash, uning dialoglarini aniqlash va har bir chatga arxivlash siyosatini
qo‘llash. Tarix importi (6-bosqich) va real vaqt (7-bosqich) shu qatlam ustiga quriladi.

Natijada:

- `tgarchive login` — bir martalik interaktiv kirish (telefon, kod, 2FA). Archiver akkauntingizda
  oddiy **ikkinchi sessiya** sifatida ko‘rinadi (*Settings → Devices*, qurilma nomi `tgarchive`).
- Baza bitta akkauntga **bog‘lanadi** (`app_meta.account_id`): boshqa akkaunt bilan ishlatib bo‘lmaydi.
- `tgarchive dialogs` — barcha dialoglar (Archive papkasi ham) va ularga qo‘llanadigan siyosat.
- Telethon → `MessageRecord` / `ChatInfo` adapteri tayyor: 6–7-bosqichlar uni to‘g‘ridan-to‘g‘ri ishlatadi.

## Yaratilgan va o‘zgargan fayllar

| Fayl | Vazifa |
|---|---|
| [archiver/client.py](../src/tgarchive/archiver/client.py) | `TelegramClient` yaratish: qurilma nomi, FloodWait chegarasi, qayta ulanish; session fayli 0600 |
| [archiver/auth.py](../src/tgarchive/archiver/auth.py) | Interaktiv kirish (kod, 2FA, urinishlar soni), `logout` |
| [archiver/convert.py](../src/tgarchive/archiver/convert.py) | Telethon TL → `ChatInfo`, `MessageRecord`, `MediaInfo` (yagona Telethon'ni biladigan joy) |
| [archiver/dialogs.py](../src/tgarchive/archiver/dialogs.py) | Dialoglarni o‘qish, siyosatni hisoblash, bazaga yozish, jadval chiqarish |
| [policy.py](../src/tgarchive/policy.py) | Chat bo‘yicha siyosat: `FULL` / `WINDOW` (kanal: oxirgi N post) / `SKIP`, media qarori |
| [db/meta.py](../src/tgarchive/db/meta.py) | `app_meta` kalit-qiymat, bazani akkauntga bog‘lash |
| [migratsiya 0002](../src/tgarchive/migrations/versions/20261025_0002_account_binding.py) | `app_meta` jadvali, `chats.archive_mode` ustuni |
| [cli.py](../src/tgarchive/cli.py) | `login`, `whoami`, `dialogs [--save] [--limit N]`, `logout [--yes]` |
| [config.py](../src/tgarchive/config.py) | `CHANNEL_KEEP_POSTS`, `CHANNEL_SAVE_COMMENTS`, `MEDIA_IN_CHANNELS`; media sukut qiymatlari |
| tests | `test_convert.py`, `test_policy.py`, `test_auth.py`, `test_dialogs.py`, `test_account_binding.py`, `tl_objects.py`, `integration/test_live_account.py` |

## O‘rnatish

4-bosqichdagi o‘rnatish yetarli, yangi bog‘liqlik yo‘q. Yangilangan kodni o‘rnatgandan keyin:

```bash
tgarchive db-upgrade        # 0001 -> 0002
```

`.env` da `TG_API_ID` va `TG_API_HASH` bo‘lishi shart (my.telegram.org → *API development tools*).

## Ishga tushirish va kutiladigan natija

### 1. Kirish

```bash
tgarchive login
```

```
Telefon raqam (+998...): +998901234567
Kod Telegram ilovangizga yuborildi. Uni hech kimga yubormang va forward qilmang.
Telegram'dan kelgan kod: 12345
Akkauntda ikki bosqichli himoya (2FA) yoqilgan.
2FA parol (ekranda ko'rinmaydi):
Yangi session yaratildi: Ism Familiya (id=123456789, @username)
Session fayli: /.../data/session/archiver.session
Telegram -> Settings -> Devices ro'yxatida 'tgarchive' qurilmasi ko'rinadi.
```

Qayta ishga tushirilsa, kod so‘ralmaydi: `Session allaqachon faol: ...`.

Docker'da (12-bosqich): `docker compose run --rm archiver tgarchive login`.

### 2. Tekshirish

```bash
tgarchive whoami
```

```
Akkaunt: Ism Familiya (id=123456789, @username)
Baza:    shu akkauntga bog'langan
```

### 3. Dialoglar va siyosat

```bash
tgarchive dialogs           # faqat ko'rish
tgarchive dialogs --save    # chatlarni bazaga yozish (6-bosqich shu ro'yxatdan ishlaydi)
```

```
REJIM     TUR                CHAT ID  NOM                               SABAB / MEDIA
full      private                  7  Ali                               to'liq, doimiy; media: ha (photo,video_note,voice)
full      saved                  111  Saved Messages                    to'liq, doimiy; media: ha (photo,video_note,voice)
skip      bot                      9  Helper                            ARCHIVE_BOTS=false; media: yo'q (arxivlanmaydi)
full      group                 -555  Oila                              to'liq, doimiy; media: ha (photo,video_note,voice) [Archive]
window20  channel     -1001234567890  Yangiliklar                       kanal: oxirgi 20 ta post; media: yo'q (MEDIA_IN_CHANNELS=false)
Jami: 5 | to'liq: 3 | oxirgi postlar: 1 | o'tkazib yuboriladi: 1
```

### 4. Chiqish (session'ni bekor qilish)

```bash
tgarchive logout            # 'ha' deb tasdiqlash so'raladi; --yes bilan so'ralmaydi
```

Session Telegram serverida tugatiladi va fayl o‘chiriladi. Arxiv bazasi saqlanib qoladi va shu akkauntga bog‘langanicha qoladi.

### Chiqish kodlari

| Kod | Ma’nosi |
|---|---|
| 0 | muvaffaqiyat |
| 1 | ish vaqtidagi muammo (masalan, baza sxemasi eski) |
| 2 | konfiguratsiya xatosi |
| 3 | avtorizatsiya: session yo‘q, bekor qilingan yoki baza boshqa akkauntga tegishli |

## Siyosat qoidalari

Ustuvorlik (yuqoridagisi kuchliroq):

1. `CHAT_DENYLIST` — mutlaq.
2. Egasining per-chat qarori (`chats.archive_override`, 9-bosqichda bot orqali).
3. `CHAT_ALLOWLIST` (bo‘sh bo‘lmasa) va chat turi sozlamalari (`ARCHIVE_PRIVATE`, `ARCHIVE_GROUPS`, ...).

| Chat turi | Rejim | Media |
|---|---|---|
| Shaxsiy, Saved Messages, guruh, superguruh | `full` — butun tarix, doimiy | ha (`MEDIA_TYPES`) |
| Kanal | `window20` — kanal ma’lumoti + oxirgi 20 ta post; izohlar har biri | yo‘q (`MEDIA_IN_CHANNELS=false`) |
| Bot bilan chat | `skip` (`ARCHIVE_BOTS=false`) | — |
| Himoyalangan (noforwards) har qanday chat | turiga qarab | **hech qachon** |

## Adapter qoidalari (Telethon → MessageRecord)

| Holat | Qanday saqlanadi |
|---|---|
| Kiruvchi shaxsiy xabar | Telegram `from_id` bermaydi → yuboruvchi = suhbatdosh |
| Chiquvchi shaxsiy xabar | yuboruvchi = sizning akkaunt ID'ingiz |
| Kanal posti | yuboruvchi = kanalning o‘zi |
| Media caption | `text` (Telegram'da caption = xabar matni) |
| Forum mavzusidagi oddiy xabar | `reply_to_top_id` = mavzu, `reply_to_msg_id` bo‘sh (bu reply emas) |
| Forum ichidagi reply | `reply_to_msg_id` + `reply_to_top_id` |
| Boshqa chatdagi xabarga reply | `reply_to_peer_id` saqlanadi |
| Story'ga javob | reply hisoblanmaydi |
| Yashirin forward | faqat `fwd_from_name` |
| Servis xabar | `action` (masalan `chat_add_user`), matn yo‘q |
| Formatlash | `entities`: `[{"type": "text_url", "offset": 0, "length": 5, "url": "..."}]` |
| Rasm | eng katta o‘lcham bo‘yicha `width/height/size` |
| Hujjat turlari | video xabar → ovozli → stiker → animatsiya → video → audio → hujjat (shu tartibda tekshiriladi) |
| View-once | `ttl_seconds` saqlanadi (media worker uni hech qachon yuklamaydi) |

## Testlash

```bash
pytest                                   # 147 ta unit test, Telegram'siz
TGARCHIVE_INTEGRATION=1 pytest -m integration   # ixtiyoriy, haqiqiy session bilan FAQAT O'QISH
```

Unit testlar haqiqiy Telethon TL obyektlarini tarmoqsiz yasaydi ([tests/tl_objects.py](../tests/tl_objects.py))
va kirish oqimini soxta client bilan tekshiradi: noto‘g‘ri kod, 2FA, urinishlar tugashi, FloodWait,
bot akkaunt rad etilishi, telefon/kod/parol loglarga tushmasligi.

Integratsion test uchun alohida test akkaunt va alohida `.env.test` (`DATA_DIR` boshqa) tavsiya etiladi.

## Ma’lum cheklovlar

- `login` ni avtomatik test qilib bo‘lmaydi (haqiqiy telefon kodi kerak). Oqim mantiqi soxta client bilan to‘liq testlangan.
- Kod Telegram ilovasiga keladi. Uni boshqa chatga forward qilsangiz, Telegram xavfsizlik uchun kirishni bloklaydi.
- Session fayli shifrlanmagan (Telethon SQLite). Himoya — fayl ruxsatlari (0600, katalog 0700) va konteyner izolyatsiyasi. Shubha bo‘lsa: `tgarchive logout` yoki *Settings → Devices*.
- Linux'ga xos ruxsat testlari (`test_session_file_is_private`) Windows'da o‘tkazib yuboriladi va VPS'da ishlaydi.
- `dialogs` siyosatni ko‘rsatadi, lekin hali hech narsa import qilmaydi — bu 6-bosqich.
- Muhokama guruhi (izohlar) ma’lumotlari dialoglar ro‘yxatidan olinmaydi; ular 6-bosqichda har bir kanal uchun alohida so‘raladi.
