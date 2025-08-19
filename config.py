import os
from dotenv import load_dotenv

load_dotenv()

# Bot configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Server configuration
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

DAILY_REMIND_TIME = os.getenv("DAILY_REMIND")
DAILY_DEADLINE_TIME = os.getenv("DAILY_DEADLINE")
print(DAILY_DEADLINE_TIME, DAILY_REMIND_TIME)
# Database configuration
DATABASE_FILE = os.getenv("DATABASE_FILE", "messages.db")
EXPORT_DIR = os.getenv("EXPORT_DIR", "exports")
DB_ENCRYPTION_KEY = os.getenv('DB_ENCRYPTION_KEY')
DB_ENCRYPTION_PASSWORD = os.getenv('DB_ENCRYPTION_PASSWORD')
DB_SALT = os.getenv('DB_SALT')
# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")
TIMEZONE = os.getenv("TIMEZONE")