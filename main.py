import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, SUPER_ADMIN_ID
from database import init_db
from handlers import router
from scheduler import create_scheduler, run_daily_payment_reminders_if_due


async def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN muhit o'zgaruvchisini sozlang.")
    if SUPER_ADMIN_ID <= 0:
        raise RuntimeError("SUPER_ADMIN_ID muhit o'zgaruvchisini musbat Telegram ID bilan sozlang.")

    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    scheduler = create_scheduler(bot)
    scheduler.start()
    try:
        await run_daily_payment_reminders_if_due(bot)
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
