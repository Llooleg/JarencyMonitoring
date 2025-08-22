from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import sqlite3
import json
import os
import time
import asyncio
from datetime import datetime, timedelta
from colorama import Fore, Style, init
from collections import Counter
import aiofiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from config import HOST, PORT, DATABASE_FILE, TIMEZONE, DB_ENCRYPTION_KEY, DB_ENCRYPTION_PASSWORD, DB_SALT, DAILY_REMIND_TIME, DAILY_DEADLINE_TIME, N_CHANGED_FILES, GITLAB_TOKEN, GITLAB_URL
from encryption import DatabaseEncryption
from pytz import timezone
import logging
from fastapi import Request
import gspread
import gspread
from datetime import datetime, timedelta
import re
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from config import (
    GOOGLE_CREDENTIALS_FILE, TIMESHEET_CHECK_TIME, DEVELOPER_SHEETS,
    TIMESHEET_REMINDER_ENABLED, TIMESHEET_CHECK_DAYS_BACK, MIN_HOURS_THRESHOLD
)
import aiohttp
from pytz import timezone as pytz_timezone


logger = logging.getLogger(__name__)

app = FastAPI(title="Telegram Bot Server", version="1.0.0")

# --- Simple stats container ---
class ServerStats:
    def __init__(self):
        self.messages_processed = 0
        self.responses_generated = 0
        self.errors = 0
        self.start_time = datetime.now()
        self.last_activity = datetime.now()


stats = ServerStats()

class MessageData(BaseModel):
    message_id: int
    timestamp: str
    chat: Dict
    user: Dict
    content: str
    message_type: str


