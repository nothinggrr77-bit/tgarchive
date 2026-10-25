# Deploy: GitHub → server → doimiy ishlash (Docker Compose)

Bu yo‘riqnoma archiver'ni Linux VPS'da 24/7 ishga tushiradi. Server qayta yoqilganda ham u
avtomatik ishga tushadi. Ikkita xizmat ishlaydi: **archiver** (asosiy akkaunt) va **bot** (admin bot, [09-bot.md](09-bot.md)).

**Kerak:** Ubuntu 22.04/24.04 yoki Debian 12/13 VPS (1 vCPU, 1 GB RAM yetadi), SSH kirish,
GitHub akkaunt, my.telegram.org dan `api_id` / `api_hash`.

---

## 1. GitHub'ga yuklash (Windows, Git Bash)

Loyiha papkasida Git repozitoriysi va birinchi commit tayyor.

1. github.com/new sahifasida **bo‘sh, Private** repozitoriy yarating (README, .gitignore, license **qo‘shmang**). Nom, masalan, `tgarchive`.
2. Git Bash'da:

```bash
cd "/c/Users/Davlatov/OneDrive/Desktop/arxiv bot"

# Maxfiy fayl repozitoriyga tushmaganini tekshirish — natija BO'SH bo'lishi kerak:
git ls-files | grep -Ei '(^|/)\.env$|\.session|\.sqlite3'

git remote add origin https://github.com/<LOGIN>/tgarchive.git
git push -u origin main
```

> **Muhim (Windows):** bu papka OneDrive ichida. Haqiqiy `.env` (API_HASH bilan) va session faylini
> shu papkada **yaratmang** — OneDrive ularni Microsoft bulutiga yuklaydi. Haqiqiy `.env` faqat serverda bo‘ladi.

Keyingi o‘zgarishlarni yuborish: `git add -A && git commit -m "..." && git push`.

---

## 2. Serverga Docker o‘rnatish

Rasmiy yo‘riqnoma: <https://docs.docker.com/engine/install/ubuntu/> ("Install using the `apt` repository" bo‘limi;
Debian uchun `.../install/debian/`). O‘rnatgandan keyin:

```bash
sudo systemctl enable --now docker        # server yoqilganda Docker ham yoqilsin
sudo usermod -aG docker $USER             # sudo'siz ishlatish uchun; keyin qayta SSH bilan kiring
docker compose version                    # "Docker Compose version v2.x" chiqishi kerak
```

`docker` guruhi amalda root huquqiga teng — serverga faqat siz kirishingiz kerak.

Firewall: archiver faqat **chiquvchi** ulanish qiladi, hech qanday port ochish shart emas.

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

---

## 3. Repozitoriyni serverga yuklab olish

Private repozitoriy uchun eng xavfsiz yo‘l — faqat o‘qish huquqli **deploy key**:

```bash
ssh-keygen -t ed25519 -C "tgarchive-server" -f ~/.ssh/tgarchive_deploy -N ""
cat ~/.ssh/tgarchive_deploy.pub
```

Chiqqan qatorni GitHub'da: repozitoriy → **Settings → Deploy keys → Add deploy key** ga qo‘ying
("Allow write access" **belgilanmasin**). Keyin:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-tgarchive
  HostName github.com
  User git
  IdentityFile ~/.ssh/tgarchive_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

git clone git@github-tgarchive:<LOGIN>/tgarchive.git ~/tgarchive
cd ~/tgarchive
```

---

## 4. Sozlash va ishga tushirish — bitta buyruq (tavsiya)

Oldindan tayyorlang:
- my.telegram.org → **api_id** va **api_hash**;
- @BotFather → **bot tokeni**;
- **OWNER_ID** (bilmasangiz, keyin qo'shasiz — [09-bot.md](09-bot.md));
- zaxira uchun **age ochiq kaliti** (ixtiyoriy — [10-backup-export.md](10-backup-export.md)).

```bash
cd ~/tgarchive
bash scripts/setup.sh
```

Skript nima qiladi:
1. Docker va Compose borligini tekshiradi.
2. `.env` ni yaratadi (`chmod 600`) va yo'q qiymatlarni **format tekshiruvi bilan** so'raydi. Maxfiy qiymatlar ekranda ko'rinmaydi.
3. Image yig'adi (`docker compose build`).
4. **`doctor`** — barcha ulanish nuqtalarini tekshiradi (pastga qarang).
5. Session yo'q bo'lsa — **login**: telefon raqam, Telegram ilovangizga kelgan kod, (bo'lsa) 2FA parol.
6. Xizmatlarni ishga tushiradi. `OWNER_ID` hali yo'q bo'lsa — faqat archiver ishga tushadi.

Skriptni qayta ishga tushirish xavfsiz: tayyor qiymatlarga tegmaydi, session yaroqli bo'lsa login so'ramaydi.
Masalan, `OWNER_ID` ni keyin `.env` ga yozib, skriptni yana bir marta ishga tushirasiz.

Login kodini hech kimga yubormang va forward qilmang — Telegram forward qilingan kodni bloklaydi.

### doctor — ulanishlarni tekshirish

```bash
docker compose run --rm --no-deps -T archiver doctor
docker compose run --rm --no-deps -T bot doctor
```

```
[umumiy]
  ✅ SQLite: 3.46.1, FTS5 bor
  ✅ Baza: /data/db/archive.sqlite3, migratsiya 0004, butun
