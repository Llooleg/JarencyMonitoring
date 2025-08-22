import os
from dotenv import load_dotenv
from oauth2client.client import GOOGLE_APPLICATION_CREDENTIALS

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


DEVELOPER_SHEETS = {
    "root": {
        "sheet_id": "yoursheetid",
        "worksheet_name": "root: учет времени",  # Name of the tab
        "date_column": 4,    # Column A
        "hours_column": 7,   # Column B
        "description_column": 6  # Column C
    }, }

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
TIMESHEET_CHECK_TIME = os.getenv("TIMESHEET_CHECK_TIME")
TIMESHEET_REMINDER_ENABLED=True
TIMESHEET_CHECK_DAYS_BACK=5
MIN_HOURS_THRESHOLD=0.5
USER_MORNING_DIGEST = {
    "root": {
        "time": "15:07",
        "timezone": "Europe/Moscow",
        "chat_id": -4967360927,
        "enabled": True
    }}

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
N_CHANGED_FILES =os.getenv("N_CHANGED_FILES")
