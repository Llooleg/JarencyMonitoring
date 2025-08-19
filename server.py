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
from config import HOST, PORT, DATABASE_FILE, TIMEZONE, DB_ENCRYPTION_KEY, DB_ENCRYPTION_PASSWORD, DB_SALT, DAILY_REMIND_TIME, DAILY_DEADLINE_TIME
from encryption import DatabaseEncryption
from pytz import timezone
import logging
from fastapi import Request

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

        # Индексы для ускорения запросов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_gitlab_dev_ts ON gitlab_events(dev, ts)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_dev_date ON daily_reports(dev, date)')

        self.apply_migrations(cursor)

        conn.commit()
        conn.close()

    def apply_migrations(self, cursor):
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='migrations'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE migrations (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

        print(f"{Fore.GREEN}✅ Миграции БД применены")

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


async def check_daily():
    try:
        current_time = datetime.now(timezone(TIMEZONE))
        print(f"\n{Fore.CYAN}📊 [CHECK_DAILY] ==================================")
        print(f"{Fore.CYAN}⏰ Задача запущена в {current_time}")
        print(f"{Fore.CYAN}🔍 Проверка отчетов...")

        logger.info(f"[check_daily] Задача выполнена в {current_time}")

        await asyncio.sleep(0)

        print(f"{Fore.GREEN}✅ Проверка daily-отчетов завершена")
        print(f"{Fore.CYAN}📊 [CHECK_DAILY] ЗАВЕРШЕНО =========================\n")

    except Exception as e:
        print(f"{Fore.RED}❌ [check_daily] Ошибка: {e}")
        logger.error(f"[check_daily] Ошибка: {e}")


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

    print(f"{Fore.CYAN}🚀 Запуск сервера... (startup event)")
    print(f"{Fore.YELLOW}🔗 Доступен по адресу: http://{HOST}:{PORT}")
    print(f"{Fore.YELLOW}📋 Документация API: http://{HOST}:{PORT}/docs")

    tz = timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    # Clear any existing jobs (safe-guard if reloads)
    scheduler.remove_all_jobs()

    # Schedule morning digest (every day)
    remind_hour = int(DAILY_REMIND_TIME.split(":")[0])
    remind_minute = int(DAILY_REMIND_TIME.split(":")[1])

    scheduler.add_job(
        morning_digest,
        CronTrigger(hour=remind_hour, minute=remind_minute, timezone=tz),
        id='morning_digest_daily',
        replace_existing=True
    )
    print(f"{Fore.GREEN}✅ Morning digest scheduled daily at {DAILY_REMIND_TIME}")

    # Schedule daily check (every day)
    deadline_hour = int(DAILY_DEADLINE_TIME.split(":")[0])
    deadline_minute = int(DAILY_DEADLINE_TIME.split(":")[1])

    scheduler.add_job(
        check_daily,
        CronTrigger(hour=deadline_hour, minute=deadline_minute, timezone=tz),
        id='check_daily_reports',
        replace_existing=True
    )
    print(f"{Fore.GREEN}✅ Daily check scheduled daily at {DAILY_DEADLINE_TIME}")

    # Add listener
    scheduler.add_listener(job_listener, mask=EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    # Add a short immediate test job to verify scheduler works
    test_time_2 = datetime.now(tz) + timedelta(seconds=10)
    scheduler.add_job(
        morning_digest,
        'date',
        run_date=test_time_2,
        id='immediate_test',
        replace_existing=True
    )
    print(f"{Fore.YELLOW}🧪 Immediate test scheduled for {test_time_2}")

    # Start the scheduler
    scheduler.start()
    print(f"{Fore.GREEN}⏰ Планировщик запущен (startup event)")


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


@app.post("/gitlab/webhook")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    """Endpoint for GitLab webhooks"""
    try:
        payload = await request.json()
        event_type = payload.get('object_kind', 'unknown')
        headers = dict(request.headers)

        print(f"{Fore.CYAN}📨 Received GitLab webhook:")
        # Use the event_type from payload instead of headers
        print(f"{Fore.CYAN}🔧 Event Type: {event_type}")

        user_info = extract_user_from_gitlab_payload(payload)
        dev_username = user_info.get('username', 'unknown')

        background_tasks.add_task(
            save_gitlab_webhook,
            dev_username,
            event_type,  # Pass the correct event_type here too
            payload
        )

        return {"status": "processed", "user": dev_username, "event_type": event_type}

    except Exception as e:
        print(f"{Fore.RED}❌ Error processing GitLab webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")



def extract_user_from_gitlab_payload(payload: dict) -> dict:
    """Extract user information from GitLab webhook payload"""
    user = {}

    if 'user' in payload:
        user = payload['user']
    elif 'user' in payload.get('object_attributes', {}):
        user = payload['object_attributes']['user']
    elif 'user' in payload.get('commit', {}):
        user = payload['commit']['user']
    elif 'author' in payload.get('commit', {}):
        user = payload['commit']['author']

    return user





def save_gitlab_webhook(dev: str, event_type: str, payload: dict):
    """Save GitLab webhook to database"""
    try:
        event_type = payload.get('object_kind', 'unknown')

        db_manager.add_gitlab_event(dev, event_type, payload)
        print(f"{Fore.GREEN}✅ GitLab event saved for user: {dev}, type: {event_type}")
    except Exception as e:
        print(f"{Fore.RED}❌ Error saving GitLab event: {e}")

# If executed directly, run with uvicorn (the scheduler starts on startup event)
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    uvicorn.run(app, host=HOST, port=PORT)
