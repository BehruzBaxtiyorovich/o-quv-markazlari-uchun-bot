from __future__ import annotations

import hmac
import logging
import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import DATABASE_ADMIN_PASSWORD, SUPER_ADMIN_ID, TIMEZONE
from date_utils import (
    format_center_name,
    format_uzbek_date,
    normalize_telegram_username,
    parse_telegram_identity,
)
from database import (
    add_teacher,
    clear_managed_data,
    centers_for_user,
    create_center,
    create_group,
    create_student,
    delete_managed_record,
    delete_student,
    get_managed_record,
    get_center,
    get_group,
    get_student,
    group_role,
    group_teacher_ids,
    list_centers,
    list_center_teachers,
    list_center_staff_page,
    list_groups_page,
    list_groups_for_user,
    list_centers_page,
    list_group_staff_details,
    list_students_page,
    list_attendance_page,
    list_managed_records,
    list_students,
    save_attendance,
    set_center_active,
    set_group_teacher,
    staff_role,
    insert_managed_record,
    update_managed_record,
    update_center_payment_details,
    update_staff_full_name,
    update_student,
)

router = Router()
logger = logging.getLogger(__name__)
DATABASE_PAGE_SIZE = 8
DATABASE_AUTH_SECONDS = 900
DATABASE_TABLES = {
    "centers": ("Markazlar", ("name", "card_number", "card_holder", "admin_telegram", "admin_username", "is_active")),
    "staff": ("Xodimlar", ("center_id", "telegram_id", "full_name", "username", "role")),
    "groups": ("Guruhlar", ("center_id", "name")),
    "group_staff": ("Guruh-xodim biriktirishlari", ("group_id", "telegram_id")),
    "students": ("O'quvchilar", ("group_id", "full_name", "parent_telegram_id", "parent_username", "balance_date")),
    "attendance": ("Davomat", ("student_id", "date", "status")),
}
DATABASE_FIELD_LABELS = {
    "name": "Nomi",
    "card_number": "Karta raqami",
    "card_holder": "Karta egasi",
    "admin_telegram": "Admin Telegram ID",
    "admin_username": "Admin username",
    "is_active": "Faollik (1/0)",
    "center_id": "Markaz ID",
    "telegram_id": "Telegram ID",
    "username": "Telegram username (ixtiyoriy, - bo'lsa bo'sh)",
    "full_name": "Ism-familiya",
    "role": "Rol (admin/teacher)",
    "group_id": "Guruh ID",
    "parent_telegram_id": "Ota-ona Telegram ID",
    "parent_username": "Ota-ona username (ixtiyoriy, - bo'lsa bo'sh)",
    "balance_date": "To'lov sanasi (YYYY-MM-DD)",
    "student_id": "O'quvchi ID",
    "date": "Davomat sanasi (YYYY-MM-DD)",
    "status": "Holat (Keldi/Kelmadi)",
}


class CenterWizard(StatesGroup):
    name = State()
    admin_id = State()
    admin_full_name = State()


class CenterPaymentWizard(StatesGroup):
    card_number = State()
    card_holder = State()
    admin_username = State()


class GroupWizard(StatesGroup):
    name = State()


class TeacherWizard(StatesGroup):
    full_name = State()
    telegram_id = State()


class DatabaseWizard(StatesGroup):
    password = State()
    value = State()
    clear_confirm = State()


class StudentWizard(StatesGroup):
    full_name = State()
    parent_id = State()
    balance_date = State()
    edit_name = State()
    edit_parent = State()
    edit_date = State()


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if query.message:
        await query.message.edit_text(text, reply_markup=markup)


async def _authorized(
    query: CallbackQuery, center_id: int, admin_only: bool = False
) -> tuple[dict | None, str | None]:
    role = await staff_role(center_id, query.from_user.id)
    if role is None:
        await query.answer("⛔ Bu markazga kirish huquqingiz yo'q.", show_alert=True)
        return None, None
    center = await get_center(center_id)
    if center is None:
        await query.answer("🔎 Markaz topilmadi.", show_alert=True)
        return None, None
    if not center["is_active"]:
        await query.answer("⚠️ Markaz obunasi faol emas. Administratorga murojaat qiling.", show_alert=True)
        if query.message:
            await query.message.answer(
                "⚠️ Markaz vaqtincha bloklangan. Obuna to'lovi tasdiqlanishi uchun "
                "markaz administratoriga murojaat qiling."
            )
        return None, None
    if admin_only and role != "admin":
        await query.answer("🔒 Bu amal faqat markaz admini uchun.", show_alert=True)
        return None, None
    return dict(center), role


async def _message_authorized(
    message: Message, center_id: int, admin_only: bool = False
) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    role = await staff_role(center_id, user_id)
    center = await get_center(center_id)
    if not role or not center:
        await message.answer("⛔ Bu markaz uchun ruxsat yo'q yoki markaz topilmadi.")
        return False
    if not center["is_active"]:
        await message.answer(
            "⚠️ Markaz vaqtincha bloklangan. Obuna to'lovi tasdiqlanishi uchun "
            "markaz administratoriga murojaat qiling."
        )
        return False
    if admin_only and role != "admin":
        await message.answer("🔒 Bu amal faqat markaz admini uchun.")
        return False
    return True


async def _student_belongs_to_center(student_id: int, center_id: int) -> bool:
    student = await get_student(student_id)
    if not student:
        return False
    group = await get_group(student["group_id"])
    return bool(group and group["center_id"] == center_id)


async def _message_group_authorized(
    message: Message, center_id: int, group_id: int, admin_only: bool = False
) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    role = await group_role(center_id, group_id, user_id)
    if role is None:
        await message.answer("⛔ Bu guruhga kirish huquqingiz yo'q.")
        return False
    if admin_only and role != "admin":
        await message.answer("🔒 Bu amal faqat markaz admini uchun.")
        return False
    center = await get_center(center_id)
    if not center or not center["is_active"]:
        await message.answer("⚠️ Markaz faol emas.\n\n📞 Administratorga murojaat qiling.")
        return False
    return True


async def _student_access_role(
    student_id: int, center_id: int, telegram_id: int
) -> str | None:
    student = await get_student(student_id)
    if not student:
        return None
    group = await get_group(student["group_id"])
    if not group or group["center_id"] != center_id:
        return None
    return await group_role(center_id, group["id"], telegram_id)


def _today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _parse_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Date must use YYYY-MM-DD format")
    return parsed


def _display_date(value: str) -> str:
    try:
        return format_uzbek_date(date.fromisoformat(value))
    except ValueError:
        return f"⚠️ Noto'g'ri sana: {value}"


def _next_month_same_day(current: date) -> date:
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


async def _render_center(
    query: CallbackQuery, center: dict, role: str, telegram_id: int
) -> None:
    groups = await list_groups_for_user(center["id"], telegram_id, role)
    rows = []
    if role == "admin":
        payment_label = (
            "✏️ To'lov ma'lumotlarini tahrirlash"
            if center["card_number"] or center["card_holder"] or center["admin_username"]
            else "💳 To'lov ma'lumotlarini kiritish"
        )
        rows.extend(
            [
                [InlineKeyboardButton(text=payment_label, callback_data=f"paysetup:{center['id']}")],
                [InlineKeyboardButton(text="➕ Guruh qo'shish", callback_data=f"gadd:{center['id']}")],
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text=f"📁 {group['name']}", callback_data=f"group:{center['id']}:{group['id']}")]
            for group in groups
        ]
    )
    text = (
        f"🏫 {format_center_name(center['name'])}\n\n"
        "📂 Kerakli guruhni tanlang:"
    )
    if not groups and role == "teacher":
        text = (
            f"🏫 {format_center_name(center['name'])}\n\n"
            "ℹ️ Sizga hozircha guruh biriktirilmagan."
        )
    await _edit(query, text, _keyboard(rows))


