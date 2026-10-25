# syntax=docker/dockerfile:1
# Telegram Personal Archive — archiver (keyinroq bot ham shu image'dan ishlaydi).
# Build:  docker compose build
FROM python:3.12.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

# Ilova root huquqisiz ishlaydi
RUN groupadd --system --gid 10001 tgarchive \
 && useradd --system --uid 10001 --gid tgarchive --home-dir /app --no-create-home \
            --shell /usr/sbin/nologin tgarchive

WORKDIR /app

# Bog'liqliklar lock fayldan, sha256 hash tekshiruvi bilan. pyaes (Telethon bog'liqligi)
# faqat manba arxivi sifatida tarqatiladi — sof Python, kompilyator kerak emas.
COPY requirements.txt ./
RUN pip install --require-hashes --only-binary=:all: --no-binary=pyaes -r requirements.txt

COPY src ./src
# Named volume'lar birinchi ulanganda shu kataloglarning egasi (tgarchive) va ruxsatini (0700) oladi
RUN python -m compileall -q src \
 && mkdir -p /data/db /data/session /data/media /data/exports /data/backups \
 && chown -R tgarchive:tgarchive /data \
 && chmod -R 700 /data

USER tgarchive

ENTRYPOINT ["python", "-m", "tgarchive"]
CMD ["run"]
