# 10-bosqich. Audit, eksport, zaxira va tiklash

## Maqsad

- **Zaxira:** baza va session har kuni avtomatik nusxalanadi, standart `age` formatida shifrlanadi, oxirgi 7 tasi saqlanadi.
- **Tiklash:** bitta buyruq bilan. Joriy baza o‘chirilmaydi, yonida qoladi.
- **Eksport:** chat, sana oralig‘i yoki istalgan qidiruv filtri bo‘yicha HTML yoki JSON (zip).
- **Audit:** `/audit` — bot buyruqlari, ruxsatsiz urinishlar, eksport, zaxira, o‘chirish hodisalari.
- **Saqlash muddati (retention):** mexanizm tayyor, lekin **sukut bo‘yicha o‘chiq** (sizning qaroringiz: doimiy saqlash).

## Yaratilgan va o‘zgargan fayllar

| Fayl | Mazmuni |
|---|---|
| [backup.py](../src/tgarchive/backup.py) | Zaxira yaratish (online SQLite backup → tar.gz → age, oqim bilan), tekshirish, tiklash |
| [crypto.py](../src/tgarchive/crypto.py) | age kalitlari: tekshirish, o‘qish, yaratish |
| [export.py](../src/tgarchive/export.py) | HTML/JSON eksport, zip, media, eski eksportlarni tozalash |
| [archiver/maintenance.py](../src/tgarchive/archiver/maintenance.py) | Rejali ishlar: zaxira, retention, eksport tozalash |
| [db/retention.py](../src/tgarchive/db/retention.py) | Saqlash muddati |
| [db/search.py](../src/tgarchive/db/search.py) | `apply_filters` — qidiruv va eksport uchun umumiy filtrlar |
| `bot/views.py`, `bot/handlers.py` | `/export`, `/audit`, `/status` da zaxira holati |
| `cli.py` | `backup create/list/verify/restore/keygen`, `export`, `retention show/set` |
| `config.py`, `.env.example`, `pyproject.toml`, lock fayllar | `BACKUP_*`, `EXPORT_*`; `pyrage==1.4.0` |
| tests: `test_backup.py`, `test_export.py`, `test_bot.py` | 27 ta yangi test |

## Zaxira

### Nima saqlanadi

`BACKUP_DIR/tgarchive-YYYYmmdd-HHMMSS.tar.gz.age` ichida:

| Fayl | Izoh |
|---|---|
| `manifest.json` | vaqt, versiya, migratsiya, xabarlar soni, har bir faylning sha256 |
| `db/archive.sqlite3` | SQLite **online backup API** bilan olingan izchil nusxa — xizmat to‘xtamaydi |
| `session/archiver.session` | Asosiy akkaunt session'i — **akkauntga to‘liq kirish**, shuning uchun shifrlash muhim |
| `media/...` | Faqat `BACKUP_INCLUDE_MEDIA=true` bo‘lsa (hajmi katta bo‘lishi mumkin) |

Har kuni (`BACKUP_INTERVAL_H=24`) archiver o‘zi yaratadi. Oxirgi 7 tasi saqlanadi (`BACKUP_KEEP`).
Xato bo‘lsa, bir soatdan keyin qayta urinadi. Holat `tgarchive status` va bot `/status` da:

```
Zaxira: oxirgi 2026-10-25 03:30 UTC (5 soat oldin), 48.2 MB, shifrlangan
```

### Shifrlash kaliti (bir marta)

Zaxira **ochiq kalit** bilan shifrlanadi. **Maxfiy kalit serverda bo‘lmaydi**: server buzilsa ham,
boshqa joyga ko‘chirilgan eski zaxiralarni ochib bo‘lmaydi.

**Tavsiya — kalitni o‘z kompyuteringizda yaratish.** `age` dasturini <https://github.com/FiloSottile/age/releases>
dan oling (Windows uchun ham bor) va:

```bash
age-keygen -o tgarchive-key.txt      # "Public key: age1..." qatorini ko'rsatadi
```

`tgarchive-key.txt` ni kompyuteringizda xavfsiz joyda saqlang: parol menejeri yoki shifrlangan disk.
Bu faylni **yo‘qotsangiz, zaxiralarni ochib bo‘lmaydi.** Ochiq kalitni serverdagi `.env` ga yozing:

```
BACKUP_AGE_RECIPIENTS=age1...
```

Muqobil yo‘l (agar `age` o‘rnatilmagan bo‘lsa): `docker compose run --rm archiver backup keygen`. Bu buyruq
kalitlarni faqat ekranga chiqaradi, hech qayerga saqlamaydi. Maxfiy qismni kompyuteringizdagi faylga
ko‘chiring va terminal tarixini tozalang.