# --- Database manager (unchanged) ---
class DatabaseManager:

    def __init__(self):
        self.encryption = None

        if DB_ENCRYPTION_KEY:
            print(f"{Fore.GREEN}🔐 Using direct encryption key")
            self.encryption = DatabaseEncryption(direct_key=DB_ENCRYPTION_KEY)
        elif DB_ENCRYPTION_PASSWORD:
            print(f"{Fore.GREEN}🔐 Using password-derived encryption")
            self.encryption = DatabaseEncryption(password=DB_ENCRYPTION_PASSWORD)
        else:
            print(f"{Fore.YELLOW}⚠️ No encryption credentials found - running without encryption")

        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # GitLab
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gitlab_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dev TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
        ''')

        # отчёты
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dev TEXT NOT NULL,
                date DATE NOT NULL,
                submitted BOOLEAN DEFAULT 0,
                message_id INTEGER
            )
        ''')

        #  Loom
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS loom_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dev TEXT NOT NULL,
                mr_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                url TEXT
            )
        ''')


        cursor.execute('''CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    chat_id INTEGER,
    chat_title TEXT,
    user_id INTEGER,
    username TEXT,
    first_name TEXT,
    content TEXT,
    message_type TEXT)
    ''')
        # server.py → DatabaseManager.init_database
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gitlab_username TEXT UNIQUE NOT NULL,
                telegram_id INTEGER UNIQUE NOT NULL
            )
        ''')

        def add_user_mapping(self, gitlab_username: str, telegram_id: int):
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO user_mapping (gitlab_username, telegram_id)
                VALUES (?, ?)
            """, (gitlab_username, telegram_id))
            conn.commit()
            conn.close()

        def get_telegram_id(self, gitlab_username: str) -> Optional[int]:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id FROM user_mapping WHERE gitlab_username = ?", (gitlab_username,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

        def get_gitlab_username(self, telegram_id: int) -> Optional[str]:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT gitlab_username FROM user_mapping WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

        def list_user_mappings(self) -> list[dict]:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT gitlab_username, telegram_id FROM user_mapping")
            rows = cursor.fetchall()
            conn.close()
            return [{"gitlab_username": r[0], "telegram_id": r[1]} for r in rows]

        # Индексы для ускорения запросов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_gitlab_dev_ts ON gitlab_events(dev, ts)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_dev_date ON daily_reports(dev, date)')

        self.apply_migrations(cursor)

        conn.commit()
        conn.close()

    def add_user_mapping(self, gitlab_username: str, telegram_id: int):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO user_mapping (gitlab_username, telegram_id)
            VALUES (?, ?)
        """, (gitlab_username, telegram_id))
        conn.commit()
        conn.close()

    def get_telegram_id(self, gitlab_username: str) -> Optional[int]:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM user_mapping WHERE gitlab_username = ?", (gitlab_username,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def get_gitlab_username(self, telegram_id: int) -> Optional[str]:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT gitlab_username FROM user_mapping WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def list_user_mappings(self) -> list[dict]:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT gitlab_username, telegram_id FROM user_mapping")
        rows = cursor.fetchall()
        conn.close()
        return [{"gitlab_username": r[0], "telegram_id": r[1]} for r in rows]

    def apply_migrations(self, cursor):
        """Enhanced migration method"""
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='migrations'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE migrations (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

        # Check if content column exists in daily_reports
        cursor.execute("PRAGMA table_info(daily_reports)")
        columns = [column[1] for column in cursor.fetchall()]

        if 'content' not in columns:
            cursor.execute('ALTER TABLE daily_reports ADD COLUMN content TEXT')
            cursor.execute("""
                INSERT INTO migrations (name) VALUES ('add_content_to_daily_reports')
            """)
            print(f"{Fore.GREEN}✅ Added content column to daily_reports")

        print(f"{Fore.GREEN}✅ Database migrations applied")


    def add_gitlab_event(self, dev: str, event_type: str, payload: dict):
        event_type = payload.get('object_kind', 'unknown')
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO gitlab_events (dev, ts, type, payload_json) VALUES (?, ?, ?, ?)",
            (dev, datetime.utcnow().isoformat(), event_type, json.dumps(payload)))
        conn.commit()
        conn.close()
    def get_gitlab_events(self, dev: str, period_hours: int = 24) -> list:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        since = (datetime.utcnow() - timedelta(hours=period_hours)).isoformat()

        cursor.execute(
            "SELECT * FROM gitlab_events WHERE dev = ? AND ts >= ?",
            (dev, since))
        events = [dict(zip(['id', 'dev', 'ts', 'type', 'payload'], row)) for row in cursor.fetchall()]
        conn.close()
        return events

    def save_daily_report(self, dev: str, date: str, content: str, message_id: int = None):
        """Save or update a daily report"""
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO daily_reports 
            (dev, date, submitted, message_id, content) 
            VALUES (?, ?, 1, ?, ?)
        """, (dev, date, message_id, content))

        conn.commit()
        conn.close()

    def get_daily_report(self, dev: str, date: str) -> dict:
        """Get daily report for a user on a specific date"""
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM daily_reports 
            WHERE dev = ? AND date = ?
        """, (dev, date))

        result = cursor.fetchone()
        conn.close()

        if result:
            return dict(zip(['id', 'dev', 'date', 'submitted', 'message_id', 'content'], result))
        return None

    def save_message(self, message: MessageData):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO messages (
                message_id, timestamp, chat_id, chat_title, user_id, username, first_name, content, message_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.message_id,
            message.timestamp,
            message.chat.get("id"),
            message.chat.get("title"),
            message.user.get("id"),
            message.user.get("username"),
            message.user.get("first_name"),
            message.content,
            message.message_type
        ))
        conn.commit()
        conn.close()



    def mark_daily_submitted(self, dev: str, date: str, message_id: int):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO daily_reports (dev, date, submitted, message_id) VALUES (?, ?, 1, ?)",
            (dev, date, message_id))
        conn.commit()
        conn.close()

    def check_daily_submitted(self, dev: str, date: str) -> bool:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT submitted FROM daily_reports WHERE dev = ? AND date = ?",
            (dev, date))
        result = cursor.fetchone()
        conn.close()
        return result[0] == 1 if result else False

    def add_loom_reminder(self, dev: str, mr_id: int, title: str, url: str = None):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO loom_reminders (dev, mr_id, title, url) VALUES (?, ?, ?, ?)",
            (dev, mr_id, title, url))
        conn.commit()
        conn.close()

    def get_pending_loom_reminders(self, dev: str) -> list:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM loom_reminders WHERE dev = ? AND status = 'pending'",
            (dev,))
        reminders = [dict(zip(['id', 'dev', 'mr_id', 'title', 'status', 'url'], row)) for row in cursor.fetchall()]
        conn.close()
        return reminders

    def _generate_activity_description(event_type: str, payload: dict) -> str:
        """Generate human-readable description of GitLab event"""
        try:
            if event_type == 'push':
                commits_count = len(payload.get('commits', []))
                branch = payload.get('ref', '').replace('refs/heads/', '')
                repo = payload.get('project', {}).get('name', 'repository')
                return f"Pushed {commits_count} commit(s) to {branch} in {repo}"

            elif event_type == 'merge_request':
                mr = payload.get('object_attributes', {})
                action = mr.get('action', 'updated')
                title = mr.get('title', 'MR')[:50]
                return f"Merge request {action}: {title}"

            elif event_type == 'issue':
                issue = payload.get('object_attributes', {})
                action = issue.get('action', 'updated')
                title = issue.get('title', 'Issue')[:50]
                return f"Issue {action}: {title}"

            elif event_type == 'note':
                note = payload.get('object_attributes', {})
                noteable_type = note.get('noteable_type', 'object')
                return f"Commented on {noteable_type.lower()}"

            else:
                return f"{event_type.replace('_', ' ').title()} activity"

        except Exception:
            return f"{event_type} activity"

    def get_users_for_daily_check(self):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT dev FROM gitlab_events")  # или из конфигурации
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users

    def get_last_daily_message(self, dev: str, date: str):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT message_id, content, timestamp 
            FROM messages 
            WHERE (username = ? OR first_name LIKE ?)
              AND date(timestamp) = ? 
              AND (content LIKE '/daily%' OR content LIKE '%#daily%')
            ORDER BY timestamp DESC 
            LIMIT 1
        """, (dev, f"%{dev}%", date))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"message_id": row[0], "content": row[1], "timestamp": row[2]}
        return None

    def get_facts_for_user(self, username: str, since_hours: int = 24) -> dict:
        """
        Get recent GitLab activity facts for a specific user

        Args:
            username: GitLab username
            since_hours: How many hours back to look (default 24)

        Returns:
            Dict containing user facts and activity summary
        """
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        since_timestamp = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()

        # Get all events for the user in the time period
        cursor.execute(
            "SELECT * FROM gitlab_events WHERE dev = ? AND ts >= ? ORDER BY ts DESC",
            (username, since_timestamp)
        )

        raw_events = cursor.fetchall()
        events = [dict(zip(['id', 'dev', 'ts', 'type', 'payload'], row)) for row in raw_events]

        # Parse events and extract meaningful facts
        facts = {
            'username': username,
            'period_hours': since_hours,
            'total_events': len(events),
            'event_summary': {},
            'activities': [],
            'repositories': set(),
            'branches': set(),
            'merge_requests': [],
            'issues': [],
            'commits': [],
            'last_activity': None
        }

        for event in events:
            try:
                payload = json.loads(event['payload'])
                event_type = event['type']
                timestamp = event['ts']

                # Update last activity
                if not facts['last_activity'] or timestamp > facts['last_activity']:
                    facts['last_activity'] = timestamp

                # Count event types
                facts['event_summary'][event_type] = facts['event_summary'].get(event_type, 0) + 1

                # Extract repository info
                if 'project' in payload:
                    repo_name = payload['project'].get('name', 'unknown')
                    facts['repositories'].add(repo_name)

                # Process different event types
                if event_type == 'push':
                    # Extract commit info
                    commits = payload.get('commits', [])
                    for commit in commits:
                        facts['commits'].append({
                            'id': commit.get('id', '')[:8],
                            'message': commit.get('message', '').split('\n')[0][:100],
                            'timestamp': commit.get('timestamp', timestamp),
                            'repository': repo_name if 'repo_name' in locals() else 'unknown'
                        })

                    # Extract branch info
                    if 'ref' in payload:
                        branch = payload['ref'].replace('refs/heads/', '')
                        facts['branches'].add(branch)

                elif event_type == 'merge_request':
                    mr = payload.get('object_attributes', {})
                    facts['merge_requests'].append({
                        'iid': mr.get('iid'),
                        'title': mr.get('title', '')[:100],
                        'state': mr.get('state'),
                        'action': mr.get('action'),
                        'source_branch': mr.get('source_branch'),
                        'target_branch': mr.get('target_branch'),
                        'repository': repo_name if 'repo_name' in locals() else 'unknown'
                    })

                elif event_type == 'issue':
                    issue = payload.get('object_attributes', {})
                    facts['issues'].append({
                        'iid': issue.get('iid'),
                        'title': issue.get('title', '')[:100],
                        'state': issue.get('state'),
                        'action': issue.get('action'),
                        'repository': repo_name if 'repo_name' in locals() else 'unknown'
                    })

                # Add to activities timeline
                facts['activities'].append({
                    'timestamp': timestamp,
                    'type': event_type,
                    'description': DatabaseManager._generate_activity_description(event_type, payload)
                })

            except json.JSONDecodeError:
                print(f"{Fore.YELLOW}⚠️ Could not parse payload for event {event['id']}")
                continue
            except Exception as e:
                print(f"{Fore.YELLOW}⚠️ Error processing event {event['id']}: {e}")
                continue

        # Convert sets to lists for JSON serialization
        facts['repositories'] = list(facts['repositories'])
        facts['branches'] = list(facts['branches'])

        # Sort activities by timestamp (most recent first)
        facts['activities'].sort(key=lambda x: x['timestamp'], reverse=True)

        conn.close()
        return facts


db_manager = DatabaseManager()


@app.middleware("http")
async def log_requests(request, call_next):
    start_time = time.time()
    print(f"{Fore.CYAN}🌐 {request.method} {request.url.path}")

    response = await call_next(request)
    process_time = time.time() - start_time
    status_code = response.status_code

    print(f"{Fore.GREEN}✅ Ответ: {status_code} ({process_time:.2f}s)")
    return response


@app.get("/health")
async def health_check():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.close()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "uptime": str(datetime.now() - stats.start_time),
        "database": db_status
    }


@app.get("/scheduler/status")
async def scheduler_status():
    jobs = []
    if scheduler is None:
        return {"running": False, "jobs": [], "timezone": TIMEZONE}

    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time),
            "function": getattr(job.func, "__name__", str(job.func))
        })
    return {
        "running": scheduler.running,
        "jobs": jobs,
        "timezone": str(scheduler.timezone)
    }



@app.post("/process_message")
async def process_message(message_data: MessageData):
    """Обработка сообщения"""
    print(f"\n{Fore.YELLOW}📨 Получено сообщение от сервера:")
    print(f"👤 Пользователь: {message_data.user['first_name']}")
    print(f"💬 Чат: {message_data.chat['title']}")
    print(f"📝 Содержание: {message_data.content}")

    stats.messages_processed += 1
    stats.last_activity = datetime.now()

    return {
        "should_reply": "testing",
        "response": "Hi",
        "processed_at": datetime.now().isoformat()
    }


import os
from datetime import datetime

BOT_USERNAME = os.getenv('BOT_USERNAME', '')  # Get bot username from environment


@app.post("/save_message")
async def save_message(message_data: MessageData):
    try:
        # First save the message normally
        db_manager.save_message(message_data)
        print(message_data)

        # Check if message starts with /daily command
        content = message_data.content.strip()
        tokens = content.split()

        if not tokens:
            return {"status": "success", "id": message_data.message_id}

        command = tokens[0]
        valid_commands = ['/daily', f'/daily@{BOT_USERNAME}'] if BOT_USERNAME else ['/daily']

        if command in valid_commands:
            # Get GitLab username from Telegram user
            telegram_id = message_data.user.id
            dev = db_manager.get_gitlab_username(telegram_id)

            if not dev:
                raise HTTPException(
                    status_code=400,
                    detail="GitLab username not mapped. Use /map_me command first."
                )

            # Parse date and content
            remaining_tokens = tokens[1:]
            if not remaining_tokens:
                raise HTTPException(
                    status_code=400,
                    detail="Please provide content after /daily command"
                )

            # Try to parse date if second token is in YYYY-MM-DD format
            date_str = None
            content_tokens = remaining_tokens

            try:
                if len(remaining_tokens) >= 1:
                    datetime.strptime(remaining_tokens[0], '%Y-%m-%d')
                    date_str = remaining_tokens[0]
                    content_tokens = remaining_tokens[1:]
            except ValueError:
                # Not a date, use current date
                date_str = datetime.now().strftime('%Y-%m-%d')

            content = ' '.join(content_tokens)

            # Save daily report
            db_manager.save_daily_report(
                dev=dev,
                date=date_str,
                content=content,
                message_id=message_data.message_id
            )

            return {
                "status": "success",
                "id": message_data.message_id,
                "daily_report_saved": True,
                "dev": dev,
                "date": date_str
            }

        return {"status": "success", "id": message_data.message_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {e}")


class BotManager:
    def __init__(self):
        self.bot_token = None
        self.bot_instance = None
        self.registered_at = None

    async def register_bot(self, bot_info: Dict[str, Any]):
        """Register bot instance"""
        self.bot_token = bot_info.get('bot_token')
        self.registered_at = bot_info.get('registered_at')

        # Create bot instance for sending messages
        if self.bot_token:
            from telegram import Bot
            self.bot_instance = Bot(token=self.bot_token)
            print(f"{Fore.GREEN}✅ Bot registered and ready for messaging")
            return True
        return False

    async def send_message_to_chat(self, chat_id: int, message: str):
        """Send message to specific chat"""
        if not self.bot_instance:
            print(f"{Fore.RED}❌ Bot not registered")
            return False

        try:
            await self.bot_instance.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='HTML'
            )
            return True
        except Exception as e:
            print(f"{Fore.RED}❌ Error sending message: {e}")
            return False


# Global bot manager
bot_manager = BotManager()


# Add to your server.py endpoints

@app.post("/bot/register")
async def register_bot(bot_info: Dict[str, Any]):
    """Register bot with server for message sending"""
    try:
        success = await bot_manager.register_bot(bot_info)
        if success:
            return {
                "status": "success",
                "message": "Bot registered successfully",
                "registered_at": datetime.now().isoformat()
            }
        else:
            raise HTTPException(status_code=400, detail="Bot registration failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration error: {str(e)}")




# Global scheduler variable (will be created in startup event)
scheduler: AsyncIOScheduler = None

# --- Job functions (async so they run cleanly in AsyncIO loop) ---
async def morning_digest():
    try:
        current_time = datetime.now(timezone(TIMEZONE))
        print(f"\n{Fore.YELLOW}🌅 [MORNING_DIGEST] =================================")
        print(f"{Fore.YELLOW}⏰ Задача запущена в {current_time}")
        print(f"{Fore.YELLOW}📧 Отправка утренних напоминаний...")

        logger.info(f"[morning_digest] Задача выполнена в {current_time}")

        # пример работы
        await asyncio.sleep(0)  # yield to event loop

        print(f"{Fore.GREEN}✅ Утренний дайджест отправлен")
        print(f"{Fore.YELLOW}🌅 [MORNING_DIGEST] ЗАВЕРШЕНО ======================\n")

    except Exception as e:
        print(f"{Fore.RED}❌ [morning_digest] Ошибка: {e}")
        logger.error(f"[morning_digest] Ошибка: {e}")


async def send_daily_reminder(dev: str, date: str):
    """Send private reminder to user about missing daily report"""
    try:
        # First try to get Telegram ID from user_mapping table
        telegram_id = db_manager.get_telegram_id(dev)

        if telegram_id:
            print(f"{Fore.CYAN}📨 Found Telegram ID for {dev}: {telegram_id}")
        else:
            # Fallback: try to find user ID from messages table
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT user_id FROM messages 
                WHERE (username = ? OR first_name LIKE ?)
                ORDER BY timestamp DESC 
                LIMIT 1
            """, (dev, f"%{dev}%"))
            result = cursor.fetchone()
            conn.close()

            if result:
                telegram_id = result[0]
                print(f"{Fore.CYAN}📨 Found Telegram ID for {dev} in messages: {telegram_id}")
            else:
                print(f"{Fore.YELLOW}⚠️ Could not find Telegram ID for {dev}")
                return

        if bot_manager and bot_manager.bot_instance:
            formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")

            reminder_message = f"""
🔔 Ежедневный отчет: напоминание!

Привет! Мы заметили, что у тебя нет отчета за {formatted_date}.

Пожалуйста, пришли отчет в таком формате 
/daily 
факты за вчера (GitLab):
• MR#… “…”
• Коммиты: …
• Комментарии: …

часы (Sheets, вчера): X.X

шаблон:
/daily
1) Что сделал вчера:
• …

2) План на сегодня (шаг ≤3ч):
• …
• …

3) Блокеры:
• Нет (или: опиши кратко проблему и что нужно)
"""

            await bot_manager.send_message_to_chat(telegram_id, reminder_message)
            print(f"{Fore.GREEN}📨 Sent private reminder to {dev} (Telegram ID: {telegram_id})")
        else:
            print(f"{Fore.YELLOW}⚠️ Could not send reminder to {dev} - bot not available")

    except Exception as e:
        print(f"{Fore.RED}❌ Error sending reminder to {dev}: {e}")