async def _render_group(
    query: CallbackQuery, center_id: int, group_id: int, role: str
) -> None:
    group = await get_group(group_id)
    if group is None or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    students = await list_students(group_id)
    rows = [
        [
            InlineKeyboardButton(
                text=f"👨‍🎓 O'quvchilar ro'yxati ({len(students)})",
                callback_data=f"groupstudents:{center_id}:{group_id}:0",
            )
        ],
        [
            InlineKeyboardButton(
                text="➕ O'quvchi qo'shish",
                callback_data=f"sadd:{center_id}:{group_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📝 Davomat", callback_data=f"att:{center_id}:{group_id}"
            )
        ],
    ]
    if role == "admin":
        rows.extend(
            [
                [InlineKeyboardButton(text="➕ O'qituvchini guruhga qo'shish", callback_data=f"gnewteacher:{center_id}:{group_id}")],
                [InlineKeyboardButton(text="👥 Biriktirilgan o'qituvchilar", callback_data=f"gstaff:{center_id}:{group_id}")],
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Guruhlar", callback_data=f"center:{center_id}")]
    )
    text = (
        f"📁 {group['name']}\n\n"
        f"👨‍🎓 O'quvchilar soni: {len(students)}\n\n"
        "👇 Kerakli amalni tanlang:"
    )
    await _edit(query, text, _keyboard(rows))


async def _render_group_students(
    query: CallbackQuery, center_id: int, group_id: int, page: int
) -> None:
    group = await get_group(group_id)
    if group is None or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    students = await list_students_page(
        group_id, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(students) > DATABASE_PAGE_SIZE
    students = students[:DATABASE_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=f"👤 {student['full_name']}"[:60],
                callback_data=f"student:{center_id}:{student['id']}",
            )
        ]
        for student in students
    ]
    page_buttons = _page_buttons(
        f"groupstudents:{center_id}:{group_id}", page, has_next
    )
    if page_buttons:
        rows.append(page_buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Guruh menyusiga qaytish",
                callback_data=f"group:{center_id}:{group_id}",
            )
        ]
    )
    text = (
        f"📁 {group['name']}\n\n"
        f"👨‍🎓 O'quvchilar ro'yxati — {len(students)} ta ko'rsatilmoqda\n\n"
        "👇 O'quvchi profilini ochish uchun ismini tanlang:"
    )
    if not students:
        text = f"📁 {group['name']}\n\nℹ️ Guruhda hozircha o'quvchi yo'q."
    await _edit(query, text, _keyboard(rows))


async def _render_student(
    query: CallbackQuery, center_id: int, student_id: int, role: str
) -> None:
    student = await get_student(student_id)
    if student is None:
        await query.answer("🔎 O'quvchi topilmadi.", show_alert=True)
        return
    group = await get_group(student["group_id"])
    if group is None or group["center_id"] != center_id:
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    rows = []
    if role == "admin":
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="✏️ Ismni tahrirlash",
                        callback_data=f"editname:{center_id}:{student_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📱 Ota-ona ma'lumotini tahrirlash",
                        callback_data=f"editparent:{center_id}:{student_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📅 Keyingi oyga uzaytirish",
                        callback_data=f"extend:{center_id}:{student_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗓 Sanani qo'lda kiritish",
                        callback_data=f"editdate:{center_id}:{student_id}",
                    )
                ],
            ]
        )
    if role == "admin":
        rows.append(
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"delask:{center_id}:{student_id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Guruhga qaytish", callback_data=f"group:{center_id}:{group['id']}")]
    )
    text = (
        f"👤 {student['full_name']}\n"
        f"🆔 O'quvchi ID: {student['id']}\n\n"
        f"👪 Ota-ona ID: {student['parent_telegram_id']}\n"
        f"🔗 Ota-ona username: "
        f"{'@' + student['parent_username'] if student['parent_username'] else 'kiritilmagan'}\n\n"
        f"💳 To'lov muddati: "
        f"{_display_date(student['balance_date'])}"
    )
    await _edit(query, text, _keyboard(rows))


@router.message(F.text == "/start")
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    if user_id == SUPER_ADMIN_ID:
        await message.answer(
            "👑 Super Admin paneli\n\n👇 Kerakli bo'limni tanlang:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="🏫 Markazlarni boshqarish", callback_data="sa:panel")]]
            ),
        )
        return
    centers = await centers_for_user(user_id)
    if not centers:
        await message.answer("⛔ Sizga markaz xodimi sifatida ruxsat berilmagan.")
        return
    for center in centers:
        profile_name = message.from_user.full_name if message.from_user else ""
        if profile_name:
            profile_username = message.from_user.username or ""
            await update_staff_full_name(
                center["id"], user_id, profile_name, profile_username
            )
        if not center["is_active"]:
            await message.answer(
                f"⚠️ {format_center_name(center['name'])} vaqtincha bloklangan.\n\n"
                "📞 Obuna to'lovi bo'yicha markaz administratoriga murojaat qiling."
            )
        else:
            role = await staff_role(center["id"], user_id)
            rows = []
            if role == "admin":
                payment_label = (
                    "✏️ To'lov ma'lumotlarini tahrirlash"
                    if center["card_number"] or center["card_holder"] or center["admin_username"]
                    else "💳 To'lov ma'lumotlarini kiritish"
                )
                rows.append(
                    [InlineKeyboardButton(
                        text=payment_label,
                        callback_data=f"paysetup:{center['id']}",
                    )]
                )
            rows.append(
                [InlineKeyboardButton(
                    text="📁 Guruhlar va o'quvchilar",
                    callback_data=f"center:{center['id']}",
                )]
            )
            await message.answer(
                f"🏫 {format_center_name(center['name'])}",
                reply_markup=_keyboard(rows),
            )


@router.callback_query(F.data == "sa:panel")
async def super_admin_panel(query: CallbackQuery) -> None:
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    centers = await list_centers()
    rows = []
    for center in centers:
        state = "🔴 Bloklash" if center["is_active"] else "🟢 Faollashtirish"
        next_active = 0 if center["is_active"] else 1
        rows.append(
            [InlineKeyboardButton(
                text=f"{format_center_name(center['name'])} — {state}",
                callback_data=f"sa:toggle:{center['id']}:{next_active}",
            )]
        )
    rows.append([InlineKeyboardButton(text="➕ Yangi markaz ochish", callback_data="sa:new")])
    rows.append([InlineKeyboardButton(text="🔐 Baza boshqaruvi", callback_data="db:unlock")])
    await _edit(query, "🏫 Markazlarni boshqarish\n\n👇 Markazni tanlang:", _keyboard(rows))
    await query.answer()


async def _super_admin_only(query: CallbackQuery) -> bool:
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return False
    return True


async def _database_authorized(
    query: CallbackQuery, state: FSMContext
) -> bool:
    if not await _super_admin_only(query):
        return False
    data = await state.get_data()
    expires_at = data.get("database_auth_expires", 0)
    if expires_at > datetime.now().timestamp():
        return True
    await state.set_state(DatabaseWizard.password)
    if query.message:
        await query.message.answer(
            "🔐 Baza boshqaruvi parolini yuboring:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="db:authcancel")]]
            ),
        )
    await query.answer()
    return False


@router.callback_query(F.data == "db:unlock")
async def database_unlock_start(
    query: CallbackQuery, state: FSMContext
) -> None:
    if not await _super_admin_only(query):
        return
    if not DATABASE_ADMIN_PASSWORD:
        await query.answer(
            "⚠️ Baza paroli sozlanmagan.\n\n"
            "🔧 .env faylida DATABASE_ADMIN_PASSWORD ni sozlang.",
            show_alert=True,
        )
        return
    await state.update_data(database_auth_expires=0)
    await state.set_state(DatabaseWizard.password)
    if query.message:
        await query.message.answer(
            "🔐 Baza boshqaruvi parolini yuboring:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="db:authcancel")]]
            ),
        )
    await query.answer()


@router.message(DatabaseWizard.password)
async def database_unlock_finish(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    if not DATABASE_ADMIN_PASSWORD:
        await state.clear()
        await message.answer("⚠️ Baza paroli sozlanmagan.\n\n🔧 .env sozlamasini tekshiring.")
        return
    supplied_password = (message.text or "").strip()
    try:
        await message.delete()
    except TelegramAPIError as error:
        logger.warning("Could not remove database password message: %s", error)
    if not hmac.compare_digest(supplied_password, DATABASE_ADMIN_PASSWORD):
        await message.answer("❌ Parol noto'g'ri.\n\n🔁 Qayta urinib ko'ring yoki bekor qiling.")
        return
    await state.update_data(
        database_auth_expires=datetime.now().timestamp() + DATABASE_AUTH_SECONDS
    )
    await state.set_state(None)
    await message.answer(
        "✅ Baza boshqaruvi ochildi.\n\n"
        "⏱ Ruxsat 15 daqiqadan so'ng avtomatik yopiladi.",
        reply_markup=_keyboard(
            [[InlineKeyboardButton(text="🗄 Baza jadvallarini boshqarish", callback_data="dbm:tables")],
             [InlineKeyboardButton(text="🔒 Hozir qulflash", callback_data="db:lock")]]
        ),
    )


@router.callback_query(F.data == "db:authcancel")
async def database_auth_cancel(
    query: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(None)
    if query.message:
        await query.message.edit_text(
            "↩️ Baza boshqaruviga kirish bekor qilindi.",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(
                    text="⬅️ Super Admin paneli", callback_data="sa:panel"
                )]]
            ),
        )
    await query.answer()


@router.callback_query(F.data == "db:lock")
async def database_lock(query: CallbackQuery, state: FSMContext) -> None:
    if not await _super_admin_only(query):
        return
    await state.update_data(database_auth_expires=0)
    await state.set_state(None)
    await query.answer("🔒 Baza qulflandi.")
    await super_admin_panel(query)