Bir nechta kalit (masalan, ikkita kompyuter): vergul bilan, `age1...,age1...`. `ssh-ed25519 ...` ochiq kaliti ham ishlaydi.

> Kalit berilmasa, zaxira **shifrlanmagan** holda yaratiladi (0600 ruxsat bilan). `status` buni
> `⚠️ SHIFRLANMAGAN` deb ko‘rsatadi. Bunday zaxirani server tashqarisiga ko‘chirmang.

### Qo‘lda boshqarish

```bash
docker compose exec archiver python -m tgarchive backup create   # hozir
docker compose exec archiver python -m tgarchive backup list
```

### Zaxirani o‘z kompyuteringizga ko‘chirish (tavsiya)

Faqat serverdagi zaxira disk buzilishidan himoya qilmaydi. Haftada bir marta:

```bash
# Serverda
mkdir -p ~/tgarchive-backups
docker compose cp archiver:/data/backups/. ~/tgarchive-backups/

# Kompyuteringizda (Git Bash)
scp 'user@server:~/tgarchive-backups/*.age' .
```

Kompyuterda tekshirish (rasmiy `age` bilan, tgarchive kerak emas):

```bash
age -d -i tgarchive-key.txt tgarchive-20261025-033000.tar.gz.age | tar tz
```

### Tekshirish

```bash
# Kalitni stdin'dan beramiz — u konteynerga ham, diskka ham yozilmaydi
docker compose run --rm -T archiver backup verify tgarchive-20261025-033000.tar.gz.age --identity - < ~/tgarchive-key.txt
```

Natija: `Zaxira butun: ...` va xabarlar soni. Tekshiruv quyidagilarni o‘z ichiga oladi:
- age shifrini ochish (noto‘g‘ri kalit yoki o‘zgartirilgan fayl — xato);
- har bir faylning sha256'i;
- baza `quick_check`;
- arxivda `../` va havolalar yo‘qligi.

### Tiklash

```bash
cd ~/tgarchive
docker compose stop archiver bot
# kalitni vaqtincha serverga ko'chiring (masalan scp bilan), keyin:
docker compose run --rm -T archiver backup restore tgarchive-20261025-033000.tar.gz.age --identity - --yes < ~/tgarchive-key.txt
shred -u ~/tgarchive-key.txt      # kalitni serverdan o'chiring
docker compose up -d
```

- Archiver yoki bot ishlab tursa, tiklash **rad etiladi**: bitta bazaga ikki jarayon yozmasin.
- Joriy baza va session **o‘chirilmaydi**: `archive.sqlite3.before-restore-<vaqt>` nomi bilan yonida qoladi.
- Tiklashdan keyin migratsiyalar avtomatik qo‘llanadi (eski zaxira yangi versiyada ham ochiladi).
- Session Telegram'da bekor qilingan bo‘lsa (*Devices* dan o‘chirilgan): `docker compose run --rm archiver login`.

**Yangi serverga ko‘chish:** loyihani klonlang va `.env` ni yozing. `docker compose up -d` bilan bir marta
ishga tushirib, keyin to‘xtating. So‘ng zaxirani volume'ga nusxalang:
`docker compose cp ./tgarchive-...age archiver:/data/backups/` va yuqoridagi tiklash buyrug'ini bajaring.

## Eksport

Bot:

```
/export chat:-1001234 after:2026-01-01 before:2026-02-01
/export chat:7 json +media
/export is:deleted
/export all
```

CLI: `docker compose exec bot python -m tgarchive export chat:7 after:2026-01-01 json`

| Kalit | Ma’nosi |
|---|---|
| filtrlar | `/search` bilan bir xil: `chat:`, `from:`, `after:`, `before:`, `on:`, `type:`, `has:`, `is:`, `name:`, matn |
| `json` | JSON (sukut: HTML) |
| `+media` | Yuklangan fayllar ham `media/` ga (`EXPORT_MAX_MEDIA_MB=500` gacha) |
| `all` | Butun arxiv (filtrsiz eksport faqat shu so‘z bilan — tasodifan bo‘lmasin) |

**Natija:** `export-YYYYmmdd-HHMMSS.zip` (`export.html` yoki `export.json` + `README.txt` [+ `media/`]).
50 MB gacha bo‘lsa, bot faylni o‘zi yuboradi. Kattasini serverdan olasiz:
`docker compose cp bot:/data/exports/<nom> .`