[archiver]
  ✅ Kataloglar: yozish mumkin: db, media, exports, backups, session
  ✅ TG_API_ID/TG_API_HASH: Telegram tasdiqladi
  ✅ Session: Ism (id 123456789, @username)
  ✅ Zaxira: har 24 soatda, age bilan shifrlanadi, 7 ta saqlanadi
[bot]
  ✅ BOT_TOKEN: @mening_arxiv_botim
  ✅ OWNER_ID: 987654321 (Ism) — botga /start yuborgan
Natija: 0 xato, 0 ogohlantirish
```

- `doctor` ma'lumotlarni o'zgartirmaydi.
- API_ID/API_HASH QR-login tokeni so'rovi orqali tekshiriladi: telefonga kod bormaydi, hech kimga xabar yuborilmaydi.
- Archiver ishlab turgan bo'lsa, session'ga tegilmaydi — holati heartbeat'dan olinadi.
- Har bir ❌ qatorda nima qilish kerakligi yozilgan.

## 5. Qo'lda sozlash (skriptsiz)

```bash
cp .env.example .env && chmod 600 .env && nano .env      # TG_API_ID, TG_API_HASH, BOT_TOKEN, OWNER_ID
docker compose build
docker compose run --rm --no-deps -T archiver doctor
docker compose run --rm archiver login                    # bir marta
docker compose up -d
docker compose ps                                         # ~1 daqiqadan keyin: Up ... (healthy)
docker compose exec archiver python -m tgarchive status
```

Yo'llar (`DATA_DIR`, `DB_PATH`, …) Docker'da `docker-compose.yml` tomonidan belgilanadi — ularni o'zgartirmang.
Rejani ko'rish (ixtiyoriy, archiver to'xtatilgan holda): `docker compose run --rm archiver dialogs`.

Birinchi import hajmga qarab bir necha soatdan bir necha kungacha davom etishi mumkin (FloodWait
cheklovlari). Bu normal. Jarayon `status` da ko'rinadi va istalgan payt to'xtatilsa, to'xtagan joyidan davom etadi.
Birinchi avtomatik zaxira xizmat ishga tushishi bilan olinadi. Katta import tugagach, qo'lda yana bittasini oling:
`docker compose exec archiver python -m tgarchive backup create`.

---

## 6. Doimiy ishlash qanday ta’minlanadi

| Mexanizm | Nima qiladi |
|---|---|
| `restart: unless-stopped` | Jarayon yiqilsa yoki server qayta yoqilsa — avtomatik ishga tushadi |
| `systemctl enable docker` | Server yoqilganda Docker ham yoqiladi |
| healthcheck (`tgarchive health`) | Heartbeat 3 daqiqadan eski bo‘lsa yoki session muammosi bo‘lsa — `unhealthy` |
| Checkpoint | Har sahifa o‘z checkpoint'i bilan saqlanadi: to‘xtash ma’lumot yo‘qotmaydi |
| Qayta ulanish | Tarmoq uzilsa — 10 s dan 5 daqiqagacha oshib boruvchi pauza bilan |
| Log aylanishi | 5 × 10 MB, disk to‘lib qolmaydi |

Tekshirish: `sudo reboot`, keyin qayta kirib `docker compose ps`.

---

## 7. Kundalik buyruqlar

```bash
cd ~/tgarchive
docker compose ps                                      # holat
docker compose exec archiver python -m tgarchive status
docker compose logs --since 1h archiver                # oxirgi bir soat
docker compose restart archiver
docker compose stop archiver                           # to'xtatish (ma'lumot saqlanadi)
docker compose up -d                                   # qayta ishga tushirish
docker system df -v | grep tgarchive                   # volume hajmlari
docker compose exec archiver python -m tgarchive media large   # 50 MB dan katta fayllar
docker compose exec archiver python -m tgarchive media fetch <ID>
```

**Yangilash** (yangi bosqichlar chiqqanda):

```bash
cd ~/tgarchive
git pull
docker compose up -d --build        # migratsiyalar ishga tushishda avtomatik qo'llanadi
```

> ⚠️ `docker compose down -v` — **butun arxivni va session'ni o‘chiradi**. Hech qachon ishlatmang.
> Oddiy `docker compose down` xavfsiz (volume'lar qoladi).

> Archiver ishlab turganda session ishlatadigan buyruqlarni (`login`, `whoami`, `dialogs`) ishga
> tushirmang — bitta session faylini ikki jarayon bir vaqtda ishlatmasin. Avval `docker compose stop archiver`.
> `status` va `health` faqat bazani o‘qiydi — ular xavfsiz.

---

## 8. Session muammosi

Belgilari: `docker compose ps` → `unhealthy`, `status` da `Archiver: auth_error` va `Sabab: ...`.
Odatda session Telegram'da *Settings → Devices* dan o‘chirilganda bo‘ladi.

```bash
docker compose stop archiver
docker compose run --rm archiver login
docker compose up -d
```

Session o‘g‘irlanganidan shubha bo‘lsa: avval Telegram'da *Devices* → `tgarchive` ni tugating, keyin yuqoridagi qadamlar.

---

## 9. Zaxira va tiklash

Archiver har kuni baza va session'ning zaxirasini avtomatik yaratadi (`backups` volume, oxirgi 7 tasi).
**Bir marta sozlang:** shifrlash uchun age ochiq kalitini `.env` dagi `BACKUP_AGE_RECIPIENTS` ga yozing.
Maxfiy kalit faqat sizning kompyuteringizda bo'ladi. Kalit yaratish, zaxirani kompyuteringizga
ko'chirish, tekshirish va tiklash: **[10-backup-export.md](10-backup-export.md)**.

```bash
docker compose exec archiver python -m tgarchive backup list
docker compose exec archiver python -m tgarchive status        # "Zaxira: oxirgi ..." qatori
```

---

## 10. Muammolar

| Belgi | Sabab va yechim |
|---|---|
| Nima ishlamayotgani noma'lum | `docker compose run --rm --no-deps -T archiver doctor` (va `bot doctor`) — har bir ❌ yonida yechim |
| `env file .env not found` | `bash scripts/setup.sh` yoki `cp .env.example .env` |
| `[archiver] XATO ... TG_API_HASH` | `.env` dagi qiymat my.telegram.org dagi 32 belgili hash emas |
| `Kirish amalga oshmadi: juda ko'p urinish` | Telegram cheklovi — ko‘rsatilgan vaqtdan keyin qayta urinib ko‘ring |
| `Bu baza boshqa akkauntga bog'langan` | Boshqa raqam bilan login qilindi; har akkaunt uchun alohida papka/klon kerak |
| Log'da `flood_wait` | Normal: Telegram tezlik cheklovi, archiver o‘zi kutadi |
| Log'da `chat_inaccessible` | Chatdan chiqqansiz yoki kirish yo‘q — 24 soatdan keyin qayta tekshiriladi |
| `permission denied` volume'da | Volume'ni boshqa image yaratgan bo‘lishi mumkin: `docker compose down` (−v **siz**), so‘ng `docker compose up -d` |