def _page_buttons(
    prefix: str, page: int, has_next: bool
) -> list[InlineKeyboardButton]:
    buttons = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi sahifa",
                callback_data=f"{prefix}:{page - 1}",
            )
        )
    if has_next:
        buttons.append(
            InlineKeyboardButton(
                text="Keyingi sahifa ➡️",
                callback_data=f"{prefix}:{page + 1}",
            )
        )
    return buttons


def _managed_key(table: str, record: dict) -> str:
    if table == "group_staff":
        return f"{record['group_id']}_{record['telegram_id']}"
    return str(record["id"])


def _managed_record_title(table: str, record: dict) -> str:
    if table == "centers":
        return f"{record['id']} — {record['name']}"
    if table == "staff":
        return (
            f"{record['full_name'] or 'Ism kiritilmagan'} "
            f"({record['role']}, ID {record['telegram_id']})"
        )
    if table == "groups":
        return f"{record['id']} — {record['name']} (markaz {record['center_id']})"
    if table == "group_staff":
        return f"Guruh {record['group_id']} — xodim {record['telegram_id']}"
    if table == "students":
        return f"{record['id']} — {record['full_name']}"
    return (
        f"{record['id']} — o'quvchi {record['student_id']}, "
        f"{record['date']} ({record['status']})"
    )


async def _show_managed_tables(query: CallbackQuery) -> None:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📋 {label}",
                callback_data=f"dbm:table:{table}:0",
            )
        ]
        for table, (label, _) in DATABASE_TABLES.items()
    ]
    rows.extend(
        [
            [InlineKeyboardButton(
                text="🧨 Butun bazani tozalash",
                callback_data="dbm:clearask",
            )],
            [InlineKeyboardButton(
                text="🔒 Bazani qulflash", callback_data="db:lock"
            )],
        ]
    )
    await _edit(
        query,
        "🗄 Baza boshqaruvi\n\n👇 Ko'rish yoki boshqarish uchun jadvalni tanlang:",
        _keyboard(rows),
    )


async def _show_managed_table(
    query: CallbackQuery, table: str, page: int
) -> None:
    if table not in DATABASE_TABLES:
        await query.answer("🔎 Jadval topilmadi.", show_alert=True)
        return
    records = await list_managed_records(
        table, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(records) > DATABASE_PAGE_SIZE
    records = records[:DATABASE_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=_managed_record_title(table, dict(record))[:60],
                callback_data=f"dbm:record:{table}:{_managed_key(table, dict(record))}",
            )
        ]
        for record in records
    ]
    buttons = _page_buttons(f"dbm:table:{table}", page, has_next)
    if buttons:
        rows.append(buttons)
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Yangi yozuv qo'shish", callback_data=f"dbm:add:{table}:{page}")],
            [InlineKeyboardButton(text="⬅️ Jadvallar ro'yxati", callback_data="dbm:tables")],
        ]
    )
    label = DATABASE_TABLES[table][0]
    text = f"📋 {label}\n\n📊 Ko'rsatilmoqda: {len(records)} ta yozuv"
    if not records:
        text = f"📋 {label}\n\nℹ️ Jadval hozircha bo'sh."
    await _edit(query, text, _keyboard(rows))


async def _show_managed_record(
    query: CallbackQuery, table: str, key: str
) -> None:
    if table not in DATABASE_TABLES:
        await query.answer("🔎 Jadval topilmadi.", show_alert=True)
        return
    record = await get_managed_record(table, key)
    if record is None:
        await query.answer("🔎 Yozuv topilmadi.", show_alert=True)
        return
    record_data = dict(record)
    _, editable_fields = DATABASE_TABLES[table]
    rows = [
        [
            InlineKeyboardButton(
                text=f"✏️ {DATABASE_FIELD_LABELS.get(field, field)}",
                callback_data=f"dbm:edit:{table}:{key}:{field}",
            )
        ]
        for field in editable_fields
    ]
    rows.extend(
        [
            [InlineKeyboardButton(
                text="🗑 Yozuvni o'chirish",
                callback_data=f"dbm:deleteask:{table}:{key}",
            )],
            [InlineKeyboardButton(
                text="⬅️ Jadvalga qaytish",
                callback_data=f"dbm:table:{table}:0",
            )],
        ]
    )
    content = "\n".join(
        f"🔹 {field}: {value}" for field, value in record_data.items()
    )
    await _edit(
        query,
        f"📝 {DATABASE_TABLES[table][0]}\n\n{content}",
        _keyboard(rows),
    )


@router.callback_query(F.data == "dbm:tables")
async def managed_tables(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    await _show_managed_tables(query)
    await query.answer()


@router.callback_query(F.data.startswith("dbm:table:"))
async def managed_table(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, page_text = query.data.split(":")
    await _show_managed_table(query, table, max(0, int(page_text)))
    await query.answer()


@router.callback_query(F.data.startswith("dbm:record:"))
async def managed_record(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, key = query.data.split(":", 3)
    await _show_managed_record(query, table, key)
    await query.answer()


@router.callback_query(F.data.startswith("dbm:add:"))
async def managed_record_add(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, page_text = query.data.split(":")
    if table not in DATABASE_TABLES:
        await query.answer("🔎 Jadval topilmadi.", show_alert=True)
        return
    fields = list(DATABASE_TABLES[table][1])
    await state.update_data(
        database_operation="add",
        database_table=table,
        database_page=max(0, int(page_text)),
        database_fields=fields,
        database_values={},
        database_field_index=0,
    )
    await state.set_state(DatabaseWizard.value)
    first_field = fields[0]
    if query.message:
        await query.message.answer(
            f"✍️ {DATABASE_FIELD_LABELS.get(first_field, first_field)}\n\n"
            "Qiymatini yuboring:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="dbm:cancel")]]
            ),
        )
    await query.answer()


@router.callback_query(F.data.startswith("dbm:edit:"))
async def managed_record_edit(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, key, field = query.data.split(":")
    if table not in DATABASE_TABLES or field not in DATABASE_TABLES[table][1]:
        await query.answer("⚠️ Bu maydonni tahrirlab bo'lmaydi.", show_alert=True)
        return
    record = await get_managed_record(table, key)
    if record is None:
        await query.answer("🔎 Yozuv topilmadi.", show_alert=True)
        return
    await state.update_data(
        database_operation="edit",
        database_table=table,
        database_key=key,
        database_field=field,
    )
    await state.set_state(DatabaseWizard.value)
    if query.message:
        await query.message.answer(
            f"✏️ Maydonni tahrirlash\n\n"
            f"Yangi qiymat: {DATABASE_FIELD_LABELS.get(field, field)}",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="dbm:cancel")]]
            ),
        )
    await query.answer()


@router.callback_query(F.data == "dbm:cancel")
async def managed_edit_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    data = await state.get_data()
    table = data.get("database_table")
    key = data.get("database_key")
    page = data.get("database_page", 0)
    await state.set_state(None)
    if table and key:
        await _show_managed_record(query, table, key)
    elif table:
        await _show_managed_table(query, table, page)
    else:
        await _show_managed_tables(query)
    await query.answer("↩️ Amal bekor qilindi.")


def _validate_managed_value(field: str, value: str) -> str:
    value = value.strip()
    if field in {"username", "parent_username", "admin_username"}:
        if value in {"", "-"}:
            return ""
        return normalize_telegram_username(value)
    if not value:
        raise ValueError("Qiymat bo'sh bo'lmasligi kerak.")
    integer_fields = {
        "admin_telegram", "is_active", "center_id", "telegram_id",
        "group_id", "parent_telegram_id", "student_id",
    }
    if field in integer_fields:
        parsed = int(value)
        if field == "is_active" and parsed not in (0, 1):
            raise ValueError("Faollik qiymati faqat 0 yoki 1 bo'lishi mumkin.")
    if field in {"date", "balance_date"}:
        parsed_date = date.fromisoformat(value)
        if parsed_date.isoformat() != value:
            raise ValueError("Sana YYYY-MM-DD formatida bo'lishi kerak.")
    if field == "role" and value not in {"admin", "teacher"}:
        raise ValueError("Rol admin yoki teacher bo'lishi kerak.")
    if field == "status" and value not in {"Keldi", "Kelmadi"}:
        raise ValueError("Davomat holati Keldi yoki Kelmadi bo'lishi kerak.")
    return value


