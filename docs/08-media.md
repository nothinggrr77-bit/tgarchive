# 8-bosqich. Media arxivi

## Maqsad

Shaxsiy chat va guruhlardagi fayllarni (rasm, video, ovozli va video xabar, audio, hujjat, GIF)
diskka saqlash. Har bir fayl uchun chegara — **50 MB**. Kattaroq fayl yuklanmaydi, lekin arxivda
**"katta fayl"** sifatida ko‘rinib turadi: nomi, hajmi, turi, qaysi chat va xabarda ekani.
Kerakli bittasini egasi baribir yuklatishi mumkin.

## Yaratilgan va o‘zgargan fayllar

| Fayl | Mazmuni |
|---|---|
| [archiver/media.py](../src/tgarchive/archiver/media.py) | `MediaWorker`: tasnif, takror, disk nazorati, yuklash, qayta urinish, tekshiruv, axlat |
| [db/media.py](../src/tgarchive/db/media.py) | statistika, katta fayllar ro‘yxati, majburiy yuklash so‘rovi (CLI va 9-bosqichdagi bot uchun) |
| [db/store.py](../src/tgarchive/db/store.py) | `delete_messages`: o‘chirilgan xabarning fayli `media_trash` ga |
| [migratsiya 0004](../src/tgarchive/migrations/versions/20261025_0004_media_archive.py) | `media.skip_reason`, `media.force_download`, `media_trash` jadvali |
| [archiver/service.py](../src/tgarchive/archiver/service.py) | media worker doimiy rejimda fon vazifasi sifatida |
| [cli.py](../src/tgarchive/cli.py) | `tgarchive media stats / large / fetch / verify`; `status` da media qatori |
| `config.py`, `.env.example` | `MEDIA_MAX_FILE_MB=50`, fayl turlari |
| [tests/test_media.py](../tests/test_media.py) | 18 ta test |

## Nima yuklanadi

| Holat | Natija | `skip_reason` |
|---|---|---|
| Shaxsiy chat / guruh, tur `MEDIA_TYPES` da, ≤ 50 MB | ✅ yuklanadi | — |
| > 50 MB | ❌ yuklanmaydi, **katta fayl** sifatida ro‘yxatda | `too_large` |
| Stickerlar (sukut bo‘yicha) | ❌ | `type` |
| Kanal postlari (`MEDIA_IN_CHANNELS=false`) | ❌ | `policy` |
| Himoyalangan kontent (noforwards) | ❌ **hech qachon**, `fetch` bilan ham | `protected` |
| Bir marta ko‘riladigan (view-once) | ❌ **hech qachon** | `view_once` |

Sozlama o‘zgarsa (masalan, `MEDIA_MAX_FILE_MB=100` yoki `MEDIA_TYPES` ga `sticker` qo‘shilsa), keyingi
ishga tushishda `too_large` / `type` / `policy` sabablilar avtomatik qayta ko‘rib chiqiladi.

## Katta fayllar

```bash
docker compose exec archiver python -m tgarchive media large
```

```
     ID  SANA (UTC)             HAJM  TUR         NOM — CHAT
   1834  2026-10-21 18:02   312.4 MB  video       tugilgan_kun.mp4 — Oila (xabar 58211)
   1790  2026-10-19 09:40    74.0 MB  document    loyiha.zip — Ali (xabar 4410)
Bittasini yuklash: tgarchive media fetch <ID>
```

```bash
docker compose exec archiver python -m tgarchive media fetch 1790
# Media 1790 navbatga qo'yildi — archiver uni yaqin daqiqalarda yuklaydi.
```

`fetch` hajm, tur va siyosat chegarasini yengadi, lekin himoyalangan va view-once mediani **yengmaydi**.
Har bir so‘rov `audit_log` ga yoziladi. Agar fayl Telegram'ning o‘zida 2 GB dan katta bo‘lsa yoki disk
yetmasa, u navbatda kutib turadi.

## Qanday ishlaydi

```
tarix importi / real vaqt ──> media qatori: pending
                                   │
                     media worker (har 30 s, eng yangisidan, 20 tadan)
                                   │
        tasnif ── yuklanmaydi ──> skipped + skip_reason (katta fayl ham shu yerda)
           │
     takror? ── ha ──> done (oldingi faylga havola, disk sarflanmaydi)
           │
     disk yetarlimi? ── yo'q ──> to'xtatiladi, navbat saqlanadi
           │
     xabarni Telegram'dan qayta olish -> .tmp ga oqim bilan -> sha256 -> atomik rename -> done
           │
     xato ──> 1 daq / 5 daq / 30 daq / 2 soat / 12 soat, 5 urinishdan keyin failed
```