async def send_user_morning_digest(self, username: str, config: Dict[str, Any]):
    """Enhanced version that includes daily reports"""
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # GitLab: Use existing get_facts_for_user, filter to yesterday
        facts = self.db_manager.get_facts_for_user(username)
        gitlab_data = self._filter_facts_to_date(facts, yesterday)

        # Timesheet: Use the real checker
        timesheet_data = await self.get_timesheet_for_date(username, yesterday)

        # Daily Report: Get the daily report
        daily_report_data = await self.get_daily_report_for_date(username, yesterday)

        # Format and send
        digest_message = self.format_enhanced_morning_digest(
            username, yesterday, gitlab_data, timesheet_data, daily_report_data
        )

        chat_id = config.get('chat_id')
        if chat_id and self.bot_manager and self.bot_manager.bot_instance:
            await self.bot_manager.bot_instance.send_message(
                chat_id=chat_id,
                text=digest_message
            )
            print(f"{Fore.GREEN}Enhanced digest sent to {username}")
        else:
            print(f"{Fore.YELLOW}No chat or bot for {username}. Message: {digest_message}")
    except Exception as e:
        print(f"{Fore.RED}Digest for {username} failed: {e}")


async def check_daily():
    """Check daily reports for all developers"""
    try:
        current_time = datetime.now(timezone(TIMEZONE))
        yesterday = (current_time - timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"\n{Fore.CYAN}📊 [CHECK_DAILY] ==================================")
        print(f"{Fore.CYAN}⏰ Task started at {current_time}")
        print(f"{Fore.CYAN}📅 Checking reports for: {yesterday}")

        # Get list of users who should submit reports
        users_to_check = db_manager.get_users_for_daily_check()

        for dev in users_to_check:
            print(f"{Fore.CYAN}👤 Checking daily report for: {dev}")

            # Check if report already exists in database
            existing_report = db_manager.get_daily_report(dev, yesterday)

            if existing_report and existing_report.get('submitted'):
                print(f"{Fore.GREEN}✅ {dev} already has submitted report for {yesterday}")
                continue
            telegram_id = db_manager.get_telegram_id(dev)
            print(f"{Fore.CYAN}   Mapped Telegram ID: {telegram_id}")

            # Check if report already exists in database
            existing_report = db_manager.get_daily_report(dev, yesterday)
            print(f"{Fore.CYAN}   Existing report: {existing_report is not None}")

            # Look for /daily message
            daily_message = db_manager.get_last_daily_message(dev, yesterday)

            if daily_message:
                print(f"{Fore.GREEN}📝 Found /daily message from {dev}")

                # Extract the report content (everything after /daily)
                content = daily_message['content']
                if content.startswith('/daily'):
                    report_content = content[6:].strip()  # Remove '/daily' and whitespace
                else:
                    report_content = content.strip()

                # Save to daily_reports table
                db_manager.save_daily_report(
                    dev=dev,
                    date=yesterday,
                    content=report_content,
                    message_id=daily_message['message_id']
                )

                print(f"{Fore.GREEN}✅ Saved daily report for {dev}")

            else:
                print(f"{Fore.YELLOW}⚠️ No /daily message found for {dev}")

                # Send private reminder via bot
                await send_daily_reminder(dev, yesterday)

        print(f"{Fore.GREEN}✅ Daily report check completed")
        print(f"{Fore.CYAN}📊 [CHECK_DAILY] ЗАВЕРШЕНО =========================\n")

    except Exception as e:
        print(f"{Fore.RED}❌ [check_daily] Error: {e}")
        logger.error(f"[check_daily] Error: {e}")

