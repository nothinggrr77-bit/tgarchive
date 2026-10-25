# Telegram API cheklovlari ro‘yxati

Har bir band yonida manba ko‘rsatilgan. "Tekshirildi" — 2026-10-25 holatiga rasmiy hujjatdan tasdiqlangan.
"Telethon hujjati" — kutubxona hujjatidan; Telegram rasmiy kafolati emas.

## 1. Avtorizatsiya va session

| # | Cheklov | Ta’siri | Manba |
|---|---|---|---|
| 1.1 | `api_id` / `api_hash` faqat my.telegram.org dan, o‘z akkauntingiz uchun olinadi | `.env` da saqlanadi, Git'ga qo‘shilmaydi | my.telegram.org |
| 1.2 | Session fayli ichida **auth key** bor — u akkauntga to‘liq kirish huquqini beradi | Session o‘g‘irlansa, 2FA paroli **so‘ralmaydi** (parol faqat yangi login paytida kerak) | MTProto auth key mexanizmi (core.telegram.org/mtproto/auth_key, /api/srp) |
| 1.3 | Session'ni Telegram ilovasida *Settings → Devices* orqali bekor qilish mumkin | O‘g‘irlik shubhasida birinchi qadam — shu | Telegram ilovasi |

## 2. Tarixni olish

