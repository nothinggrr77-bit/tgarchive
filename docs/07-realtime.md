# 7-bosqich. Real vaqt hodisalari

## Maqsad

Archiver ishlab turganda yangi xabarlar **darhol** arxivga tushadi, tahrirlarning eski holati
saqlanadi, o‘chirilgan xabarlar qayd etiladi — 6-bosqichdagi 10 daqiqalik sinxron esa
o‘tkazib yuborilgan hodisalar uchun xavfsizlik to‘ri (reconcile) bo‘lib qoladi.

## Yaratilgan va o‘zgargan fayllar

| Fayl | Mazmuni |
|---|---|
| [archiver/realtime.py](../src/tgarchive/archiver/realtime.py) | `RealtimeArchiver`: Telethon handler'lari, navbat, iste’molchi, tahrir, kanal oynasi, o‘chirish va uni tekshirish |
| [archiver/service.py](../src/tgarchive/archiver/service.py) | handler'lar `connect()` dan oldin, iste’molchi akkaunt tekshiruvidan keyin; to‘xtashda navbatni yozib bo‘lish |
| [archiver/client.py](../src/tgarchive/archiver/client.py) | xizmat uchun `receive_updates`, `catch_up`, `sequential_updates` |
| [archiver/dialogs.py](../src/tgarchive/archiver/dialogs.py) | `register_chat` — yangi chat real vaqtda ham dialoglar qoidalari bilan ro‘yxatdan o‘tadi |
| [archiver/history.py](../src/tgarchive/archiver/history.py) | `resolve_reply_targets` ochiq; o‘chirilgan "eng yangi" xabar tuzatishi |
| [db/store.py](../src/tgarchive/db/store.py) | `revoke_deletions` |
| [db/status.py](../src/tgarchive/db/status.py) | `status` da real vaqt hisoblagichlari |
| `config.py`, `.env.example` | `REALTIME_ENABLED` |
| tests: `test_realtime.py`, `test_service.py`, `fake_telegram.py` | hodisalar testlari |

## Qanday ishlaydi

```
Telegram ──update──> Telethon (catch_up, ketma-ket) ──> handler: navbatga qo'yadi va qaytadi
                                                                 │
                                                 asyncio.Queue (≤ 10 000)
                                                                 │
                                         iste'molchi (bitta, kelish tartibida)
                                ┌────────────────┼──────────────────────┐
                        yangi xabar / tahrir          o'chirish (kanal)      o'chirish (shaxsiy / guruh)
                        save_messages (idempotent)    confirmed              probable -> get_messages tekshiruvi
                        kanal: oynani 20 ga qisqartirish                     yo'q -> confirmed, bor -> revoked
                        reply nishonini olish
```

- **Avval tinglovchi.** Handler'lar `connect()` dan oldin ro‘yxatdan o‘tadi: ulanishda Telegram qaytargan o‘tkazib yuborilgan hodisalar (`catch_up`) ham navbatga tushadi. Iste’molchi akkaunt bazaga mosligi tekshirilgandan keyin ishga tushadi — begona akkaunt ma’lumoti yozilmaydi. (Test: `test_handlers_registered_before_connect_and_catch_up_is_not_lost` — tartib buzilsa, test yiqilishi tekshirilgan.)
- **Tartib.** `sequential_updates` + bitta iste’molchi: tahrir hech qachon asl xabardan oldin qayta ishlanmaydi. Baribir kechikkan eski nusxa kelsa, `edit_date` bo‘yicha e’tiborsiz qoldiriladi (`stale`).
- **Takrorlanmaslik.** Real vaqt, oldinga sinxron va tarix importi bir xabarni bir necha marta keltirishi mumkin — `(chat_id, message_id)` noyobligi tufayli bitta yozuv.
- **To‘xtash.** `SIGTERM` da handler'lar olib tashlanadi, navbat 10 sekundgacha yozib bo‘linadi.
- **Bitta buzilgan hodisa** boshqalarini to‘xtatmaydi: log'ga `realtime_event_failed`, hisoblagich `errors`.

## Qoidalar

| Hodisa | Shaxsiy chat, guruh | Kanal (oxirgi 20 post) | Arxivlanmaydigan chat |
|---|---|---|---|
| Yangi xabar | saqlanadi | saqlanadi, eng eski post oynadan chiqadi (reply qilingan postlar qoladi) | e’tiborsiz |
| Tahrir | eski holat `message_versions` ga, yangisi joriy | faqat arxivdagi post uchun | e’tiborsiz |
| O‘chirish | `probable` → darhol tekshiruv → `confirmed` yoki `revoked` | `confirmed` | e’tiborsiz (audit ham yozilmaydi) |
| Yangi (bazada yo‘q) chat | birinchi xabarida siyosat bo‘yicha ro‘yxatdan o‘tadi | xuddi shunday | `skip` sifatida yoziladi |