@router.message(DatabaseWizard.value)
async def managed_value_input(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    data = await state.get_data()
    if data.get("database_auth_expires", 0) <= datetime.now().timestamp():
        await state.clear()
        await message.answer("⏱ Baza ruxsati tugadi.\n\n👑 Super Admin panelidan qayta kiring.")
        return
    table = data["database_table"]
    operation = data["database_operation"]
    try:
        if operation == "edit":
            field = data["database_field"]
            value = _validate_managed_value(field, message.text or "")
            await update_managed_record(table, data["database_key"], field, value)
            await state.set_state(None)
            await message.answer("✅ Yozuv yangilandi.")
            record = await get_managed_record(table, data["database_key"])
            if record:
                await message.answer(
                            f"📝 {DATABASE_TABLES[table][0]}\n\n"
                            + "\n".join(
                                f"🔹 {key}: {value}"
                                for key, value in dict(record).items()
                            ),
                    reply_markup=_keyboard(
                        [[InlineKeyboardButton(
                            text="⬅️ Yozuvga qaytish",
                            callback_data=f"dbm:record:{table}:{data['database_key']}",
                        )]]
                    ),
                )
            return

        fields = data["database_fields"]
        index = data["database_field_index"]
        field = fields[index]
        values = data["database_values"]
        values[field] = _validate_managed_value(field, message.text or "")
        index += 1
        if index < len(fields):
            await state.update_data(database_values=values, database_field_index=index)
            next_field = fields[index]
            await message.answer(
                f"✍️ {DATABASE_FIELD_LABELS.get(next_field, next_field)}\n\n"
                "Qiymatini yuboring:",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="dbm:cancel")]]
                ),
            )
            return
        await insert_managed_record(table, values)
        await state.set_state(None)
        await message.answer("✅ Yozuv bazaga qo'shildi.")
        await message.answer(
            f"📋 {DATABASE_TABLES[table][0]}",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(
                    text="⬅️ Jadvalga qaytish",
                    callback_data=f"dbm:table:{table}:{data.get('database_page', 0)}",
                )]]
            ),
        )
    except (ValueError, aiosqlite.IntegrityError) as error:
        await state.set_state(None)
        logger.info("Database admin operation rejected for %s: %s", table, error)
        await message.answer(
            f"❌ O'zgarish saqlanmadi: {error}",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(
                    text="⬅️ Jadvalga qaytish",
                    callback_data=f"dbm:table:{table}:{data.get('database_page', 0)}",
                )]]
            ),
        )


@router.callback_query(F.data.startswith("dbm:deleteask:"))
async def managed_delete_ask(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, key = query.data.split(":", 3)
    if table not in DATABASE_TABLES or await get_managed_record(table, key) is None:
        await query.answer("🔎 Yozuv topilmadi.", show_alert=True)
        return
    await _edit(
        query,
        "⚠️ Shu yozuvni o'chirishni tasdiqlaysizmi?",
        _keyboard(
            [[
                InlineKeyboardButton(
                    text="🗑 Ha, o'chirish",
                    callback_data=f"dbm:delete:{table}:{key}",
                ),
                InlineKeyboardButton(
                    text="↩️ Bekor qilish",
                    callback_data=f"dbm:record:{table}:{key}",
                ),
            ]]
        ),
    )
    await query.answer()


@router.callback_query(F.data.startswith("dbm:delete:"))
async def managed_delete(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, table, key = query.data.split(":", 3)
    if table not in DATABASE_TABLES:
        await query.answer("🔎 Jadval topilmadi.", show_alert=True)
        return
    try:
        await delete_managed_record(table, key)
    except aiosqlite.IntegrityError as error:
        await query.answer(f"Yozuvni o'chirib bo'lmadi: {error}", show_alert=True)
        return
    await query.answer("🗑 Yozuv o'chirildi.")
    await _show_managed_table(query, table, 0)


@router.callback_query(F.data == "dbm:clearask")
async def managed_clear_ask(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    await state.set_state(DatabaseWizard.clear_confirm)
    if query.message:
        await query.message.answer(
            "⚠️ DIQQAT: bu amal barcha markaz, xodim, guruh, o'quvchi va davomat "
            "ma'lumotlarini butunlay o'chiradi.\n\n"
            "✅ Tasdiqlash uchun aynan shu matnni yuboring:\n"
            "BAZANI TOZALASH",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="dbm:clearcancel")]]
            ),
        )
    await query.answer()


@router.callback_query(F.data == "dbm:clearcancel")
async def managed_clear_cancel(
    query: CallbackQuery, state: FSMContext
) -> None:
    if not await _database_authorized(query, state):
        return
    await state.set_state(None)
    await _show_managed_tables(query)
    await query.answer("↩️ Bazani tozalash bekor qilindi.")


@router.message(DatabaseWizard.clear_confirm)
async def managed_clear_confirm(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    data = await state.get_data()
    if data.get("database_auth_expires", 0) <= datetime.now().timestamp():
        await state.clear()
        await message.answer("⏱ Baza ruxsati tugadi.\n\n👑 Super Admin panelidan qayta kiring.")
        return
    if (message.text or "").strip() != "BAZANI TOZALASH":
        await state.set_state(None)
        await message.answer(
            "↩️ Umumiy o'chirish bekor qilindi.",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(
                    text="⬅️ Baza jadvallariga qaytish",
                    callback_data="dbm:tables",
                )]]
            ),
        )
        return
    await clear_managed_data()
    await state.set_state(None)
    await message.answer("✅ Barcha biznes ma'lumotlari o'chirildi.")


@router.callback_query(F.data.startswith("db:centers:"))
async def database_centers(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    page = max(0, int(query.data.rsplit(":", 1)[1]))
    rows_data = await list_centers_page(
        DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(rows_data) > DATABASE_PAGE_SIZE
    centers = rows_data[:DATABASE_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if center['is_active'] else '🔴'} {format_center_name(center['name'])}"[:60],
                callback_data=f"db:center:{center['id']}:0",
            )
        ]
        for center in centers
    ]
    buttons = _page_buttons("db:centers", page, has_next)
    if buttons:
        rows.append(buttons)
    rows.append(
        [InlineKeyboardButton(text="⬅️ Super Admin paneli", callback_data="sa:panel")]
    )
    text = "🗄 Baza — markazlar\n\n🟢 Faol   |   🔴 Bloklangan"
    if centers:
        text += "\n\n" + "\n\n".join(
            f"{'🟢' if center['is_active'] else '🔴'} "
            f"{format_center_name(center['name'])}\n"
            f"   📁 Guruhlar: {center['group_count']}\n"
            f"   👨‍🎓 O'quvchilar: {center['student_count']}"
            for center in centers
        )
    if not centers:
        text += "\n\nℹ️ Hozircha markazlar yo'q."
    await _edit(query, text, _keyboard(rows))
    await query.answer()


@router.callback_query(F.data.startswith("db:center:"))
async def database_center(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, center_text, page_text = query.data.split(":")
    center_id, page = int(center_text), max(0, int(page_text))
    center = await get_center(center_id)
    if not center:
        await query.answer("🔎 Markaz topilmadi.", show_alert=True)
        return
    rows_data = await list_groups_page(
        center_id, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(rows_data) > DATABASE_PAGE_SIZE
    groups = rows_data[:DATABASE_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=f"📁 {group['name']}"[:60],
                callback_data=f"db:group:{center_id}:{group['id']}:0",
            )
        ]
        for group in groups
    ]
    buttons = _page_buttons(f"db:center:{center_id}", page, has_next)
    if buttons:
        rows.append(buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text="👥 Admin va xodimlar",
                callback_data=f"db:staff:{center_id}:0",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Markazlar", callback_data="db:centers:0")]
    )
    active_text = "Faol" if center["is_active"] else "Bloklangan"
    payment_username = center["admin_username"] or "kiritilmagan"
    staff = await list_center_staff_page(center_id, 1, 0)
    admin = next((member for member in staff if member["role"] == "admin"), None)
    text = (
        f"🏫 {format_center_name(center['name'])}\n"
        f"{'🟢' if center['is_active'] else '🔴'} Holati: {active_text}\n\n"
        f"🆔 Markaz ID: {center['id']}\n"
        f"👤 Admin: {(admin['full_name'] or 'Ism kiritilmagan') if admin else 'topilmadi'} "
        f"— ID {center['admin_telegram']}"
        f"{' — @' + admin['username'] if admin and admin['username'] else ''}\n"
        f"🔗 Admin username: {payment_username}\n\n"
        f"💳 Karta: {center['card_number'] or 'kiritilmagan'}\n"
        f"👤 Karta egasi: {center['card_holder'] or 'kiritilmagan'}\n\n"
        "📂 Guruhlar:"
    )
    if not groups:
        text += "\n\nℹ️ Markazda guruh yo'q."
    await _edit(query, text, _keyboard(rows))
    await query.answer()