| # | Cheklov | Ta’siri |
|---|---|---|
| 2.1 | `messages.getHistory` bir so‘rovda **ko‘pi bilan 100** xabar qaytaradi | `HISTORY_PAGE_SIZE` 1–100 bilan cheklangan |
| 2.2 | Sahifalash `offset_id` (shu ID'dan eskilar), `min_id` / `max_id` orqali | Backfill `offset_id` bilan pastga, gap-fill `min_id` bilan yuqoriga |
| 2.3 | Faqat **hozir mavjud** xabarlar qaytadi | Oldin o‘chirilganlarni tiklab bo‘lmaydi |
| 2.4 | "Yangi a’zolar uchun tarix yashirin" superguruhlarda qo‘shilishdan oldingi xabarlar ko‘rinmaydi | Import shu nuqtada tugaydi — bu xato emas |
| 2.5 | Chiqib ketilgan yoki chetlatilgan chatlar dialoglar ro‘yxatida bo‘lmasligi mumkin | Ularning tarixi faqat oldin arxivlangan bo‘lsa bor |

## 3. Flood va tezlik cheklovlari

| # | Cheklov | Ta’siri |
|---|---|---|
| 3.1 | `FLOOD_WAIT_X` — X sekund kutish shart | Telethon `flood_sleep_threshold` (sukut 60 s) gacha o‘zi kutadi; undan uzunini biz ushlaymiz |
| 3.2 | Juda uzun FloodWait (soatlab) bo‘lishi mumkin | `MAX_FLOOD_WAIT_S` dan uzun bo‘lsa chat `next_retry_at` bilan keyinga qoldiriladi, boshqa ishlar to‘xtamaydi |
| 3.3 | `FLOOD_PREMIUM_WAIT_X` — Premium bo‘lmagan akkauntda yuklab olish tezligi cheklanadi | Media worker parallelligi past (`MEDIA_CONCURRENCY=2`) |
| 3.4 | Aniq raqamli limitlar Telegram tomonidan e’lon qilinmaydi va o‘zgaradi | Konservativ sukut qiymatlar: sahifalar orasida 1 s, chatlar ketma-ket |

## 4. Takeout (rasmiy eksport sessiyasi) — tekshirildi

| # | Cheklov | Manba |
|---|---|---|
| 4.1 | `account.initTakeoutSession` — foydalanuvchi ma’lumotlarini eksport qilish uchun | core.telegram.org/api/takeout |
| 4.2 | Barcha so‘rovlar `invokeWithTakeout` ichida yuborilishi shart | o‘sha |
| 4.3 | Xato `TAKEOUT_INIT_DELAY_X`: "xavfsizlik sababli X sekunddan keyin boshlay olasiz"; barcha qurilmalaringizga xabar boradi | core.telegram.org/method/account.initTakeoutSession |
| 4.4 | Tugatish: `account.finishTakeoutSession` (`success` bayrog‘i bilan) | core.telegram.org/api/takeout |
| 4.5 | Takeout'da flood limitlari pastroq — **faqat Telethon hujjatida** aytilgan, rasmiy sahifada yo‘q | Telethon hujjati |

## 5. Xabar ID ketma-ketliklari — tekshirildi

| # | Cheklov | Ta’siri |
|---|---|---|
| 5.1 | Shaxsiy chatlar va oddiy guruhlar **bitta umumiy** ketma-ketlikni (common message box) ishlatadi; bu ID'lar akkauntga xos | Bitta akkaunt doirasida shaxsiy chat va oddiy guruh xabar ID'si noyob |
| 5.2 | Har bir kanal/superguruhning **o‘z** ketma-ketligi bor; ID'lar barcha akkauntlar uchun bir xil | Turli kanallarda bir xil ID bo‘lishi normal |
| 5.3 | Shuning uchun noyob kalit — `(chat_id, message_id)` | Model shunga qurilgan |

Manba: core.telegram.org/api/updates

## 6. Real vaqt yangilanishlari — tekshirildi

| # | Cheklov | Ta’siri |
|---|---|---|
| 6.1 | Yangilanishlar `pts` ketma-ketligi bilan keladi; uzilish bo‘lsa `getDifference` / `getChannelDifference` bilan to‘ldiriladi | Telethon buni o‘zi bajaradi (`catch_up`) |
| 6.2 | Uzilish juda uzun bo‘lsa `differenceTooLong` / `channelDifferenceTooLong` qaytadi — holatni qaytadan olish kerak, oradagi hodisalar yo‘qoladi | Biz qayta ishga tushganda `(high_water_id, eng_yangi]` ni getHistory bilan to‘ldiramiz; yo‘qolgan **o‘chirish hodisalari** tiklanmaydi |
| 6.3 | "Ba’zi hollarda API ayrim kanal/superguruhlar uchun yangilanish yuborishni to‘xtatishi yoki kamaytirishi mumkin" | Davriy solishtirish (reconcile) — har kanalning eng yangi sahifasini tekshirish |
| 6.4 | Rasmiy klientlar ochiq turgan ko‘pi bilan 10 ta kanalni `getChannelDifference` bilan so‘rab turadi | Biz kanallarni doimiy so‘ramaymiz: dialoglar sinxroni har 10 daqiqada, kanal oynasi har soatda (`SYNC_INTERVAL_S`, `CHANNEL_REFRESH_INTERVAL_S`) |

## 7. O‘chirish hodisalari — tekshirildi

| # | Hodisa | Nima keladi | Bizning talqin |
|---|---|---|---|
| 7.1 | `updateDeleteMessages` (shaxsiy chat, oddiy guruh) | **faqat** xabar ID'lari, chat ko‘rsatilmaydi | Common box chatlari orasidan qidiriladi → yagona moslik `probable` |
| 7.2 | `updateDeleteChannelMessages` (kanal, superguruh) | `channel_id` + ID'lar | `confirmed` |
| 7.3 | Ikkalasida ham matn **kelmaydi** | — | Matn faqat oldindan arxivlangan bo‘lsa saqlanadi |
| 7.4 | `get_messages(chat, ids)` o‘chirilgan xabar uchun `None` qaytaradi (Telethon) | — | `probable` ni `confirmed` ga ko‘tarish uchun tekshiruv |

## 8. Tahrir hodisalari

| # | Cheklov | Ta’siri |
|---|---|---|
| 8.1 | `updateEditMessage` / `updateEditChannelMessage` xabarning **yangi to‘liq holatini** beradi, eskisini emas | Eski holat faqat bizda arxivlangan bo‘lsa saqlanadi |
| 8.2 | `edit_date` — oxirgi tahrir vaqti; `edit_hide` — tahrir belgisi ko‘rsatilmaydigan o‘zgarish | Eskirgan nusxani aniqlash `edit_date` bilan |
| 8.3 | Edit hodisasi har doim ham mazmun o‘zgarishi emas (link preview yuklanishi, tugmalar) | `content_hash` bilan solishtiriladi — keraksiz versiyalar yaratilmaydi |

## 9. Chat turlari bo‘yicha xulosa

| Tur | Tarix importi | O‘chirish hodisasi | ID ketma-ketligi | Alohida eslatma |
|---|---|---|---|---|
| Shaxsiy chat | To‘liq (mavjud xabarlar) | ID'lar, chatsiz → `probable` | umumiy | "Ikkala tomondan o‘chirish" sizda ham o‘chiradi |
| Bot bilan chat | To‘liq | ID'lar, chatsiz | umumiy | Sukut bo‘yicha arxivlanmaydi |
| Saved Messages | To‘liq | ID'lar, chatsiz | umumiy | Forward manbasi (`saved_from`) saqlanadi |
| Oddiy guruh | To‘liq | ID'lar, chatsiz | umumiy | Superguruhga aylantirilsa, eski tarix eski chatda qoladi (`migrated_to_chat_id`) |
| Superguruh | Qo‘shilgandan keyin yoki to‘liq (sozlamaga qarab) | `channel_id` bilan → `confirmed` | o‘z | Anonim adminda yuboruvchi = guruhning o‘zi |
| Forum | Superguruh kabi | `channel_id` bilan | o‘z | Mavzu ID'si `reply_to_top_id` da |
| Kanal | To‘liq (a’zo bo‘lsangiz) | `channel_id` bilan | o‘z | Real vaqt yangilanishlari kamaytirilishi mumkin; muallif ko‘pincha yo‘q |
| Secret chat | **Mumkin emas** | — | — | Doiradan tashqarida |

## 10. Media

| # | Cheklov | Ta’siri |
|---|---|---|
| 10.1 | Fayl hajmi 2 GB gacha (Premium bilan yuklangan fayllar 4 GB gacha) | `MEDIA_MAX_FILE_MB` bilan cheklanadi |
| 10.2 | `file_reference` vaqt o‘tib eskiradi (`FILE_REFERENCE_EXPIRED`) | Bazada saqlanmaydi; yuklashdan oldin xabar qayta olinadi |
| 10.3 | View-once media (`ttl_seconds`) | Hech qachon yuklanmaydi |
| 10.4 | Himoyalangan kontent (`noforwards`) | Media **hech qachon** yuklanmaydi (sozlab ham, override bilan ham bo‘lmaydi); matn arxivlanadi |
| 10.5 | Ba’zi fayllar CDN DC orqali beriladi | Telethon o‘zi boshqaradi |

## 10a. Kanal izohlari (comments)

| # | Cheklov | Ta’siri |
|---|---|---|
| 10a.1 | Kanal postiga izohlar kanalning o‘zida emas, unga bog‘langan **muhokama guruhida** (superguruh) saqlanadi; guruhda postning avtomatik nusxasi bo‘ladi | Izohlar o‘sha guruh chati ostida yoziladi; post nusxasi kontekst sifatida qoladi |
| 10a.2 | Izohlar `messages.getReplies` orqali olinadi (Telethon: `iter_messages(kanal, reply_to=post_id)`) | Har bir saqlangan post uchun alohida so‘rovlar — FloodWait hisobga olinadi |
| 10a.3 | Muhokama guruhiga a’zo bo‘lmasangiz, yangi izohlar real vaqtda kelmasligi mumkin | Oynadagi postlar izohlari har soatda qayta tekshiriladi (`CHANNEL_REFRESH_INTERVAL_S`) |
| 10a.4 | Muhokama guruhi bo‘lmagan kanalda izoh yo‘q | — |

Eslatma: 10a.1–10a.3 6-bosqichda haqiqiy akkauntda integratsion test bilan tekshiriladi.

## 11. Bot API (admin bot uchun)

| # | Cheklov | Ta’siri |
|---|---|---|
| 11.1 | Xabar matni ko‘pi bilan **4096** belgi | Natijalar sahifalanadi va qisqartiriladi |
| 11.2 | Caption ko‘pi bilan **1024** belgi | Media bilan uzun matn alohida yuboriladi |
| 11.3 | `callback_data` ko‘pi bilan **64 bayt** | Inline tugmalarda faqat qisqa kodlar |
| 11.4 | Bot yuboradigan fayl ko‘pi bilan **50 MB** (oddiy Bot API); local Bot API server bilan 2000 MB | Kattaroq media/eksport bo‘laklarga bo‘linadi yoki serverda qoladi |
| 11.5 | Bitta chatga sekundiga ~1 xabar, umumiy ~30 xabar/s | Sahifalash, xabarlarni birlashtirish |
| 11.6 | Bot foydalanuvchiga birinchi bo‘lib yoza olmaydi | Ikkinchi akkauntdan botga avval `/start` yuboring |
| 11.7 | Bot token sizib chiqsa, boshqa odam bot nomidan ishlay oladi (lekin arxiv bazasiga kira olmaydi) | @BotFather → `/revoke` |

Manba: core.telegram.org/bots/api, core.telegram.org/bots/faq
