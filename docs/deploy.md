# Deploy: GitHub → server → doimiy ishlash (Docker Compose)

Bu yo‘riqnoma archiver'ni Linux VPS'da 24/7 ishga tushiradi. Server qayta yoqilganda ham u
avtomatik ishga tushadi. Admin bot 9-bosqichda shu `docker-compose.yml` ga qo‘shiladi.

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

## 4. Konfiguratsiya (`.env`)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Hozircha faqat ikkitasi majburiy: `TG_API_ID`, `TG_API_HASH`. `BOT_TOKEN` va `OWNER_ID` 9-bosqichda kerak bo‘ladi.
Yo‘llar (`DATA_DIR`, `DB_PATH`, …) Docker'da `docker-compose.yml` tomonidan belgilanadi — ularni o‘zgartirmang.

---

## 5. Birinchi ishga tushirish

```bash
docker compose build
docker compose run --rm archiver check-config --component archiver
```

Kutiladigan natija: `[archiver] OK`, `TG_API_HASH  o'rnatilgan (yashirin)`.

**Kirish (bir marta):**

```bash
docker compose run --rm archiver login
```

Telefon raqam → Telegram ilovangizga kelgan kod → (bo‘lsa) 2FA parol. Kutiladigan natija:

```
Yangi session yaratildi: Ism (id=123456789, @username)
Telegram -> Settings -> Devices ro'yxatida 'tgarchive' qurilmasi ko'rinadi.
```

Kodni hech kimga yubormang va forward qilmang — Telegram forward qilingan kodni bloklaydi.

**Rejani ko‘rish (ixtiyoriy):**

```bash
docker compose run --rm archiver dialogs
```

Har chat uchun rejim: `full` (to‘liq), `window20` (kanal: oxirgi 20 post), `skip`.

**Doimiy ishga tushirish:**

```bash
docker compose up -d
docker compose ps                     # ~1 daqiqadan keyin: Up ... (healthy)
docker compose logs -f archiver       # Ctrl+C — faqat log ko'rishni to'xtatadi
docker compose exec archiver python -m tgarchive status
```

Birinchi import hajmga qarab bir necha soatdan bir necha kungacha davom etishi mumkin (FloodWait
cheklovlari). Bu normal. Jarayon `status` da ko‘rinadi va istalgan payt to‘xtatilsa, to‘xtagan joyidan davom etadi.

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

## 9. Vaqtinchalik zaxira (10-bosqichgacha)

10-bosqichda kunlik shifrlangan avtomatik zaxira qo‘shiladi. Hozircha qo‘lda:

```bash
cd ~/tgarchive
docker compose stop archiver
(umask 077; docker compose run --rm -T --entrypoint sh archiver -c 'tar -czf - -C /data db session' \
    > ~/tgarchive-backup-$(date +%F).tar.gz)
docker compose start archiver
ls -l ~/tgarchive-backup-*.tar.gz        # -rw------- bo'lishi kerak
```

Zaxirada **session ham bor** — u parol kabi maxfiy. Shifrlamasdan boshqa joyga ko‘chirmang.

Tiklash (mavjud baza va session **almashtiriladi**):

```bash
docker compose stop archiver
docker compose run --rm -T --entrypoint sh archiver \
    -c 'rm -rf /data/db/* /data/session/* && tar -xzf - -C /data' < ~/tgarchive-backup-YYYY-MM-DD.tar.gz
docker compose up -d
```

---

## 10. Muammolar

| Belgi | Sabab va yechim |
|---|---|
| `env file .env not found` | `cp .env.example .env` (4-qadam) |
| `[archiver] XATO ... TG_API_HASH` | `.env` dagi qiymat my.telegram.org dagi 32 belgili hash emas |
| `Kirish amalga oshmadi: juda ko'p urinish` | Telegram cheklovi — ko‘rsatilgan vaqtdan keyin qayta urinib ko‘ring |
| `Bu baza boshqa akkauntga bog'langan` | Boshqa raqam bilan login qilindi; har akkaunt uchun alohida papka/klon kerak |
| Log'da `flood_wait` | Normal: Telegram tezlik cheklovi, archiver o‘zi kutadi |
| Log'da `chat_inaccessible` | Chatdan chiqqansiz yoki kirish yo‘q — 24 soatdan keyin qayta tekshiriladi |
| `permission denied` volume'da | Volume'ni boshqa image yaratgan bo‘lishi mumkin: `docker compose down` (−v **siz**), so‘ng `docker compose up -d` |

## Hozirgi versiya cheklovlari

- Yangi xabarlar har 10 daqiqada so‘rov bilan olinadi; **real vaqt, tahrirlar tarixi va shaxsiy chatlardagi o‘chirishlarni aniqlash — 7-bosqichda**.
- Media fayllar hali yuklanmaydi — 8-bosqich (navbat yig‘ilib boradi, keyin yuklanadi).
- Admin bot — 9-bosqich.
- Avtomatik shifrlangan zaxira — 10-bosqich.
- Docker build va compose fayl bu yerda (Windows'da Docker yo‘q) haqiqiy Docker bilan **ishga tushirilmagan**: image muhiti taqlid qilinib tekshirilgan (lock'dan o‘rnatish, Linux wheel'lari, `PYTHONPATH` rejimi, compose sintaksisi). Serverdagi birinchi `docker compose build` — haqiqiy tekshiruv.