@router.callback_query(F.data.startswith("db:staff:"))
async def database_staff(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, center_text, page_text = query.data.split(":")
    center_id, page = int(center_text), max(0, int(page_text))
    center = await get_center(center_id)
    if not center:
        await query.answer("🔎 Markaz topilmadi.", show_alert=True)
        return
    rows_data = await list_center_staff_page(
        center_id, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(rows_data) > DATABASE_PAGE_SIZE
    staff = rows_data[:DATABASE_PAGE_SIZE]
    rows = []
    buttons = _page_buttons(f"db:staff:{center_id}", page, has_next)
    if buttons:
        rows.append(buttons)
    rows.append(
        [InlineKeyboardButton(
            text="⬅️ Markaz ma'lumotlari",
            callback_data=f"db:center:{center_id}:0",
        )]
    )
    lines = []
    for member in staff:
        role_label = "🏛 Admin" if member["role"] == "admin" else "👨‍🏫 O'qituvchi"
        lines.append(
            f"{role_label}: {member['full_name'] or 'Ism kiritilmagan'} "
            f"— ID {member['telegram_id']}"
            f"{' — @' + member['username'] if member['username'] else ''}"
        )
    text = f"👥 {format_center_name(center['name'])}\n\n"
    text += "\n\n".join(lines) if lines else "ℹ️ Xodimlar ro'yxati bo'sh."
    await _edit(query, text, _keyboard(rows))
    await query.answer()


@router.callback_query(F.data.startswith("db:group:"))
async def database_group(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, center_text, group_text, page_text = query.data.split(":")
    center_id, group_id = int(center_text), int(group_text)
    page = max(0, int(page_text))
    group = await get_group(group_id)
    if not group or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    rows_data = await list_students_page(
        group_id, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(rows_data) > DATABASE_PAGE_SIZE
    students = rows_data[:DATABASE_PAGE_SIZE]
    teachers = await list_group_staff_details(group_id)
    rows = [
        [
            InlineKeyboardButton(
                text=f"👤 {student['full_name']}"[:60],
                callback_data=(
                    f"db:student:{center_id}:{group_id}:{student['id']}:0"
                ),
            )
        ]
        for student in students
    ]
    buttons = _page_buttons(
        f"db:group:{center_id}:{group_id}", page, has_next
    )
    if buttons:
        rows.append(buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Guruhlar",
                callback_data=f"db:center:{center_id}:0",
            )
        ]
    )
    teacher_lines = [
        f"{teacher['full_name'] or 'Ism kiritilmagan'} — ID {teacher['telegram_id']}"
        f"{' — @' + teacher['username'] if teacher['username'] else ''}"
        for teacher in teachers
    ]
    student_lines = [
        f"{student['full_name']} — ID {student['id']}, ota-ona ID "
        f"{student['parent_telegram_id']}"
        f"{' — @' + student['parent_username'] if student['parent_username'] else ''}, "
        f"to'lov: "
        f"{_display_date(student['balance_date'])}"
        for student in students
    ]
    text = (
        f"📁 {group['name']}\n"
        f"🆔 Guruh ID: {group_id}\n\n"
        "👨‍🏫 Biriktirilgan o'qituvchilar:\n"
        f"{chr(10).join(teacher_lines) if teacher_lines else 'ℹ️ Hozircha biriktirilmagan'}\n\n"
        "👨‍🎓 O'quvchilar:"
    )
    if not students:
        text += "\n\nℹ️ Guruhda o'quvchi yo'q."
    else:
        text += "\n\n" + "\n\n".join(
            f"🔹 {line}" for line in student_lines
        )
    await _edit(query, text, _keyboard(rows))
    await query.answer()


@router.callback_query(F.data.startswith("db:student:"))
async def database_student(query: CallbackQuery, state: FSMContext) -> None:
    if not await _database_authorized(query, state):
        return
    _, _, center_text, group_text, student_text, page_text = query.data.split(":")
    center_id, group_id, student_id = (
        int(center_text), int(group_text), int(student_text)
    )
    page = max(0, int(page_text))
    student = await get_student(student_id)
    if not student or student["group_id"] != group_id:
        await query.answer("🔎 O'quvchi topilmadi.", show_alert=True)
        return
    group = await get_group(group_id)
    if not group or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    rows_data = await list_attendance_page(
        student_id, DATABASE_PAGE_SIZE + 1, page * DATABASE_PAGE_SIZE
    )
    has_next = len(rows_data) > DATABASE_PAGE_SIZE
    attendance = rows_data[:DATABASE_PAGE_SIZE]
    rows = []
    buttons = _page_buttons(
        f"db:student:{center_id}:{group_id}:{student_id}",
        page,
        has_next,
    )
    if buttons:
        rows.append(buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ O'quvchilar",
                callback_data=f"db:group:{center_id}:{group_id}:0",
            )
        ]
    )
    text = (
        f"👤 {student['full_name']}\n\n"
        f"🆔 O'quvchi ID: {student['id']}\n"
        f"👪 Ota-ona ID: {student['parent_telegram_id']}\n"
        f"🔗 Ota-ona username: "
        f"{'@' + student['parent_username'] if student['parent_username'] else 'kiritilmagan'}\n"
        f"💳 To'lov muddati: "
        f"{format_uzbek_date(date.fromisoformat(student['balance_date']))}\n"
        f"📁 Guruh: {group['name']}\n\n"
        "📅 Davomat tarixi:"
    )
    if attendance:
        text += "\n\n" + "\n\n".join(
            f"🔹 {_display_date(item['date'])} — "
            f"{'✅ Keldi' if item['status'] == 'Keldi' else '❌ Kelmadi'}"
            for item in attendance
        )
    else:
        text += "\n\nℹ️ Davomat yozuvlari yo'q."
    await _edit(query, text, _keyboard(rows))
    await query.answer()


@router.callback_query(F.data.startswith("sa:toggle:"))
async def toggle_center(query: CallbackQuery) -> None:
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    _, _, center_id, active = query.data.split(":")
    await set_center_active(int(center_id), bool(int(active)))
    await super_admin_panel(query)