## Hozirgi versiya cheklovlari

- Real vaqt ishlaydi (yangi xabarlar, tahrirlar, o‘chirishlar). Archiver o‘chiq bo‘lgan paytdagi o‘chirishlar faqat Telegram qayta ulanishda bergan bo‘lsa qayd etiladi — [07-realtime.md](07-realtime.md).
- Media: 50 MB gacha fayllar yuklanadi; kattalari ro‘yxatda ko‘rinadi ([08-media.md](08-media.md)). Disk to‘lib qolmasligi uchun `MEDIA_MIN_FREE_GB` (5 GB) himoyasi bor.
- Zaxira faqat server diskida bo'lsa, disk nosozligidan himoya qilmaydi — haftada bir marta o'z kompyuteringizga ko'chiring ([10-backup-export.md](10-backup-export.md)).
- Docker build va compose fayl ishlab chiqish kompyuterida (Windows'da Docker yo‘q) haqiqiy Docker bilan **ishga tushirilmagan**: image muhiti taqlid qilinib tekshirilgan (lock'dan o‘rnatish, Linux wheel'lari, `PYTHONPATH` rejimi, compose sintaksisi), `setup.sh` esa soxta `docker` bilan sinalgan. Serverdagi birinchi `bash scripts/setup.sh` — haqiqiy tekshiruv; muammo bo'lsa, chiqishini yuboring.
