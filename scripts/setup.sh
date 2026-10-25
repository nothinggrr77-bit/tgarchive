#!/usr/bin/env bash
# Telegram Personal Archive — serverda o'rnatish va qayta sozlash (qayta ishga tushirish xavfsiz).
#
#   cd ~/tgarchive && bash scripts/setup.sh
#
# Qiladi: Docker'ni tekshiradi -> .env ni to'ldiradi (format tekshiruvi bilan) -> image yig'adi ->
# doctor -> session yo'q bo'lsa login -> xizmatlarni ishga tushiradi. Tayyor sozlamalarga tegmaydi.
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
die()  { printf '  [X]  %s\n' "$*" >&2; exit 1; }

compose() { docker compose "$@"; }

say "1/6 Docker"
command -v docker >/dev/null 2>&1 || die "Docker o'rnatilmagan: docs/deploy.md, 2-qadam"
compose version >/dev/null 2>&1 || die "Docker Compose plagini yo'q: docs/deploy.md, 2-qadam"
docker info >/dev/null 2>&1 || die "Docker'ga ulanib bo'lmadi: 'sudo usermod -aG docker \$USER', so'ng SSH'dan chiqib qayta kiring"
ok "$(compose version)"

say "2/6 Sozlamalar (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  ok ".env yaratildi (.env.example dan)"
fi
chmod 600 .env

get_env() { sed -n "s/^$1=//p" .env | tail -n 1; }

set_env() {
  # Qiymat ask() da qat'iy regex bilan tekshirilgan: '|', '&', '\' va bo'shliq bo'lmaydi
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

ask() {
  # ask KALIT "savol" REGEX [secret|""] [optional|""]
  local key=$1 prompt=$2 regex=$3 secret=${4:-} optional=${5:-} value
  if [ -n "$(get_env "$key")" ]; then
    ok "$key o'rnatilgan"
    return
  fi
  while true; do
    local got=0
    if [ "$secret" = secret ]; then
      read -r -s -p "  $prompt: " value || got=$?
      echo
    else
      read -r -p "  $prompt: " value || got=$?
    fi
    if [ "$got" -ne 0 ]; then  # kiritish tugadi (terminal yo'q) — cheksiz so'ramaymiz
      value=""
      [ "$optional" = optional ] || die "$key kiritilmadi — skriptni terminalda ishga tushiring yoki .env ga yozing"
    fi
    value=$(printf '%s' "$value" | tr -d '[:space:]')
    if [ -z "$value" ] && [ "$optional" = optional ]; then
      warn "$key bo'sh qoldirildi"
      return
    fi
    if [[ "$value" =~ $regex ]]; then
      set_env "$key" "$value"
      ok "$key yozildi"
      return
    fi
    warn "format noto'g'ri, qayta kiriting"
  done
}

ask TG_API_ID "my.telegram.org -> api_id (raqam)" '^[0-9]{3,12}$'
ask TG_API_HASH "my.telegram.org -> api_hash (32 belgi; ekranda ko'rinmaydi)" '^[0-9a-f]{32}$' secret
ask BOT_TOKEN "@BotFather tokeni (ekranda ko'rinmaydi)" '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$' secret
ask OWNER_ID "Ikkinchi akkaunt ID'si (bilmasangiz Enter: docs/09-bot.md)" '^[0-9]{3,15}$' "" optional
ask BACKUP_AGE_RECIPIENTS "Zaxira uchun age ochiq kaliti age1... (bo'lmasa Enter)" '^age1[0-9a-z]{50,70}$' "" optional

say "3/6 Docker image"
compose build

say "4/6 Tekshiruv"
# Session'ni bir vaqtda ikki jarayon ishlatmasligi uchun archiver to'xtatiladi (oxirida qayta yoqiladi)
compose stop archiver >/dev/null 2>&1 || true
compose run --rm --no-deps -T archiver doctor \
  || warn "doctor xato topdi (birinchi o'rnatishda 'Session' xatosi normal — keyingi qadamda login qilamiz)"

say "5/6 Asosiy akkaunt (session)"
if compose run --rm --no-deps -T archiver whoami; then
  ok "session yaroqli"
else
  warn "session yo'q yoki yaroqsiz — login: telefon raqam, Telegram'dan kelgan kod, (bo'lsa) 2FA parol"
  compose run --rm --no-deps archiver login || die "login amalga oshmadi — qayta: bash scripts/setup.sh"
fi

say "6/6 Ishga tushirish"
if [ -n "$(get_env BOT_TOKEN)" ] && [ -n "$(get_env OWNER_ID)" ]; then
  compose up -d
  compose run --rm --no-deps -T bot doctor || warn "bot tekshiruvida muammo — yuqoriga qarang"
  echo "  Ikkinchi akkauntdan botga /start yuboring."
else
  compose up -d archiver
  warn "OWNER_ID yoki BOT_TOKEN yo'q — bot ishga tushirilmadi. Qo'shgach: bash scripts/setup.sh"
fi
compose ps
echo
echo "Holat:  docker compose exec archiver python -m tgarchive status"
echo "Loglar: docker compose logs -f archiver"