@router.callback_query(F.data == "sa:new")
async def start_center_wizard(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(CenterWizard.name)
    if query.message:
        await query.message.answer("🏫 Yangi markaz nomini yuboring:")
    await query.answer()


@router.message(CenterWizard.name)
async def center_name(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Markaz nomi bo'sh bo'lmasligi kerak.\n\n✍️ Qayta yuboring:")
        return
    await state.update_data(name=name)
    await state.set_state(CenterWizard.admin_id)
    await message.answer(
        "👤 Markaz adminining Telegram ID sini yuboring.\n\n"
        "🔗 @username ixtiyoriy. Masalan: 123456789 @ali"
    )


@router.message(CenterWizard.admin_id)
async def center_admin_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    try:
        admin_id, admin_username = parse_telegram_identity(message.text or "")
    except ValueError as error:
        await message.answer(f"⚠️ {error}")
        return
    await state.update_data(admin_id=admin_id, admin_username=admin_username)
    await state.set_state(CenterWizard.admin_full_name)
    await message.answer("👤 Markaz adminining ism-familiyasini yuboring:")


@router.message(CenterWizard.admin_full_name)
async def center_admin_full_name(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id != SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Ruxsat yo'q.")
        return
    admin_full_name = (message.text or "").strip()
    if not admin_full_name:
        await message.answer("⚠️ Adminning ism-familiyasi bo'sh bo'lmasligi kerak.")
        return
    data = await state.get_data()
    admin_id = data["admin_id"]
    center_id = await create_center(
        data["name"], admin_id, admin_full_name, data["admin_username"]
    )
    await state.clear()
    await message.answer(
        f"✅ {format_center_name(data['name'])} yaratildi.\n\n"
        f"🆔 Markaz ID: {center_id}\n"
        f"👤 Admin tayinlandi: {admin_full_name}"
    )
    try:
        await message.bot.send_message(
            admin_id,
            f"🎉 Siz {format_center_name(data['name'])}ga admin etib tayinlandingiz.\n\n"
            "🚀 Boshlash uchun /start ni bosing.",
        )
    except TelegramAPIError as error:
        logger.warning("Could not notify new center admin %s: %s", admin_id, error)
        await message.answer(
            "⚠️ Markaz yaratildi, ammo admin botni hali ishga tushirmagani uchun "
            "xabar yuborilmadi."
        )


@router.callback_query(F.data.startswith("paysetup:"))
async def center_payment_setup_start(query: CallbackQuery, state: FSMContext) -> None:
    center_id = int(query.data.split(":")[1])
    center, role = await _authorized(query, center_id, admin_only=True)
    if not center or role != "admin":
        return
    await state.update_data(center_id=center_id)
    await state.set_state(CenterPaymentWizard.card_number)
    if query.message:
        await query.message.answer("💳 Markazning to'lov kartasi raqamini yuboring:")
    await query.answer()


@router.message(CenterPaymentWizard.card_number)
async def center_payment_card(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    card_number = (message.text or "").strip()
    if not card_number:
        await message.answer("⚠️ Karta raqami bo'sh bo'lmasligi kerak.\n\n💳 Qayta yuboring:")
        return
    await state.update_data(card_number=card_number)
    await state.set_state(CenterPaymentWizard.card_holder)
    await message.answer("👤 Karta egasining ism-familiyasini yuboring:")


@router.message(CenterPaymentWizard.card_holder)
async def center_payment_holder(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    card_holder = (message.text or "").strip()
    if not card_holder:
        await message.answer("⚠️ Karta egasining ismi bo'sh bo'lmasligi kerak.\n\n👤 Qayta yuboring:")
        return
    await state.update_data(card_holder=card_holder)
    await state.set_state(CenterPaymentWizard.admin_username)
    await message.answer(
        "🔗 Ota-onalarga ko'rsatiladigan admin @username ni yuboring.\n\n"
        "Masalan: @admin_username"
    )


@router.message(CenterPaymentWizard.admin_username)
async def center_payment_username(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    try:
        username = normalize_telegram_username(message.text or "")
    except ValueError as error:
        await message.answer(f"⚠️ {error}\n\n🔗 @username formatida yuboring.")
        return
    if not username:
        await message.answer("🔗 Adminning @username ini yuboring:")
        return
    await update_center_payment_details(
        data["center_id"], data["card_number"], data["card_holder"], username
    )
    await state.clear()
    await message.answer("✅ To'lov ma'lumotlari saqlandi.")


@router.callback_query(F.data.startswith("center:"))
async def open_center(query: CallbackQuery) -> None:
    center_id = int(query.data.split(":")[1])
    center, role = await _authorized(query, center_id)
    if center:
        await _render_center(
            query, center, role or "teacher", query.from_user.id
        )
        await query.answer()


@router.callback_query(F.data.startswith("group:"))
async def open_group(query: CallbackQuery) -> None:
    _, center_id, group_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id))
    role = await group_role(int(center_id), int(group_id), query.from_user.id)
    if center and role:
        await _render_group(query, int(center_id), int(group_id), role)
        await query.answer()
    elif center:
        await query.answer("⛔ Bu guruhga biriktirilmagansiz.", show_alert=True)


@router.callback_query(F.data.startswith("groupstudents:"))
async def open_group_students(query: CallbackQuery) -> None:
    _, center_text, group_text, page_text = query.data.split(":")
    center_id, group_id = int(center_text), int(group_text)
    center, _ = await _authorized(query, center_id)
    role = await group_role(center_id, group_id, query.from_user.id)
    group = await get_group(group_id)
    if not center:
        return
    if not group or group["center_id"] != center_id or not role:
        await query.answer("⛔ Bu guruhga kirish huquqingiz yo'q.", show_alert=True)
        return
    await _render_group_students(query, center_id, group_id, max(0, int(page_text)))
    await query.answer()


@router.callback_query(F.data.startswith("gadd:"))
async def add_group_start(query: CallbackQuery, state: FSMContext) -> None:
    center_id = int(query.data.split(":")[1])
    center, role = await _authorized(query, center_id, admin_only=True)
    if not center or role != "admin":
        return
    await state.update_data(center_id=center_id)
    await state.set_state(GroupWizard.name)
    if query.message:
        await query.message.answer("📁 Yangi guruh nomini yuboring:")
    await query.answer()


@router.message(GroupWizard.name)
async def add_group_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Guruh nomi bo'sh bo'lmasligi kerak.\n\n✍️ Qayta yuboring:")
        return
    try:
        await create_group(data["center_id"], name)
    except aiosqlite.IntegrityError:
        await message.answer("⚠️ Bu nomdagi guruh mavjud.\n\n✍️ Boshqa nom kiriting:")
        return
    await state.clear()
    await message.answer("✅ Guruh yaratildi.\n\n📂 Guruhlar ro'yxatiga qaytish uchun /start ni bosing.")


@router.callback_query(F.data.startswith("gnewteacher:"))
async def add_teacher_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_text, group_text = query.data.split(":")
    center_id, group_id = int(center_text), int(group_text)
    center, role = await _authorized(query, center_id, admin_only=True)
    if not center or role != "admin":
        return
    group = await get_group(group_id)
    if not group or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    await state.update_data(center_id=center_id, group_id=group_id)
    await state.set_state(TeacherWizard.full_name)
    if query.message:
        await query.message.answer(
            f"👨‍🏫 {group['name']} guruhiga qo'shiladigan "
            "o'qituvchining ism-familiyasini yuboring:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(
                    text="⬅️ Bekor qilish",
                    callback_data=f"gnewteachercancel:{center_id}:{group_id}",
                )]]
            ),
        )
    await query.answer()


@router.message(TeacherWizard.full_name)
async def add_teacher_name(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if command == "/cancel":
        await cancel_teacher_by_command(message, state)
        return
    data = await state.get_data()
    if not await _message_group_authorized(
        message, data["center_id"], data["group_id"], admin_only=True
    ):
        await state.clear()
        return
    full_name = text
    if not full_name:
        await message.answer("⚠️ O'qituvchining ism-familiyasi bo'sh bo'lmasligi kerak.")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(TeacherWizard.telegram_id)
    await message.answer(
        f"👤 O'qituvchi: {full_name}\n\n"
        "🆔 Telegram ID sini yuboring. @username ixtiyoriy.\n"
        "Masalan: 123456789 @ali",
        reply_markup=_keyboard(
            [[InlineKeyboardButton(
                text="⬅️ Bekor qilish",
                callback_data=(
                    f"gnewteachercancel:{data['center_id']}:{data['group_id']}"
                ),
            )]]
        ),
    )


@router.callback_query(F.data.startswith("gnewteachercancel:"))
async def cancel_add_teacher(query: CallbackQuery, state: FSMContext) -> None:
    _, center_text, group_text = query.data.split(":")
    center_id, group_id = int(center_text), int(group_text)
    center, role = await _authorized(query, center_id, admin_only=True)
    if not center or role != "admin":
        await state.clear()
        return
    group = await get_group(group_id)
    if not group or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    await state.clear()
    await _render_group(query, center_id, group_id, role)
    await query.answer("↩️ O'qituvchi qo'shish bekor qilindi.")


@router.message(TeacherWizard.telegram_id, Command("cancel"))
async def cancel_teacher_by_command(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    center_id = data.get("center_id")
    group_id = data.get("group_id")
    if center_id is None or group_id is None:
        await state.clear()
        await message.answer("↩️ O'qituvchi qo'shish bekor qilindi.")
        return
    if not await _message_group_authorized(
        message, center_id, group_id, admin_only=True
    ):
        await state.clear()
        return
    await state.clear()
    await message.answer(
        "↩️ O'qituvchi qo'shish bekor qilindi.",
        reply_markup=_keyboard(
            [[InlineKeyboardButton(
                text="⬅️ Guruhga qaytish",
                callback_data=f"group:{center_id}:{group_id}",
            )]]
        ),
    )


@router.message(TeacherWizard.telegram_id)
async def add_teacher_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_group_authorized(
        message, data["center_id"], data["group_id"], admin_only=True
    ):
        await state.clear()
        return
    try:
        teacher_id, teacher_username = parse_telegram_identity(message.text or "")
    except ValueError as error:
        await message.answer(f"⚠️ {error}")
        return
    await add_teacher(
        data["center_id"], teacher_id, data["full_name"], teacher_username
    )
    assigned = await set_group_teacher(
        data["center_id"], data["group_id"], teacher_id, True
    )
    if not assigned:
        await state.clear()
        await message.answer("⚠️ O'qituvchini ushbu guruhga biriktirib bo'lmadi.")
        return
    group = await get_group(data["group_id"])
    await state.clear()
    await message.answer(
        f"✅ {data['full_name']} {group['name']} guruhiga qo'shildi.",
        reply_markup=_keyboard(
            [[InlineKeyboardButton(
                text="⬅️ Guruhga qaytish",
                callback_data=f"group:{data['center_id']}:{data['group_id']}",
            )]]
        ),
    )
    try:
        await message.bot.send_message(
            teacher_id,
            f"🎉 Siz {group['name']} guruhiga o'qituvchi etib biriktirildingiz.\n\n"
            "🚀 Boshlash uchun /start ni bosing.",
        )
    except TelegramAPIError as error:
        logger.warning("Could not notify new teacher %s: %s", teacher_id, error)
        await message.answer(
            "⚠️ O'qituvchi qo'shildi, ammo bot hali ishga tushirilmagani uchun "
            "unga xabar yuborilmadi."
        )


async def _render_group_staff_menu(
    query: CallbackQuery, center_id: int, group_id: int
) -> None:
    group = await get_group(group_id)
    if not group or group["center_id"] != center_id:
        return
    teachers = await list_center_teachers(center_id)
    assigned = await group_teacher_ids(group_id)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if teacher['telegram_id'] in assigned else '⬜'} "
                f"{teacher['full_name'] or 'Ism kiritilmagan'} — ID {teacher['telegram_id']}",
                callback_data=(
                    f"gstafftoggle:{center_id}:{group_id}:{teacher['telegram_id']}:"
                    f"{0 if teacher['telegram_id'] in assigned else 1}"
                ),
            )
        ]
        for teacher in teachers
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ Guruhga qaytish", callback_data=f"group:{center_id}:{group_id}")]
    )
    text = (
        f"👥 {group['name']}\n\n"
        "☑️ Guruhga biriktiriladigan o'qituvchilarni tanlang:"
    )
    if not teachers:
        text = "ℹ️ Markazda hozircha o'qituvchi yo'q.\n\n➕ Avval o'qituvchi qo'shing."
    await _edit(query, text, _keyboard(rows))


