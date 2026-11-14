from __future__ import annotations

import asyncio
import logging
import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import TIMEZONE
from database import (
    mark_payment_reminders_run,
    payment_reminders_run_date,
    students_for_reminders,
)
from date_utils import format_center_name, format_uzbek_date

logger = logging.getLogger(__name__)
_daily_reminders_lock = asyncio.Lock()


def payment_reminder_text(
    student: dict, days_left: int, today: date | None = None
) -> str | None:
    center = html.escape(format_center_name(student["center_name"]), quote=False)
    group_name = html.escape(student["group_name"], quote=False)
    student_name = html.escape(student["full_name"], quote=False)
    admin_username = (
        student.get("admin_username") or student.get("stored_admin_username") or ""
    ).removeprefix("@")
    admin_url = (
        f"https://t.me/{admin_username}"
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", admin_username)
        else f"tg://user?id={student['admin_telegram']}"
    )
    if admin_username and not admin_url.startswith("https://"):
        logger.warning("Invalid admin username configured for center %s", student["center_id"])
    payment_details = (
        "💳 TO'LOV MA'LUMOTLARI\n\n"
        f"💳 Karta raqami: "
        f"{html.escape(student['card_number'] or 'kiritilmagan', quote=False)}\n"
        f"👤 Karta egasi: "
        f"{html.escape(student['card_holder'] or 'kiritilmagan', quote=False)}\n"
        f'📞 Admin: <a href="{html.escape(admin_url, quote=True)}">'
        "profiliga yozish</a>"
    )
    header = (
        f"🏫 {center}\n"
        f"👥 Guruh: {group_name}\n\n"
        f"👨‍👩‍👧‍👦 Hurmatli ota-ona!\n"
        f"👨‍🎓 Farzandingiz: <b>{student_name}</b>"
    )
    due_date = format_uzbek_date(date.fromisoformat(student["balance_date"]))
    today_text = format_uzbek_date(today or date.today())
    if days_left == 5:
        notice = (
            "🔔 Oylik to'lov muddatiga 5 kun qoldi."
        )
    elif 2 <= days_left <= 4:
        notice = f"🔔 To'lov muddatiga {days_left} kun qoldi."
    elif days_left == 1:
        notice = "⚠️ Ertaga to'lov qilishning oxirgi kuni."
    elif days_left == 0:
        notice = "🚨 Bugun o'qish uchun to'lov qilishning oxirgi kuni."
    elif days_left < 0:
        notice = (
            f"❌ To'lov muddati {abs(days_left)} kun oldin o'tgan. "
            "Iltimos, to'lovni amalga oshiring."
        )
    elif days_left != 5:
        return None
    return (
        f"{header}\n\n"
        f"{notice}\n"
        f"📅 To'lov muddati: {due_date}\n"
        f"📆 Bugungi sana: {today_text}\n\n"
        f"{payment_details}"
    )


async def send_daily_payment_reminders(bot: Bot) -> None:
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    grouped: dict[int, dict] = {}
    for row in await students_for_reminders():
        student = dict(row)
        report = grouped.setdefault(
            student["center_id"],
            {
                "admin_id": student["admin_telegram"],
                "groups": {},
            }
        )
        try:
            due_date = date.fromisoformat(student["balance_date"])
        except ValueError:
            logger.exception(
                "Invalid payment date for student %s: %s",
                student["id"],
                student["balance_date"],
            )
            group_report = report["groups"].setdefault(
                student["group_name"], {"sent": [], "failed": []}
            )
            group_report["failed"].append(
                f"⚠️ {student['full_name']}: bazadagi to'lov sanasi noto'g'ri."
            )
            continue
        days_left = (due_date - today).days
        message = payment_reminder_text(student, days_left, today)
        if message is None:
            continue
        group_report = report["groups"].setdefault(
            student["group_name"], {"sent": [], "failed": []}
        )

        try:
            await bot.send_message(
                student["parent_telegram_id"], message, parse_mode="HTML"
            )
            group_report["sent"].append(student["full_name"])
        except TelegramAPIError as error:
            logger.warning(
                "Payment reminder failed for student %s: %s", student["id"], error
            )
            group_report["failed"].append(
                f"{student['full_name']}: {str(error)[:250]}"
            )

    for report in grouped.values():
        lines = ["📊 Bugungi to'lov eslatmalari hisoboti"]
        for group_name, entries in report["groups"].items():
            lines.extend(["", f"👥 Guruh: {group_name}", ""])
            lines.extend(
                f"✅ {name} — ota-onaga eslatma yuborildi"
                for name in entries["sent"]
            )
            if entries["sent"] and entries["failed"]:
                lines.append("")
            lines.extend(
                f"⚠️ {failure} — xabar yuborilmadi"
                for failure in entries["failed"]
            )
        if len(lines) == 1:
            lines.extend(["", "ℹ️ Bugun eslatma yuborilishi kerak bo'lgan o'quvchi yo'q."])
        chunks: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 3900:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            await bot.send_message(report["admin_id"], chunk)


async def run_daily_payment_reminders_if_due(
    bot: Bot, now: datetime | None = None
) -> bool:
    current_time = now or datetime.now(ZoneInfo(TIMEZONE))
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ZoneInfo(TIMEZONE))
    current_time = current_time.astimezone(ZoneInfo(TIMEZONE))
    if (current_time.hour, current_time.minute) < (9, 0):
        return False

    run_date = current_time.date().isoformat()
    async with _daily_reminders_lock:
        if await payment_reminders_run_date() == run_date:
            return False
        await send_daily_payment_reminders(bot)
        await mark_payment_reminders_run(run_date)
    return True


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_daily_payment_reminders_if_due,
        CronTrigger(hour=9, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="daily_payment_reminders",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=24 * 60 * 60,
    )
    return scheduler