**HTML:** chatlar bo‘yicha bo‘limlar, vaqt (Toshkent), yuboruvchi, matn, reply havolasi, forward,
tahrirlar (eski versiyalar ochiladigan ro‘yxatda), o‘chirilganlik belgisi, media (rasm — ko‘rinadi,
boshqalar — havola yoki "yuklanmagan" yozuvi). Brauzerda tungi rejimni ham qo‘llaydi.

**JSON:** to‘liq ma’lumot: xabar maydonlari, entity'lar, versiyalar, o‘chirilganlik, media metama’lumoti va sha256.

**Xavfsizlik:**
- HTML'dagi barcha matn escape qilinadi va qat'iy Content-Security-Policy qo'yiladi, shuning uchun eksportni ochganda hech qanday skript ishlamaydi.
- Eksport fayllari 0600 ruxsat bilan saqlanadi va 7 kundan keyin (`EXPORT_KEEP_DAYS`) serverdan avtomatik o'chiriladi.
- Har bir eksport `audit_log` ga yoziladi.

## Audit

Bot `/audit` oxirgi 15 ta audit yozuvini va 10 ta o‘chirish hodisasini ko‘rsatadi.
Audit'ga quyidagilar yoziladi:
- login;
- ruxsatsiz urinishlar (matnsiz);
- `media_fetch_requested` (katta faylni yuklatish so'rovi), `bot_send_file`;
- `export_created`;
- `backup_created` va `backup_failed`;
- `retention_set` va `retention_applied`;
- `window_pruned` (kanal oynasidan chiqqan postlar).

Xabar mazmuni audit'ga yozilmaydi.

## Saqlash muddati (retention)

Sukut bo‘yicha o‘chiq. Yoqish:

```bash
# Butun arxiv uchun: .env da  RETENTION_DAYS=365
docker compose exec archiver python -m tgarchive retention set -1001234 30    # bitta chat uchun 30 kun
docker compose exec archiver python -m tgarchive retention set -1001234 off
docker compose exec archiver python -m tgarchive retention show
```

- Kuniga bir marta muddati o‘tgan xabarlar olib tashlanadi: versiyalari, media qatorlari va fayllari bilan (fayl boshqa xabarda ishlatilmasa).
- Tarix importi muddati o‘tgan xabarlarni qayta yuklamaydi.
- Audit'ga faqat sonlar yoziladi.
- ⚠️ Olib tashlangan xabarlar faqat eski zaxiralarda qoladi.

## Testlash

```bash
pytest tests/test_backup.py tests/test_export.py -v
```

Tekshiriladi:
- **Zaxira:** shifrsiz va shifrli zaxira tarkibi; kalitsiz va noto'g'ri kalit bilan rad etish; o'zgartirilgan fayl (ikkala rejimda); arxivda `../` hujumi; media qo'shish; eski zaxiralarni o'chirish; vaqtinchalik fayllarni tozalash.
- **Tiklash:** ma'lumot qaytadi, eski baza yonida qoladi.
- **Rejalashtirish va CLI:** jadval, xato bo'lganda bir soat kutish, status qatori, noto'g'ri kalit — konfiguratsiya xatosi; CLI `create/list/verify/restore`, ishlayotgan archiver bilan tiklashni rad etish, `--identity -`.
- **Eksport:** HTML escape va CSP, `<script>` ishlamasligi, JSON tarkibi va filtrlar, media chegarasi, partiyalab o'qish, bo'sh natija, eski eksportlarni tozalash (yozilayotgan `.part` ga tegmasdan), CLI.
- **Retention:** xabarlar va fayllarni o'chirish, chat bo'yicha muddat, import retention'ni hurmat qilishi, CLI.
- **Bot:** `/export` (fayl yuborish, katta fayl, xato, bo'sh natija), `/audit`, `/status` da zaxira holati.

Zaxirani ochishda `../` filtri olib tashlansa yoki HTML escape o'chirilsa, testlar yiqilishi alohida tekshirilgan.

## Ma’lum cheklovlar

- Zaxira yaratilayotganda `BACKUP_DIR` da baza hajmicha vaqtinchalik joy kerak (izchil nusxa uchun); shifrlangan arxiv oqim bilan yoziladi.
- `BACKUP_INCLUDE_MEDIA=true` har safar barcha media'ni to‘liq qo‘shadi (inkremental emas). Media katta bo‘lsa, `media` volume'ini alohida `rsync` qilish samaraliroq.
- Zaxira faqat server diskida bo‘lsa, disk nosozligidan himoya qilmaydi — muntazam o‘z kompyuteringizga ko‘chiring.
- Diskdagi baza va media fayllari shifrlanmagan (server diskini shifrlash — VPS darajasida).