@router.callback_query(F.data.startswith("gstaff:"))
async def group_staff_menu(query: CallbackQuery) -> None:
    _, center_id_text, group_id_text = query.data.split(":")
    center_id, group_id = int(center_id_text), int(group_id_text)
    center, role = await _authorized(query, center_id, admin_only=True)
    group = await get_group(group_id)
    if not center or role != "admin":
        return
    if not group or group["center_id"] != center_id:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        return
    await _render_group_staff_menu(query, center_id, group_id)
    await query.answer()


@router.callback_query(F.data.startswith("gstafftoggle:"))
async def group_staff_toggle(query: CallbackQuery) -> None:
    _, center_text, group_text, teacher_text, assigned_text = query.data.split(":")
    center_id, group_id = int(center_text), int(group_text)
    teacher_id, assigned = int(teacher_text), bool(int(assigned_text))
    center, role = await _authorized(query, center_id, admin_only=True)
    if not center or role != "admin":
        return
    if not await set_group_teacher(center_id, group_id, teacher_id, assigned):
        await query.answer("⛔ O'qituvchi yoki guruh ushbu markazga tegishli emas.", show_alert=True)
        return
    await _render_group_staff_menu(query, center_id, group_id)
    await query.answer("✅ Guruh o'qituvchilari yangilandi.")


@router.callback_query(F.data.startswith("sadd:"))
async def add_student_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_id, group_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id))
    group = await get_group(int(group_id))
    role = await group_role(int(center_id), int(group_id), query.from_user.id)
    if not center or not group or group["center_id"] != int(center_id) or not role:
        if center and group:
            await query.answer("⛔ Bu guruhga biriktirilmagansiz.", show_alert=True)
        return
    await state.update_data(group_id=int(group_id), center_id=int(center_id))
    await state.set_state(StudentWizard.full_name)
    if query.message:
        await query.message.answer("👨‍🎓 O'quvchining ism-familiyasini yuboring:")
    await query.answer()


@router.message(StudentWizard.full_name)
async def student_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_group_authorized(
        message, data["center_id"], data["group_id"]
    ):
        await state.clear()
        return
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("⚠️ O'quvchi ism-familiyasi bo'sh bo'lmasligi kerak.\n\n✍️ Qayta yuboring:")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(StudentWizard.parent_id)
    await message.answer(
        "👪 Ota-onaning Telegram ID sini yuboring.\n\n"
        "🔗 @username ixtiyoriy. Masalan: 123456789 @ali\n"
        "ℹ️ Botdan xabar olish uchun Telegram ID shart."
    )


@router.message(StudentWizard.parent_id)
async def student_parent(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_group_authorized(
        message, data["center_id"], data["group_id"]
    ):
        await state.clear()
        return
    try:
        parent_id, parent_username = parse_telegram_identity(message.text or "")
    except ValueError as error:
        await message.answer(f"⚠️ {error}")
        return
    await state.update_data(parent_id=parent_id, parent_username=parent_username)
    await state.set_state(StudentWizard.balance_date)
    await message.answer(
        "💳 To'lov muddatini kiriting.\n\n"
        "📅 Sana formati: YYYY-MM-DD\n"
        "Masalan: 2026-11-30"
    )


@router.message(StudentWizard.balance_date)
async def student_due_date(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_group_authorized(
        message, data["center_id"], data["group_id"]
    ):
        await state.clear()
        return
    try:
        due_date = _parse_date((message.text or "").strip())
    except ValueError:
        await message.answer(
            "⚠️ Sana formati noto'g'ri.\n\n"
            "📅 YYYY-MM-DD formatida qayta yuboring.\n"
            "Masalan: 2026-11-30"
        )
        return
    try:
        await create_student(
            data["group_id"],
            data["full_name"],
            data["parent_id"],
            due_date.isoformat(),
            data["parent_username"],
        )
    except aiosqlite.IntegrityError:
        await message.answer("⚠️ Bu o'quvchi allaqachon mavjud.\n\n✍️ Boshqa ism-familiya yuboring:")
        await state.set_state(StudentWizard.full_name)
        return
    await state.clear()
    await message.answer("✅ O'quvchi qo'shildi.\n\n📂 Guruhga qaytish uchun /start ni bosing.")


@router.callback_query(F.data.startswith("student:"))
async def open_student(query: CallbackQuery) -> None:
    _, center_id, student_id = query.data.split(":")
    center, role = await _authorized(query, int(center_id))
    group_access = await _student_access_role(
        int(student_id), int(center_id), query.from_user.id
    )
    if center and group_access:
        await _render_student(query, int(center_id), int(student_id), role or "teacher")
        await query.answer()
    elif center:
        await query.answer("⛔ Bu o'quvchi guruhiga ruxsatingiz yo'q.", show_alert=True)


@router.callback_query(F.data.startswith("editname:"))
async def edit_name_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_id, student_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id), admin_only=True)
    if not center:
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    await state.update_data(center_id=int(center_id), student_id=int(student_id))
    await state.set_state(StudentWizard.edit_name)
    if query.message:
        await query.message.answer("✏️ O'quvchining yangi ism-familiyasini yuboring:")
    await query.answer()


@router.message(StudentWizard.edit_name)
async def edit_name_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("⚠️ O'quvchi ism-familiyasi bo'sh bo'lmasligi kerak.\n\n✍️ Qayta yuboring:")
        return
    try:
        await update_student(data["student_id"], "full_name", full_name)
    except aiosqlite.IntegrityError:
        await message.answer("⚠️ Shu ism va ota-ona ID bilan o'quvchi mavjud.\n\n✍️ Boshqa ism kiriting:")
        return
    await state.clear()
    await message.answer("✅ O'quvchi ism-familiyasi yangilandi.")


@router.callback_query(F.data.startswith("editparent:"))
async def edit_parent_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_id, student_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id), admin_only=True)
    if not center:
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    await state.update_data(center_id=int(center_id), student_id=int(student_id))
    await state.set_state(StudentWizard.edit_parent)
    if query.message:
        await query.message.answer(
            "👪 Ota-onaning yangi Telegram ID sini yuboring.\n\n"
            "🔗 @username ixtiyoriy. Masalan: 123456789 @ali"
        )
    await query.answer()


@router.message(StudentWizard.edit_parent)
async def edit_parent_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    try:
        parent_id, parent_username = parse_telegram_identity(message.text or "")
    except ValueError as error:
        await message.answer(f"⚠️ {error}")
        return
    await update_student(data["student_id"], "parent_telegram_id", parent_id)
    await update_student(data["student_id"], "parent_username", parent_username)
    await state.clear()
    await message.answer("✅ Ota-ona ma'lumotlari yangilandi.")


@router.callback_query(F.data.startswith("extend:"))
async def extend_payment_date(query: CallbackQuery) -> None:
    _, center_id, student_id = query.data.split(":")
    center, role = await _authorized(query, int(center_id), admin_only=True)
    if not center or role != "admin":
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    student = await get_student(int(student_id))
    if not student:
        await query.answer("🔎 O'quvchi topilmadi.", show_alert=True)
        return
    new_date = _next_month_same_day(date.fromisoformat(student["balance_date"]))
    await _confirm_payment_date(query, center, student, new_date.isoformat())


@router.callback_query(F.data.startswith("editdate:"))
async def edit_date_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_id, student_id = query.data.split(":")
    center, role = await _authorized(query, int(center_id), admin_only=True)
    if not center or role != "admin":
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    await state.update_data(center_id=int(center_id), student_id=int(student_id))
    await state.set_state(StudentWizard.edit_date)
    if query.message:
        await query.message.answer(
            "🗓 Yangi to'lov sanasini yuboring.\n\n"
            "📅 Sana formati: YYYY-MM-DD\n"
            "Masalan: 2026-11-30"
        )
    await query.answer()


@router.message(StudentWizard.edit_date)
async def edit_date_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _message_authorized(message, data["center_id"], admin_only=True):
        await state.clear()
        return
    try:
        new_date = _parse_date((message.text or "").strip())
    except ValueError:
        await message.answer(
            "⚠️ Sana formati noto'g'ri.\n\n"
            "📅 YYYY-MM-DD formatida qayta yuboring.\n"
            "Masalan: 2026-11-30"
        )
        return
    student = await get_student(data["student_id"])
    center = await get_center(data["center_id"])
    if not student or not center:
        await state.clear()
        await message.answer("🔎 O'quvchi yoki markaz topilmadi.")
        return
    await update_student(student["id"], "balance_date", new_date.isoformat())
    await state.clear()
    await _notify_payment_confirmation(message.bot, center, student, new_date.isoformat(), message)


async def _confirm_payment_date(query: CallbackQuery, center: dict, student: dict, due_date: str) -> None:
    await update_student(student["id"], "balance_date", due_date)
    await query.answer("✅ To'lov sanasi yangilandi.")
    await _render_student(query, center["id"], student["id"], "admin")
    await _notify_payment_confirmation(query.bot, center, student, due_date, query.message)


