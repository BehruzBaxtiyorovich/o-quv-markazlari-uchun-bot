import re
from datetime import date


UZBEK_MONTHS = (
    "yanvar",
    "fevral",
    "mart",
    "aprel",
    "may",
    "iyun",
    "iyul",
    "avgust",
    "sentabr",
    "oktabr",
    "noyabr",
    "dekabr",
)


def format_uzbek_date(value: date) -> str:
    return f"{value.year}-yil {value.day}-{UZBEK_MONTHS[value.month - 1]}"


def format_center_name(value: str) -> str:
    name = value.strip()
    if name.casefold().endswith("o'quv markazi"):
        return name
    return f"{name} o'quv markazi"


def normalize_telegram_username(value: str) -> str:
    username = value.strip().removeprefix("@")
    if not username:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValueError("Telegram username noto'g'ri.")
    return username


def parse_telegram_identity(value: str) -> tuple[int, str]:
    parts = value.strip().split()
    if not parts:
        raise ValueError("Telegram ID raqamini kiriting.")
    telegram_id: int | None = None
    username = ""
    for part in parts:
        if part.startswith("@"):
            if username:
                raise ValueError("Faqat bitta @username kiriting.")
            username = normalize_telegram_username(part)
            if not username:
                raise ValueError("Telegram username bo'sh bo'lmasligi kerak.")
        elif part.isdecimal() and telegram_id is None:
            telegram_id = int(part)
        else:
            raise ValueError("ID va ixtiyoriy @username kiriting, masalan: 123456789 @ali.")
    if not telegram_id or telegram_id <= 0:
        raise ValueError(
            "Xususiy foydalanuvchiga xabar yuborish uchun Telegram ID shart. "
            "ID bilan birga @username ham kiritishingiz mumkin."
        )
    return telegram_id, username