Faqat ko‘rishlar soni yoki reaksiyalar o‘zgargan "tahrir" yangi versiya yaratmaydi (`content_hash`).

## O‘chirilgan xabarlar — halol chegaralar

| Holat | Natija |
|---|---|
| Archiver ishlab turibdi, xabar oldin arxivlangan, keyin o‘chirildi | ✅ Matn saqlanadi, holat `confirmed` |
| Archiver ishlab turibdi, xabar yuborilib darhol o‘chirildi | ✅ odatda: avval yangi xabar hodisasi, keyin o‘chirish keladi |
| Archiver qisqa vaqt o‘chiq edi | ⚠️ Qayta ulanganda `catch_up` o‘tkazib yuborilgan o‘chirishlarni olib kelishi mumkin. Uzilish juda uzun bo‘lsa (`differenceTooLong`), Telegram ularni bermaydi |
| Xabar arxivga tushishidan **oldin** o‘chirilgan | ❌ Tiklab bo‘lmaydi — Telegram matnni bermaydi; `deletion_events` da `not_archived` |
| Shaxsiy chat hodisasida chat ko‘rsatilmagan | Faqat shaxsiy chat va oddiy guruh xabarlari orasidan qidiriladi; kanal xabariga **hech qachon** bog‘lanmaydi; bir nechta nomzod bo‘lsa — hech biri belgilanmaydi |
| Telegram hodisa yubormagan | ❌ Kafolat yo‘q. Telethon hujjati: *"this event isn't 100% reliable, since Telegram doesn't always notify the clients that a message was deleted"*. Kanal oynasi uchun soatlik tekshiruv qo‘shimcha himoya beradi |

## Real vaqtning boshqa cheklovlari

- **Servis xabarlari** (pin, a’zo qo‘shilishi, qo‘ng‘iroqlar) real vaqtda kelmaydi — Telethon `NewMessage` ularni o‘tkazib yuboradi. Ular 10 daqiqalik sinxron bilan olinadi.
- **Katta kanal va superguruhlar:** Telegram ba’zi kanallar uchun yangilanishlarni kamaytirishi mumkin. Shuning uchun dialoglar sinxroni (10 daqiqa) va kanal oynasi tekshiruvi (1 soat) saqlanib qoldi.
- **O‘chiq paytdagi tahrirlar:** `catch_up` olib kelmasa, eski xabarlarning tahriri aniqlanmaydi (kanal oynasidagi postlar bundan mustasno — ular har soatda yangilanadi).
- **Navbat to‘lib qolsa** (10 000 hodisa — baza juda sekin bo‘lsa), handler kutadi va Telethon kelgan
  update'larni o‘zining cheklanmagan ichki navbatida (xotirada) ushlab turadi: hodisalar tashlab yuborilmaydi,
  lekin RAM sarfi o‘sadi (`mem_limit: 512m`). Amalda shaxsiy akkaunt uchun bu holat kutilmaydi; `status` dagi
  `navbatda` qiymati doimiy o‘sib borsa — bu muammo belgisi.

## Buyruqlar va kutiladigan natija

Alohida buyruq yo‘q — `tgarchive run` (Docker'da avtomatik) real vaqtni o‘zi yoqadi. O‘chirish: `.env` da `REALTIME_ENABLED=false`.

```bash
docker compose exec archiver python -m tgarchive status
```

```
Archiver: idle (oxirgi signal 8 s oldin, v0.1.0)
Real vaqt: yangi 143, tahrir 6, o'chirish 2, navbatda 0, oxirgi hodisa 2026-10-25 14:02:11 UTC
Chatlar (rejim): full: 41, skip: 6, window: 23, -: 4
...
```

Log'da: `realtime_deleted` (o‘chirish hodisasi va natijasi), `window_trimmed`, `realtime_chat_registered`.

## Testlash

```bash
pytest tests/test_realtime.py tests/test_service.py -v
```

Tekshiriladi: kiruvchi va chiquvchi xabar, dublikat hodisa, arxivlanmaydigan chatlar, yangi chatni ro‘yxatdan o‘tkazish, boshqa chatdagi reply nishoni, tahrir versiyasi va "faqat ko‘rishlar" tahriri, kechikkan asl nusxa tahrirni buzmasligi, kanal oynasini aylantirish va reply qilingan postni saqlash, oynadan tashqaridagi tahrir, kanal o‘chirishi, `skip` kanal o‘chirishi, shaxsiy chat o‘chirishi → tekshiruv → `confirmed` / `revoked`, tekshiruv tarmoq xatosi, noma’lum o‘chirishni taxmin qilmaslik, Telethon handler'lari va tartib, buzilgan hodisa, handler'larning `connect()` dan oldin ro‘yxatdan o‘tishi va `catch_up` hodisasi yo‘qolmasligi, sessiya xatosida handler'larni olib tashlash.