def _payment_confirmation_text(
    center_name: str,
    group_name: str,
    student_name: str,
    due_date: str,
    confirmed_date: date,
) -> str:
    return (
        f"🏫 {format_center_name(center_name)}\n"
        f"👥 Guruh: {group_name}\n\n"
        f"👨‍👩‍👧‍👦 Hurmatli ota-ona, {student_name} uchun to'lov:\n\n"
        f"✅ Tasdiqlangan sana: {format_uzbek_date(confirmed_date)}\n"
        f"📅 Keyingi to'lov muddati: "
        f"{format_uzbek_date(date.fromisoformat(due_date))}\n\n"
        "🤝 Kelgusida ham to'lovni o'z vaqtida amalga oshirishingizni so'raymiz."
    )


def _attendance_notification_text(
    center_name: str,
    group_name: str,
    student_name: str,
    status: str,
    attendance_date: date,
) -> str:
    date_text = format_uzbek_date(attendance_date)
    if status == "Kelmadi":
        return (
            f"🏫 {format_center_name(center_name)}\n"
            f"👥 Guruh: {group_name}\n\n"
            f"👨‍👩‍👧‍👦 Hurmatli ota-ona!\n"
            f"❌ Farzandingiz {student_name} {date_text} kuni darsga kelmadi.\n\n"
            "📞 Sababini tushuntirish uchun o'quv markazi bilan bog'laning."
        )
    return (
        f"🏫 {format_center_name(center_name)}\n"
        f"👥 Guruh: {group_name}\n\n"
        f"👨‍👩‍👧‍👦 Hurmatli ota-ona!\n"
        f"✅ Farzandingiz {student_name} {date_text} kuni darsga keldi."
    )


async def _notify_payment_confirmation(
    bot: Bot, center: dict, student: dict, due_date: str, response: Message | None
) -> None:
    group = await get_group(student["group_id"])
    if not group:
        logger.error(
            "Cannot send payment confirmation: group %s for student %s was not found",
            student["group_id"],
            student["id"],
        )
        if response:
            await response.answer("⚠️ Sana yangilandi, ammo guruh topilmadi.")
        return
    text = _payment_confirmation_text(
        center["name"], group["name"], student["full_name"], due_date, _today()
    )
    try:
        await bot.send_message(student["parent_telegram_id"], text)
    except TelegramAPIError as error:
        logger.warning("Payment confirmation failed for student %s: %s", student["id"], error)
        if response:
            await response.answer(
                "⚠️ Sana yangilandi, ammo ota-onaga tasdiq xabari yuborilmadi."
            )
        return
    if response:
        await response.answer("✅ To'lov sanasi yangilandi.\n\n📨 Ota-onaga tasdiq yuborildi.")


@router.callback_query(F.data.startswith("delask:"))
async def ask_delete_student(query: CallbackQuery) -> None:
    _, center_id, student_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id), admin_only=True)
    if not center:
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(text="🗑 Ha, o'chirish", callback_data=f"delete:{center_id}:{student_id}"),
            InlineKeyboardButton(text="↩️ Bekor qilish", callback_data=f"student:{center_id}:{student_id}"),
        ]
    ]
    await _edit(
        query,
        "🗑 O'quvchini va uning davomat yozuvlarini o'chirasizmi?\n\n"
        "⚠️ Bu amalni qaytarib bo'lmaydi.",
        _keyboard(rows),
    )
    await query.answer()


@router.callback_query(F.data.startswith("delete:"))
async def delete_student_handler(query: CallbackQuery) -> None:
    _, center_id, student_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id), admin_only=True)
    if not center:
        return
    if not await _student_belongs_to_center(int(student_id), int(center_id)):
        await query.answer("⛔ O'quvchi bu markazga tegishli emas.", show_alert=True)
        return
    student = await get_student(int(student_id))
    if student:
        group_id = student["group_id"]
        await delete_student(int(student_id))
        await query.answer("✅ O'quvchi o'chirildi.")
        await _render_group(query, int(center_id), group_id, "admin")


@router.callback_query(F.data.startswith("att:"))
async def attendance_start(query: CallbackQuery, state: FSMContext) -> None:
    _, center_id, group_id = query.data.split(":")
    center, _ = await _authorized(query, int(center_id))
    group = await get_group(int(group_id))
    role = await group_role(int(center_id), int(group_id), query.from_user.id)
    if not center or not group or group["center_id"] != int(center_id) or not role:
        if center and group:
            await query.answer("⛔ Bu guruhga biriktirilmagansiz.", show_alert=True)
        return
    students = await list_students(int(group_id))
    if not students:
        await query.answer("ℹ️ Guruhda o'quvchilar yo'q.", show_alert=True)
        return
    statuses = {str(student["id"]): "Keldi" for student in students}
    await state.update_data(center_id=int(center_id), group_id=int(group_id), statuses=statuses)
    await _render_attendance(query, students, statuses)
    await query.answer()


async def _render_attendance(
    query: CallbackQuery, students: list, statuses: dict[str, str]
) -> None:
    rows = []
    for student in students:
        status = statuses[str(student["id"])]
        label = "✅ Keldi" if status == "Keldi" else "❌ Kelmadi"
        rows.append(
            [InlineKeyboardButton(
                text=f"{label} — {student['full_name']}",
                callback_data=f"attoggle:{student['id']}",
            )]
        )
    rows.append([InlineKeyboardButton(text="💾 Davomatni saqlash", callback_data="atsave")])
    await _edit(
        query,
        f"📝 DAVOMAT — {_today().isoformat()}\n\n"
        "👇 Har bir o'quvchining holatini belgilang.\n"
        "✅ Tugmani bosing, holat almashadi.\n\n"
        "💾 Tayyor bo'lgach, davomatni saqlang.",
        _keyboard(rows),
    )


@router.callback_query(F.data.startswith("attoggle:"))
async def attendance_toggle(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    statuses = data.get("statuses")
    student_id = query.data.split(":")[1]
    if not statuses or student_id not in statuses:
        await query.answer("⚠️ Davomat sessiyasi tugagan. Qaytadan oching.", show_alert=True)
        return
    center_id, group_id = data["center_id"], data["group_id"]
    center, _ = await _authorized(query, center_id)
    role = await group_role(center_id, group_id, query.from_user.id)
    if not center:
        await state.clear()
        return
    if not role:
        await query.answer("⛔ Bu guruhga kirish huquqingiz yo'q.", show_alert=True)
        await state.clear()
        return
    statuses[student_id] = "Kelmadi" if statuses[student_id] == "Keldi" else "Keldi"
    await state.update_data(statuses=statuses)
    students = await list_students(data["group_id"])
    await _render_attendance(query, students, statuses)
    await query.answer()


@router.callback_query(F.data == "atsave")
async def attendance_save(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("statuses"):
        await query.answer("⚠️ Davomat sessiyasi topilmadi. Qaytadan oching.", show_alert=True)
        return
    center, _ = await _authorized(query, data["center_id"])
    role = await group_role(
        data["center_id"], data["group_id"], query.from_user.id
    )
    if not center:
        await state.clear()
        return
    if not role:
        await query.answer("⛔ Bu guruhga kirish huquqingiz yo'q.", show_alert=True)
        await state.clear()
        return
    statuses = {int(student_id): value for student_id, value in data["statuses"].items()}
    attendance_date = _today().isoformat()
    await save_attendance(attendance_date, statuses)
    group = await get_group(data["group_id"])
    if not group:
        await query.answer("🔎 Guruh topilmadi.", show_alert=True)
        await state.clear()
        return
    students = await list_students(data["group_id"])
    failed = []
    absent_names = []
    present_names = []
    for student in students:
        status = statuses.get(student["id"], "Keldi")
        text = _attendance_notification_text(
            center["name"], group["name"], student["full_name"], status, _today()
        )
        try:
            await query.bot.send_message(student["parent_telegram_id"], text)
            if status == "Kelmadi":
                absent_names.append(student["full_name"])
            else:
                present_names.append(student["full_name"])
        except TelegramAPIError as error:
            logger.warning("Attendance notification failed for student %s: %s", student["id"], error)
            failed.append(student["full_name"])
    await state.clear()
    summary = (
        f"✅ Davomat saqlandi\n\n"
        f"🟢 Keldi: {', '.join(present_names) if present_names else 'yo‘q'}\n\n"
        f"🔴 Kelmadi: {', '.join(absent_names) if absent_names else 'yo‘q'}"
    )
    if failed:
        summary += f"\n\n⚠️ Xabar yuborilmadi:\n{', '.join(failed)}"
    await _edit(query, summary, _keyboard(
        [[InlineKeyboardButton(text="⬅️ Guruhga qaytish", callback_data=f"group:{data['center_id']}:{data['group_id']}")]]
    ))
    await query.answer()