- **Navbat** — `media` jadvalining o‘zi (`status`, `next_attempt_at`). Alohida navbat xizmati kerak emas.
- **Fayl yo‘li** faqat raqamlardan va tozalangan kengaytmadan yasaladi: `MEDIA_DIR/<chat_id>/<YYYY-MM>/<message_id>-<media_id>.<ext>`. Fayl nomidagi `../` kabi qismlar yo‘lga hech qachon tushmaydi; o‘qishda yo‘l `MEDIA_DIR` ichida ekanligi qayta tekshiriladi.
- **Bayt bazaga yozilmaydi** — bazada nisbiy yo‘l, hajm, MIME, sha256 va Telegram identifikatorlari.
- **Yuklash paytida** hajm 50 MB dan oshsa (Telegram aytgan hajm noto‘g‘ri bo‘lsa) — yuklash to‘xtatiladi, fayl o‘chiriladi, `too_large`.
- **Takror:** forward qilingan yoki qayta yuborilgan fayl (bir xil Telegram ID) bir marta yuklanadi va qolgan xabarlar shu faylga havola qiladi.
- **Uzilish:** yarim yuklangan fayl faqat `.tmp` da bo‘ladi; keyingi ishga tushishda tozalanadi va qayta yuklanadi.
- **RAM:** fayl xotirada to‘planmaydi (Telethon diskka bo‘laklab yozadi, sha256 1 MB bo‘laklar bilan), bir vaqtda ko‘pi bilan `MEDIA_CONCURRENCY` (2) ta yuklash.
- **Bir martalik `tgarchive import`** fayl yuklamaydi — bu faqat doimiy xizmatning (`run`) ishi.

## Disk nazorati

| Sozlama | Sukut | Ma’nosi |
|---|---|---|
| `MEDIA_MIN_FREE_GB` | 5 | Shundan kam bo‘sh joy qolsa — yuklash to‘xtaydi |
| `MEDIA_MAX_TOTAL_GB` | bo‘sh | Barcha media uchun umumiy chegara (ixtiyoriy) |

To‘xtaganda navbat yo‘qolmaydi, worker har 10 daqiqada qayta tekshiradi; `status` da
`Media worker: paused` va sababi ko‘rinadi. Joy bo‘shatilgach yoki chegara oshirilgach — o‘zi davom etadi.

## Yetishmayotgan va buzilgan fayllar

- Har ishga tushishda tez tekshiruv: har bir yuklangan fayl diskda bormi va hajmi mosmi.
- To‘liq tekshiruv (sha256): `tgarchive media verify --checksum`.
- Topilmagan yoki buzilgan fayl `missing` bo‘ladi va avtomatik qayta yuklanadi (Telegram'da hali bo‘lsa).

## Fayllarni o‘chirish

Kanal oynasidan chiqqan post (yoki keyinroq retention) arxivdan olib tashlanganda, uning fayli **shu
tranzaksiyada** `media_trash` ga yoziladi va media worker uni o‘chiradi — agar o‘sha faylni boshqa xabar
ishlatmasa. Faqat shu aniq ro‘yxatdagi fayllar o‘chiriladi: bazani eski zaxiradan tiklash diskdagi
"ortiqcha" fayllarni o‘chirib yubormaydi.

## Ruxsatsiz kirishdan himoya

- Fayllar `0600`, kataloglar `0700` (`umask 077`), egasi konteynerning root bo‘lmagan foydalanuvchisi.
- Media hech qanday port orqali berilmaydi; 9-bosqichdagi bot ularni faqat `OWNER_ID` ga yuboradi.
- Docker'da `media` volume'i alohida; bot uni faqat o‘qish uchun oladi.

## Buyruqlar

```bash
tgarchive media stats                 # yuklangan / navbatda / katta / xato, disk
tgarchive media large [--limit 20] [--offset 0]
tgarchive media fetch <ID>
tgarchive media verify [--checksum]
tgarchive status                      # umumiy holat, media qatori bilan
```

Kutiladigan `status` qismi:

```
Media worker: downloading, hozir: media 5121
Media: yuklangan 18234 (6.2 GB), navbatda 412, katta fayllar (> 50 MB) 37 (4.8 GB), xato 3, Telegram'da yo'q 12
  Yuklanmaydi: policy: 2210, protected: 14, type: 980, view_once: 2
Disk (MEDIA_DIR): bo'sh 31.4 GB / 79.0 GB
```

## Testlash

```bash
pytest tests/test_media.py -v
```

Tekshiriladi: tasnif qoidalari (view-once, himoya, siyosat, tur, hajm, majburiy yuklash), kengaytma va
yo‘l xavfsizligi (`../`, mutlaq yo‘l), kichik fayllarni yuklash va sha256, katta faylni yuklamasdan
ro‘yxatga olish, yuklash paytida hajm oshib ketishi, `fetch`, takror faylni bir marta yuklash, Telegram'da
yo‘q xabar, qayta urinish va `failed`, FloodWait, disk to‘lishi va umumiy chegara, uzilgan yuklashni
tiklash, yo‘qolgan/buzilgan faylni aniqlash va qayta yuklash, sozlama o‘zgarishi, kanal oynasidan
chiqqan post faylini o‘chirish (umumiy faylni saqlab), xizmat rejimida yuklash, `import` da yuklamaslik, CLI.

## Ma’lum cheklovlar

- Telegram fayl hajmini oldindan aytadi; agar aytmasa, chegara yuklash paytida tekshiriladi.
- Premium bo‘lmagan akkauntda Telegram yuklash tezligini cheklashi mumkin — katta navbat sekin tugaydi.
- O‘chirilgan xabarning mediasi faqat oldin yuklangan bo‘lsa saqlanadi; o‘chirilgandan keyin `unavailable`.
- Diskdagi fayllar shifrlanmagan (diskni shifrlash — server darajasida; zaxiralar 10-bosqichda shifrlanadi).
