# O'quv markazi Telegram bot

## Ishga tushirish

1. Python 3.10 yoki yangiroq versiyani o'rnating.
2. Bog'liqliklarni o'rnating: `python -m pip install -r requirements.txt`
3. `.env.example` faylidan `.env` nusxa oling va `BOT_TOKEN` hamda
   `SUPER_ADMIN_ID` hamda `DATABASE_ADMIN_PASSWORD` qiymatlarini o'zingiznikiga
   almashtiring.
4. Botni ishga tushiring: `python main.py`

Bot SQLite bazasini (`bot.sqlite3`) avtomatik yaratadi. Super Admin `/start`
orqali markaz nomi, markaz adminining Telegram ID si (ixtiyoriy `@username` bilan)
va ism-familiyasini kiritib,
markazni yaratadi. Karta raqami va karta egasining ismini markaz admini botdagi
**To'lov ma'lumotlari** bo'limidan kiritadi; ota-onaga ko'rsatiladigan admin
username'i ham shu yerda belgilanadi. Admin xodimlarni qo'shib, har bir guruhga
alohida biriktiradi va guruhlar hamda o'quvchilarni boshqaradi. O'qituvchi faqat
biriktirilgan guruhlarni ko'radi, o'quvchi qo'shadi va davomat yuritadi. Guruh
sahifasida o'quvchilar alohida **O'quvchilar ro'yxati** tugmasi orqali
ochiladi; ro'yxat sahifalarga bo'linib, har bir o'quvchi alohida tugmada chiqadi.
Xodim hamda ota-onani qo'shishda ID bilan birga ixtiyoriy `@username` ni, masalan
`123456789 @ali`, kiritish mumkin. Bot xususiy chatga xabar yuborishi uchun
Telegram ID kerak; username o'zi orqali shaxsiy foydalanuvchini aniqlab xabar
yuborib bo'lmaydi. Ota-onalar xabarlarni olish uchun avval botga
`/start` yuborishlari kerak. Kunlik to'lov eslatmalari `TIMEZONE` bo'yicha
soat 09:00 da yuboriladi. Bot 09:00 dan keyin ishga tushsa, bugungi eslatmalar
ishga tushishi bilan yuboriladi; bir kunda takroran yuborilmasligi uchun
bajarilgan sana bazada saqlanadi.

Ota-onalarga davomat xabarlarida markaz nomi va farzandining ism-familiyasi
guruh bilan birga ko'rsatiladi; kelmagan holatda markaz bilan bog'lanish izohi
beriladi. To'lov eslatmalari hamda tasdiqlari ham guruh, o'quvchi ismi va to'lov
tafsilotlarini o'z ichiga oladi; admin profili username orqali bosiladigan havola
sifatida beriladi (username bo'lmasa Telegram ID havolasi ishlatiladi). Adminning
kunlik hisoboti markaz nomi o'rniga guruhlar bo'yicha jamlanadi. Markaz nomi
xabarlarda doim `o'quv markazi` qo'shimchasi bilan ko'rsatiladi. Sanalar, masalan,
`2026-yil 6-noyabr` shaklida yoziladi. Bot xabarlari tegishli emoji belgilar,
sarlavhalar va bo'sh qatorlar bilan bo'limlarga ajratiladi; davomat, to'lov va
hisobot ma'lumotlari bir-biriga qo'shilib ketmaydigan ko'rinishda yuboriladi.

Super Admin panelidagi **🔐 Baza boshqaruvi** bo'limiga alohida parol bilan
kiriladi. Unda markazlar, admin va xodimlar, guruhga biriktirilgan o'qituvchilar,
o'quvchilar (ID, ota-ona Telegram ID/username, to'lov sanasi) hamda davomatni ko'rish,
markaz, xodim, guruh, guruh-xodim biriktirishi, o'quvchi va davomat yozuvlarini
qo'shish, tahrirlash yoki tasdiqlab o'chirish mumkin.
Butun bazani tozalash uchun `BAZANI TOZALASH` iborasini kiritib alohida tasdiqlash
talab qilinadi. Baza boshqaruvi 15 daqiqadan so'ng avtomatik qulflanadi.
O'qituvchi qo'shilayotganda ism-familiyasi va Telegram ID si, ixtiyoriy
username bilan birga, so'raladi.
Avvaldan bazada bo'lgan xodimlarning ismi bo'sh bo'lsa, ular botda `/start`
bosganda Telegram profilidagi ism bilan to'ldiriladi.

`.env` faylini va bot tokenini hech kimga yubormang yoki ommaviy repozitoriyga
qo'shmang.
