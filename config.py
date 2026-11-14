import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
DATABASE_ADMIN_PASSWORD = os.getenv("DATABASE_ADMIN_PASSWORD", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")