def job_listener(event):
    if event.exception:
        print(f"❌ Job {event.job_id} crashed: {event.exception}")
    else:
        print(f"✅ Job {event.job_id} executed at {datetime.now()}")


# --- Startup & shutdown handlers ---
@app.on_event("startup")
async def startup_event():
    global scheduler
    init(autoreset=True)

    print(f"{Fore.CYAN}🚀 Starting server... (startup event)")
    print(f"{Fore.YELLOW}🔗 Available at: http://{HOST}:{PORT}")
    print(f"{Fore.YELLOW}📋 API docs: http://{HOST}:{PORT}/docs")

    tz = timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    # Clear any existing jobs
    scheduler.remove_all_jobs()

    from config import USER_MORNING_DIGEST
    digest_scheduler = UserDigestScheduler(db_manager)

    # Schedule individual user digests
    digest_scheduler.schedule_user_digests(scheduler, USER_MORNING_DIGEST)

    # Keep your existing scheduled jobs
    deadline_hour = int(DAILY_DEADLINE_TIME.split(":")[0])
    deadline_minute = int(DAILY_DEADLINE_TIME.split(":")[1])

    scheduler.add_job(
        check_daily,
        CronTrigger(hour=deadline_hour, minute=deadline_minute, timezone=tz),
        id='check_daily_reports',
        replace_existing=True
    )

    scheduler.add_job(
        check_timesheet_compliance,
        CronTrigger(hour=18, minute=0, timezone=tz),
        id='timesheet_compliance_check',
        replace_existing=True
    )

    # Add listener and start scheduler
    scheduler.add_listener(job_listener, mask=EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.start()

    print(f"{Fore.GREEN}⏰ Individual user digests scheduled")
    print(f"{Fore.GREEN}⏰ Scheduler started (startup event)")

@app.on_event("shutdown")
async def shutdown_event():
    global scheduler
    print(f"{Fore.RED}⛔ Shutting down server (shutdown event)")
    if scheduler:
        scheduler.shutdown(wait=False)
        print(f"{Fore.RED}❌ Scheduler stopped")


@app.get("/facts/{username}")
async def get_user_facts(username: str, hours: int = 24):
    """Get recent GitLab activity facts for a user"""
    try:
        facts = db_manager.get_facts_for_user(username, hours)

        print(f"{Fore.CYAN}📊 Facts requested for user: {username}")
        print(f"{Fore.CYAN}🕐 Period: {hours} hours")
        print(f"{Fore.CYAN}📈 Total events: {facts['total_events']}")

        return {
            "status": "success",
            "facts": facts,
            "generated_at": datetime.now().isoformat()
        }

    except Exception as e:
        print(f"{Fore.RED}❌ Error getting facts for {username}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving user facts: {str(e)}")

@app.post("/daily/submit")
async def submit_daily_report(report: Dict):
    username = report['username']
    date = report['date']
    content = report['content']
    message_id = report['message_id']
    db_manager.save_daily_report(username, date, content, message_id)
    return {"status": "success"}


@app.post("/gitlab/webhook")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    """Endpoint for GitLab webhooks. Now with Loom reminder crap for merged MRs."""
    try:
        payload = await request.json()
        event_type = payload.get('object_kind', 'unknown')
        headers = dict(request.headers)

        print(f"{Fore.CYAN}📨 Received GitLab webhook:")
        print(f"{Fore.CYAN}🔧 Event Type: {event_type}")

        user_info = extract_user_from_gitlab_payload(payload)
        dev_username = user_info.get('username', 'unknown')

        background_tasks.add_task(
            save_gitlab_webhook,
            dev_username,
            event_type,
            payload
        )

        # Your new BS: Handle merged MRs with conditions.
        if event_type == 'merge_request':
            mr_attrs = payload.get('object_attributes', {})
            if mr_attrs.get('action') == 'merge':
                # Grab labels.
                label_titles = [l.get('title', '') for l in payload.get('labels', [])]
                has_feature_label = 'feature' in label_titles

                # Grab project and MR deets.
                project = payload.get('project', {})
                project_id = project.get('id')
                project_path = project.get('path_with_namespace', 'unknown/project')
                mr = payload.get('merge_request', {})
                mr_iid = mr.get('iid')
                mr_title = mr.get('title', 'Untitled MR')
                mr_url = mr.get('url', '')

                # Fetch changed files count (async, baby).
                changed_files = await get_changed_files_count(project_id, mr_iid)

                # Condition check.
                if has_feature_label or changed_files > N_CHANGED_FILES:
                    # Add to loom_reminders.
                    db_manager.add_loom_reminder(
                        dev=dev_username,
                        mr_id=mr_iid,  # Using iid, change to id if you want global.
                        title=mr_title,
                        url=mr_url
                    )
                    print(f"{Fore.GREEN}✅ Loom reminder added for {dev_username} on MR !{mr_iid}")

                    # Build the DM text.
                    mr_ref = f"{project_path}!{mr_iid}"
                    dm_text = (
                        f"Feature closed: MR#{mr_ref} “{mr_title}” (automatic). "
                        "Need Loom-note: • what, why, what QA and design should be looking for"
                    )

                    # Send DM if we can find Telegram ID.
                    telegram_id = db_manager.get_telegram_id(dev_username)
                    if telegram_id and bot_manager and bot_manager.bot_instance:
                        await bot_manager.send_message_to_chat(telegram_id, dm_text)
                        print(f"{Fore.GREEN}✅ DM fired to {dev_username} ({telegram_id})")
                    else:
                        print(f"{Fore.YELLOW}⚠️ No Telegram ID or bot for {dev_username}. Skipped DM.")

        return {"status": "processed", "user": dev_username, "event_type": event_type}

    except Exception as e:
        print(f"{Fore.RED}❌ Error processing GitLab webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")


def extract_user_from_gitlab_payload(payload: dict) -> dict:
    """Grab user info. For MRs, prefer the author if it's a merge."""
    user = {}
    event_type = payload.get('object_kind', 'unknown')

    if event_type == 'merge_request' and 'merge_request' in payload:
        # Use MR author for reminders, not the merger.
        user = payload['merge_request'].get('author', {})
    else:
        # Fallback to original logic.
        if 'user' in payload:
            user = payload['user']
        elif 'user' in payload.get('object_attributes', {}):
            user = payload['object_attributes']['user']
        elif 'user' in payload.get('commit', {}):
            user = payload['commit']['user']
        elif 'author' in payload.get('commit', {}):
            user = payload['commit']['author']

    return user


async def get_changed_files_count(project_id: int, mr_iid: int) -> int:
    """Hit GitLab API for changed files count. If it flops, assume 0."""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Private-Token": GITLAB_TOKEN}
            url = f"{GITLAB_URL}/projects/{project_id}/merge_requests/{mr_iid}/changes"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return len(data.get('changes', []))
                print(f"{Fore.RED}❌ GitLab API barfed: {resp.status}")
                return 0
    except Exception as e:
        print(f"{Fore.RED}❌ API call exploded: {e}")
        return 0

def save_gitlab_webhook(dev: str, event_type: str, payload: dict):
    """Save GitLab webhook to database"""
    try:
        event_type = payload.get('object_kind', 'unknown')

        db_manager.add_gitlab_event(dev, event_type, payload)
        print(f"{Fore.GREEN}✅ GitLab event saved for user: {dev}, type: {event_type}")
    except Exception as e:
        print(f"{Fore.RED}❌ Error saving GitLab event: {e}")





@dataclass
class TimeEntry:
    date: str
    hours: float
    description: str
    row_number: int


@dataclass
class DeveloperSheet:
    username: str
    sheet_id: str
    worksheet_name: str = "Sheet1"  # Default worksheet name


class GoogleSheetsTimeTracker:
    def __init__(self, credentials_file: str, developers_config: Dict[str, DeveloperSheet]):
        """
        Initialize Google Sheets time tracker

        Args:
            credentials_file: Path to Google service account credentials JSON
            developers_config: Dict mapping usernames to their sheet configurations
        """
        self.gc = gspread.service_account(filename=credentials_file)
        self.developers = developers_config

    def get_worksheet(self, dev_username: str):
        """Get worksheet for a specific developer"""
        if dev_username not in self.developers:
            raise ValueError(f"No sheet configuration found for developer: {dev_username}")

        dev_config = self.developers[dev_username]
        spreadsheet = self.gc.open_by_key(dev_config.sheet_id)
        return spreadsheet.worksheet(dev_config.worksheet_name)

    def parse_time_entries(self, worksheet, date_col: int = 4, hours_col: int = 7, desc_col: int = 6) -> List[
        TimeEntry]:
        """
        Parse time entries from worksheet

        Args:
            worksheet: gspread worksheet object
            date_col: Column number for dates (1-indexed)
            hours_col: Column number for hours (1-indexed)
            desc_col: Column number for descriptions (1-indexed)
        """
        entries = []

        # Get all values from the sheet
        all_values = worksheet.get_all_values()

        # Skip header row (assuming row 1 is header)
        for row_idx, row in enumerate(all_values[1:], start=2):  # start=2 because we skip header
            if len(row) >= max(date_col, hours_col, desc_col):
                date_value = row[date_col - 1].strip() if date_col - 1 < len(row) else ""
                hours_value = row[hours_col - 1].strip() if hours_col - 1 < len(row) else ""
                desc_value = row[desc_col - 1].strip() if desc_col - 1 < len(row) else ""

                # Parse date
                parsed_date = self._parse_date(date_value)
                if not parsed_date:
                    continue

                # Parse hours
                hours = self._parse_hours(hours_value)

                entries.append(TimeEntry(
                    date=parsed_date,
                    hours=hours,
                    description=desc_value,
                    row_number=row_idx
                ))

        return entries

    def _parse_date(self, date_str: str) -> Optional[str]:
        """Parse various date formats to YYYY-MM-DD"""
        if not date_str:
            return None

        # Common date formats
        date_formats = [
            "%Y-%m-%d",  # 2024-01-15
            "%d/%m/%Y",  # 15/01/2024
            "%m/%d/%Y",  # 01/15/2024
            "%d.%m.%Y",  # 15.01.2024
            "%d-%m-%Y",  # 15-01-2024
        ]

        for fmt in date_formats:
            try:
                parsed = datetime.strptime(date_str, fmt)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None

    def _parse_hours(self, hours_str: str) -> float:
        """Parse hours from string (handles decimal and fractional formats)"""
        if not hours_str:
            return 0.0

        # Remove any non-numeric characters except . and ,
        cleaned = re.sub(r'[^\d.,]', '', hours_str)
        if not cleaned:
            return 0.0

        # Replace comma with dot for decimal parsing
        cleaned = cleaned.replace(',', '.')

        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    def get_missing_entries(self, dev_username: str, target_date: str) -> Tuple[bool, TimeEntry]:
        """
        Check if developer has time entry for a specific date

        Args:
            dev_username: Developer username
            target_date: Date to check in YYYY-MM-DD format

        Returns:
            Tuple of (has_entry, time_entry_or_none)
        """
        try:
            worksheet = self.get_worksheet(dev_username)
            entries = self.parse_time_entries(worksheet)

            for entry in entries:
                if entry.date == target_date:
                    return True, entry

            return False, None

        except Exception as e:
            print(f"Error checking entries for {dev_username}: {e}")
            return False, None

    def check_multiple_dates(self, dev_username: str, dates: List[str]) -> Dict[str, Tuple[bool, TimeEntry]]:
        """Check multiple dates at once for efficiency"""
        results = {}

        try:
            worksheet = self.get_worksheet(dev_username)
            entries = self.parse_time_entries(worksheet)

            # Create a lookup dict for faster searching
            entries_by_date = {entry.date: entry for entry in entries}

            for date in dates:
                if date in entries_by_date:
                    entry = entries_by_date[date]
                    # Check if entry has meaningful hours/description
                    has_meaningful_entry = entry.hours > 0 or bool(entry.description.strip())
                    results[date] = (has_meaningful_entry, entry)
                else:
                    results[date] = (False, None)

        except Exception as e:
            print(f"Error checking multiple dates for {dev_username}: {e}")
            # Return False for all dates on error
            results = {date: (False, None) for date in dates}

        return results


class TimeTrackingIntegration:
    def __init__(self, db_manager: DatabaseManager, sheets_tracker: GoogleSheetsTimeTracker):
        self.db_manager = db_manager
        self.sheets_tracker = sheets_tracker

    async def check_missing_time_entries(self, dev_username: str, days_back: int = 7) -> List[Dict]:
        """
        Check for GitLab activity without corresponding time entries

        Returns list of dates with missing time entries
        """
        missing_entries = []

        # Get GitLab events for the period
        gitlab_events = self.db_manager.get_gitlab_events(dev_username, days_back * 24)

        if not gitlab_events:
            return missing_entries

        # Extract unique dates from GitLab events
        event_dates = set()
        for event in gitlab_events:
            try:
                event_date = datetime.fromisoformat(event['ts']).strftime("%Y-%m-%d")
                event_dates.add(event_date)
            except (ValueError, KeyError):
                continue

        # Check time entries for all dates at once
        time_entries_status = self.sheets_tracker.check_multiple_dates(
            dev_username, list(event_dates)
        )

        # Find dates with GitLab activity but no/insufficient time entries
        for date in event_dates:
            has_entry, time_entry = time_entries_status.get(date, (False, None))

            if not has_entry:
                # Get GitLab events for this specific date
                date_events = [
                    event for event in gitlab_events
                    if datetime.fromisoformat(event['ts']).strftime("%Y-%m-%d") == date
                ]

                missing_entries.append({
                    'date': date,
                    'gitlab_events_count': len(date_events),
                    'gitlab_events': date_events,
                    'time_entry': time_entry,
                    'severity': 'missing' if not time_entry else 'incomplete'
                })

        return missing_entries

    async def generate_reminder_message(self, dev_username: str, missing_entries: List[Dict]) -> str:
        """Generate a friendly reminder message for missing time entries"""
        if not missing_entries:
            return ""

        message_parts = [
            f"@{dev_username}!",
            "",
            f"Мы заметили, что у вас отсутствует активность за {len(missing_entries)} дней:",
            ""
        ]

        for entry in missing_entries:
            date = entry['date']
            events_count = entry['gitlab_events_count']

            # Format date nicely
            try:
                formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")
            except ValueError:
                formatted_date = date

            message_parts.append(f"📅 {formatted_date} - {events_count} GitLab event(s)")

            # Show some activity details
            for event in entry['gitlab_events'][:2]:  # Show max 2 events per day
                event_type = event.get('type', 'activity')
                message_parts.append(f"   • {event_type.replace('_', ' ').title()}")

            if len(entry['gitlab_events']) > 2:
                message_parts.append(f"   • ... and {len(entry['gitlab_events']) - 2} more")

            message_parts.append("")

        message_parts.extend([
            "⏰ Пожалуйста, обновите вашу таблицу!"
        ])

        return "\n".join(message_parts)


# Add this to your existing server.py functions
async def check_timesheet_compliance():
    """Updated scheduled job to check timesheet compliance"""
    try:
        current_time = datetime.now(timezone(TIMEZONE))
        print(f"\n{Fore.MAGENTA}📊 [TIMESHEET_CHECK] ==============================")
        print(f"{Fore.MAGENTA}⏰ Task started at {current_time}")

        # Use the helper function to get configuration
        developers_config = get_developers_config()

        sheets_tracker = GoogleSheetsTimeTracker(
            credentials_file=GOOGLE_CREDENTIALS_FILE,
            developers_config=developers_config
        )

        integration = TimeTrackingIntegration(db_manager, sheets_tracker)

        # Check each developer
        for dev_username in developers_config.keys():
            print(f"{Fore.MAGENTA}👤 Checking timesheet for: {dev_username}")

            missing_entries = await integration.check_missing_time_entries(
                dev_username,
                TIMESHEET_CHECK_DAYS_BACK
            )

            if missing_entries:
                print(f"{Fore.YELLOW}⚠️ Found {len(missing_entries)} missing time entries for {dev_username}")

                # Generate reminder message
                reminder = await integration.generate_reminder_message(dev_username, missing_entries)

                # Here you would send the reminder via your preferred method
                print(f"{Fore.YELLOW}📝 Reminder message generated for {dev_username}")
                # TODO: Implement actual reminder sending
                # await send_telegram_message(dev_username, reminder)

            else:
                print(f"{Fore.GREEN}✅ {dev_username} is up to date with timesheet")

        print(f"{Fore.GREEN}✅ Timesheet compliance check completed")
        print(f"{Fore.MAGENTA}📊 [TIMESHEET_CHECK] ЗАВЕРШЕНО ==================\n")

    except Exception as e:
        print(f"{Fore.RED}❌ [timesheet_check] Error: {e}")
        logger.error(f"[timesheet_check] Error: {e}")


class TimesheetCheckRequest(BaseModel):
    username: str
    days_back: int = 7
    send_reminder: bool = False

class TimesheetCheckResponse(BaseModel):
    status: str
    username: str
    missing_entries_count: int
    missing_entries: List[Dict]
    reminder_message: str = ""
    checked_at: str
# You'll also need to add this job to your scheduler in startup_event():
# scheduler.add_job(
#     check_timesheet_compliance,
#     CronTrigger(hour=18, minute=0, timezone=tz),  # Run at 6 PM daily
#     id='timesheet_compliance_check',
#     replace_existing=True
def get_developers_config() -> Dict[str, DeveloperSheet]:
    """
    Get developers configuration from your config file or environment
    Modify this function based on how you want to store the configuration
    """
    return {
            username: DeveloperSheet(
                username=username,
                sheet_id=sheet_config['sheet_id'],
                worksheet_name=sheet_config.get('worksheet_name', 'Sheet1')
            )
            for username, sheet_config in DEVELOPER_SHEETS.items()
        }


@app.post("/timesheet/check")
async def check_user_timesheet(request: TimesheetCheckRequest):
    """
    Check timesheet compliance for a specific user
    This endpoint can be called by your Telegram bot
    """
    try:
        print(f"{Fore.MAGENTA}📊 Manual timesheet check requested for: {request.username}")

        # Initialize Google Sheets integration
        developers_config = get_developers_config()  # We'll create this helper function

        if request.username not in developers_config:
            raise HTTPException(
                status_code=404,
                detail=f"No timesheet configuration found for user: {request.username}"
            )

        sheets_tracker = GoogleSheetsTimeTracker(
            credentials_file=GOOGLE_CREDENTIALS_FILE,
            developers_config=developers_config
        )

        integration = TimeTrackingIntegration(db_manager, sheets_tracker)

        # Check for missing entries
        missing_entries = await integration.check_missing_time_entries(
            request.username,
            request.days_back
        )

        # Generate reminder message if requested or if there are missing entries
        reminder_message = ""
        if missing_entries and (request.send_reminder or len(missing_entries) > 0):
            reminder_message = await integration.generate_reminder_message(
                request.username,
                missing_entries
            )

        response = TimesheetCheckResponse(
            status="success",
            username=request.username,
            missing_entries_count=len(missing_entries),
            missing_entries=missing_entries,
            reminder_message=reminder_message,
            checked_at=datetime.now().isoformat()
        )

        print(f"{Fore.GREEN}✅ Timesheet check completed for {request.username}")
        print(f"{Fore.CYAN}📊 Missing entries: {len(missing_entries)}")

        return response

    except HTTPException:
        raise
    except Exception as e:
        print(f"{Fore.RED}❌ Error checking timesheet for {request.username}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error checking timesheet: {str(e)}"
        )


@app.get("/timesheet/check/{username}")
async def check_user_timesheet_simple(username: str, days_back: int = 7):
    """
    Simple GET endpoint for timesheet check (for easier testing)
    """
    request = TimesheetCheckRequest(
        username=username,
        days_back=days_back,
        send_reminder=False
    )
    return await check_user_timesheet(request)


@app.post("/timesheet/check-all")
async def check_all_timesheets(days_back: int = 7):
    """
    Check timesheet compliance for all configured developers
    Useful for bulk checks or scheduled tasks
    """
    try:
        developers_config = get_developers_config()
        results = []

        sheets_tracker = GoogleSheetsTimeTracker(
            credentials_file=GOOGLE_CREDENTIALS_FILE,
            developers_config=developers_config
        )

        integration = TimeTrackingIntegration(db_manager, sheets_tracker)

        for username in developers_config.keys():
            try:
                print(f"{Fore.MAGENTA}📊 Проверка для: {username}")

                missing_entries = await integration.check_missing_time_entries(username, days_back)

                reminder_message = ""
                if missing_entries:
                    reminder_message = await integration.generate_reminder_message(username, missing_entries)

                results.append({
                    "username": username,
                    "status": "checked",
                    "missing_entries_count": len(missing_entries),
                    "missing_entries": missing_entries,
                    "reminder_message": reminder_message
                })

                print(f"{Fore.GREEN}✅ {username}: {len(missing_entries)} missing entries")

            except Exception as e:
                print(f"{Fore.RED}❌ Error checking {username}: {e}")
                results.append({
                    "username": username,
                    "status": "error",
                    "error": str(e),
                    "missing_entries_count": 0,
                    "missing_entries": [],
                    "reminder_message": ""
                })

        return {
            "status": "completed",
            "total_users": len(developers_config),
            "checked_at": datetime.now().isoformat(),
            "results": results
        }

    except Exception as e:
        print(f"{Fore.RED}❌ Ошибка: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка в проверке: {str(e)}")


@app.get("/timesheet/entry/{username}/{date}")
async def get_timesheet_entry(username: str, date: str):
    """Get timesheet entry for a specific user and date"""
    try:
        developers_config = get_developers_config()

        if username not in developers_config:
            raise HTTPException(
                status_code=404,
                detail=f"No timesheet configuration found for user: {username}"
            )

        sheets_tracker = GoogleSheetsTimeTracker(
            credentials_file=GOOGLE_CREDENTIALS_FILE,
            developers_config=developers_config
        )

        # Check if user has an entry for the specific date
        has_entry, time_entry = sheets_tracker.get_missing_entries(username, date)

        return {
            "status": "success",
            "username": username,
            "date": date,
            "has_entry": has_entry,
            "entry": {
                "hours": time_entry.hours if time_entry else 0,
                "description": time_entry.description if time_entry else ""
            } if has_entry else None
        }

    except Exception as e:
        print(f"{Fore.RED}❌ Error getting timesheet entry: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error getting timesheet entry: {str(e)}"
        )


class UserDigestScheduler:
    def __init__(self, db_manager, bot_manager=None, sheets_tracker=None):
        self.db_manager = db_manager
        self.bot_manager = bot_manager or globals().get('bot_manager')
        self.sheets_tracker = sheets_tracker or GoogleSheetsTimeTracker(
            credentials_file=GOOGLE_CREDENTIALS_FILE,
            developers_config=get_developers_config()
        )  # Reuse the existing tracker
        self.scheduled_jobs = {}

    def schedule_user_digests(self, scheduler: AsyncIOScheduler, user_configs: Dict[str, Dict[str, Any]]):
        """Schedule digests for all users. No fluff."""
        for username, config in user_configs.items():
            if config.get('enabled', True):
                self.schedule_single_user_digest(scheduler, username, config)

    def schedule_single_user_digest(self, scheduler: AsyncIOScheduler, username: str, config: Dict[str, Any]):
        """Schedule for one user. If it fails, tough luck."""
        try:
            time_str = config.get('time', '09:00')
            hour, minute = map(int, time_str.split(':'))
            user_tz = pytz_timezone(config.get('timezone', 'UTC'))
            job_id = f'morning_digest_{username}'

            if job_id in self.scheduled_jobs:
                scheduler.remove_job(job_id)

            scheduler.add_job(
                self.send_user_morning_digest,
                CronTrigger(hour=hour, minute=minute, timezone=user_tz),
                id=job_id,
                args=[username, config],
                replace_existing=True
            )

            self.scheduled_jobs[job_id] = {'username': username, 'config': config}
            print(f"{Fore.GREEN}✅ Scheduled digest for {username} at {time_str} ({user_tz})")
        except Exception as e:
            print(f"{Fore.RED}❌ Couldn't schedule for {username}. Fix your config, idiot: {e}")

    async def send_user_morning_digest(self, username: str, config: Dict[str, Any]):
        """Send the damn digest. Uses existing functions, no reinvention."""
        try:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            # GitLab: Use existing get_facts_for_user, filter to yesterday
            facts = self.db_manager.get_facts_for_user(username)  # Buffer for timezone BS
            gitlab_data = self._filter_facts_to_date(facts, yesterday)

            # Timesheet: Use the real checker now, you lazy bastard
            timesheet_data = await self.get_timesheet_for_date(username, yesterday)

            # Format and send
            digest_message = self.format_user_morning_digest(username, yesterday, gitlab_data, timesheet_data)
            chat_id = config.get('chat_id')
            if chat_id and self.bot_manager and self.bot_manager.bot_instance:
                # Remove parse_mode parameter to send as plain text
                await self.bot_manager.bot_instance.send_message(
                    chat_id=chat_id,
                    text=digest_message  # No parse_mode parameter
                )
                print(f"{Fore.GREEN}✅ Digest blasted to {username}")
            else:
                print(f"{Fore.YELLOW}⚠️ No chat or bot for {username}. Message: {digest_message}")
        except Exception as e:
            print(f"{Fore.RED}❌ Digest for {username} exploded: {e}")

    def _filter_facts_to_date(self, facts: dict, date: str) -> dict:
        """Filter existing facts to one date. Simple, unlike your original mess."""
        if not facts:
            return {'total_events': 0, 'activities': [], 'event_summary': {}}

        daily_activities = [
            act for act in facts.get('activities', [])
            if act['timestamp'].startswith(date)
        ]
        return {
            'total_events': len(daily_activities),
            'activities': daily_activities,
            'event_summary': Counter(act['type'] for act in daily_activities)
        }

    async def get_timesheet_for_date(self, username: str, date: str) -> dict:
        """Actual implementation using sheets_tracker. No more fake data, princess."""
        try:
            has_entry, time_entry = self.sheets_tracker.get_missing_entries(username, date)  # Wait, that's backwards—it's get_missing_entries but returns (has_entry, entry)
            return {
                'has_entry': has_entry,
                'entry': {
                    'hours': time_entry.hours if time_entry else 0,
                    'description': time_entry.description if time_entry else ''
                } if has_entry else None
            }
        except Exception as e:
            print(f"{Fore.RED}❌ Timesheet fetch for {username} on {date} failed: {e}")
            return {'has_entry': False, 'entry': None}

    def format_user_morning_digest(self, username: str, date: str, gitlab_data: dict, timesheet_data: dict) -> str:
        """Keep it short and sweet. No novel-writing."""
        formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")
        lines = [
            f"🌅 Morning digest for @{username} - {formatted_date}",
            "",
            "GitLab Shenanigans:"  # Removed ** markers
        ]
        if gitlab_data['total_events'] > 0:
            lines.append(f"- {gitlab_data['total_events']} events total")
            for etype, count in gitlab_data['event_summary'].items():
                lines.append(f"  - {etype}: {count}")
            lines.append("- Highlights:")
            for act in gitlab_data['activities'][:3]:
                lines.append(f"    - {act['description']}")
        else:
            lines.append("- You slacked off? No activity.")

        lines.extend([
            "",
            "**Timesheet Status:**"
        ])
        if timesheet_data['has_entry']:
            entry = timesheet_data['entry']
            lines.append(f"- Hours: {entry['hours']}")
            lines.append(f"- Notes: {entry['description'][:100]}..." if entry['description'] else "- No notes, lazybones.")
        else:
            lines.append("- Missing! Log it before HR hunts you down.")

        return "\n".join(lines)



@app.get("/digest/generate/{username}")
async def generate_user_digest(username: str):
    """Generate digest without the old bloat. If it breaks again, it's on you."""
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        facts = db_manager.get_facts_for_user(username)  # Grab extra for timezone fuckery
        gitlab_data = UserDigestScheduler(db_manager)._filter_facts_to_date(facts, yesterday)
        timesheet_data = await UserDigestScheduler(db_manager).get_timesheet_for_date(username, yesterday)
        message = UserDigestScheduler(db_manager).format_user_morning_digest(username, yesterday, gitlab_data, timesheet_data)
        return {"status": "success", "username": username, "date": yesterday, "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Digest blew up again: {str(e)}. Maybe sacrifice a goat?")

# If executed directly, run with uvicorn (the scheduler starts on startup event)
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    uvicorn.run(app, host=HOST, port=PORT)
