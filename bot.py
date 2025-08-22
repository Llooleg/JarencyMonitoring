import logging
import asyncio
import aiohttp
import json
import time
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from colorama import Fore, Style, init
from config import SERVER_URL, BOT_TOKEN, DATABASE_FILE
from telegram.ext import ChatMemberHandler
from typing import Dict, List, Optional, Tuple, Any
from server import db_manager, bot_manager
import sqlite3

init(autoreset=True)

# Logging setup – because who doesn't love watching their bot cry in logs?
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TelegramBotMonitor:
    def __init__(self):
        self.session = None
        self.message_count = 0
        self.error_count = 0
        self.server_responses = 0
        self.start_time = datetime.now()
        self.bot_instance = None  # For server comms, don't fuck this up

    async def start_session(self):
        """Create HTTP session – if this fails, your internet sucks."""
        import ssl

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
        )

        print(f"{Fore.GREEN}✅ HTTP session created like a boss")
        await self.check_server_health()

    async def register_bot_with_server(self):
        """Register bot with server – or die trying."""
        try:
            bot_info = {
                "bot_token": BOT_TOKEN,
                "status": "online",
                "registered_at": datetime.now().isoformat()
            }

            async with self.session.post(
                    f"{SERVER_URL}/bot/register",
                    json=bot_info,
                    timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"{Fore.GREEN}✅ Bot registered – server's happy for once")
                    return result
                else:
                    print(f"{Fore.RED}❌ Registration failed: {response.status}. Server hates you")
                    return None
        except Exception as e:
            print(f"{Fore.RED}❌ Registration error: {e}")
            return None

    async def send_digest_message(self, chat_id: int, message: str, username: str = None):
        """Send digest – hope the user doesn't choke on it."""
        try:
            print(f"{Fore.CYAN}📤 Firing digest to chat_id: {chat_id}")

            if self.bot_instance:
                await self.bot_instance.send_message(
                    chat_id=chat_id,
                    text=message,
                    #parse_mode='Markdown'  # For that sweet formatting
                )
                print(f"{Fore.GREEN}✅ Digest delivered. Bon appétit")
            else:
                print(f"{Fore.RED}❌ No bot instance? You're screwed")
        except Exception as e:
            print(f"{Fore.RED}❌ Digest send failed: {e}. Maybe the chat's dead anyway")

    async def manual_digest_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manual digest trigger – for when you're too impatient to wait for morning."""
        try:
            if len(context.args) < 1:
                await update.message.reply_text("❌ Gimme a username, dumbass: /digest username")
                return

            username = context.args[0]
            print(f"{Fore.CYAN}📨 Manual digest for {username} – because why not?")

            async with self.session.get(
                    f"{SERVER_URL}/daily/submit",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    digest_data = await response.json()
                    digest_message = digest_data.get('message', 'No digest, loser')
                    await update.message.reply_text(digest_message)
                    print(f"{Fore.GREEN}✅ Manual digest sent. Feel productive yet?")
                else:
                    await update.message.reply_text(f"❌ Digest gen failed: {response.status}. Server's on strike")
        except Exception as e:
            print(f"{Fore.RED}❌ Manual digest error: {e}. Try again after coffee")
            await update.message.reply_text("❌ Digest error – blame the dev")

    async def configure_digest_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Configure digest – admin only, or GTFO."""
        try:
            user_id = update.effective_user.id
            ADMIN_IDS = [123456789, 987654321]  # Your VIP list, change or perish

            if user_id not in ADMIN_IDS:
                await update.message.reply_text("❌ Admin only. Go play elsewhere")
                return

            if len(context.args) < 2:
                await update.message.reply_text(
                    "❌ Usage: /configure_digest username HH:MM [timezone]\nExample: /configure_digest john 09:00 Europe/Amsterdam"
                )
                return

            username = context.args[0]
            time_str = context.args[1]
            timezone_str = context.args[2] if len(context.args) > 2 else "UTC"

            config_data = {
                "chat_id": update.effective_chat.id,
                "time": time_str,
                "timezone": timezone_str,
                "enabled": True
            }

            async with self.session.post(
                    f"{SERVER_URL}/digest/configure/{username}",
                    json=config_data,
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    await update.message.reply_text(
                        f"✅ Digest set for @{username}\n⏰ {time_str} ({timezone_str})\n💬 In {update.effective_chat.title or 'this dump'}"
                    )
                else:
                    error_text = await response.text()
                    await update.message.reply_text(f"❌ Config failed: {error_text}. Try not sucking")
        except Exception as e:
            print(f"{Fore.RED}❌ Config error: {e}. Admin fail")
            await update.message.reply_text("❌ Config error – server's laughing at you")

    async def list_digests_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List digests – admin eyes only."""
        try:
            user_id = update.effective_user.id
            ADMIN_IDS = [123456789, 987654321]

            if user_id not in ADMIN_IDS:
                await update.message.reply_text("❌ Admin required. Buzz off")
                return

            async with self.session.get(
                    f"{SERVER_URL}/digest/status",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    status_data = await response.json()

                    if not status_data.get('jobs'):
                        await update.message.reply_text("📭 No digests configured. Everyone's slacking")
                        return

                    message_lines = [
                        f"📊 Digest Status ({status_data['total_scheduled']} lazy users):",
                        ""
                    ]

                    for job in status_data['jobs']:
                        username = job['username']
                        next_run = job.get('next_run', 'When hell freezes')
                        timezone_info = job.get('timezone', 'UTC')

                        message_lines.append(
                            f"👤 @{username}\n   ⏰ Next: {next_run}\n   🌍 {timezone_info}\n"
                        )

                    await update.message.reply_text("\n".join(message_lines))
                else:
                    await update.message.reply_text("❌ Status error. Server's hungover")
        except Exception as e:
            print(f"{Fore.RED}❌ List error: {e}. No digests for you")
            await update.message.reply_text("❌ List error – go cry to IT")

    async def morning_digest_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Morning digest – yesterday's sins summarized."""
        if not context.args:
            await update.message.reply_text("❌ Username, fool: /morning_digest username")
            return

        username = context.args[0]
        await update.message.reply_text(f"🌅 Brewing digest for {username}... Hope they didn't slack")

        try:
            async with self.session.get(
                    f"{SERVER_URL}/digest/generate/{username}",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    digest_data = await response.json()
                    digest_message = digest_data.get('message', 'No digest – they ghosted GitLab')
                    await update.message.reply_text(digest_message, parse_mode='Markdown')
                else:
                    await update.message.reply_text("❌ Digest gen failed. Server's dead inside")
        except Exception as e:
            print(f"{Fore.RED}❌ Morning digest error: {e}. Start your day with failure")
            await update.message.reply_text("❌ Digest error – maybe tomorrow, eh?")


    async def close_session(self):
        """Закрываем HTTP сессию"""
        if self.session:
            await self.session.close()
            print(f"{Fore.RED}❌ HTTP сессия закрыта")

    async def check_server_health(self):
        """Проверка здоровья сервера"""
        try:
            async with self.session.get(f"{SERVER_URL}/health") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"{Fore.GREEN}🟢 Сервер доступен: {data}")
                    return True
                else:
                    print(f"{Fore.RED}🔴 Сервер недоступен: {response.status}")
                    return False
        except Exception as e:
            print(f"{Fore.RED}🔴 Ошибка подключения к серверу: {e}")
            return False

    async def health_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для проверки здоровья системы"""
        server_ok = await self.check_server_health()

        if server_ok:
            await update.message.reply_text("✅ Система работает нормально\n🤖 ИИ-анализ готов к работе")
        else:
            await update.message.reply_text("❌ Проблемы с подключением к серверу")
    async def send_to_server(self, message_data):
        """Отправляем данные на сервер (только для сохранения)"""
        try:
            print(f"{Fore.CYAN}📤 Отправка сообщения на сервер для сохранения...")

            async with self.session.post(
                    f"{SERVER_URL}/save_message",
                    json=message_data,
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"{Fore.GREEN}✅ Сообщение сохранено на сервере")
                    return result
                else:
                    print(f"{Fore.RED}❌ Ошибка сервера: {response.status}")
                    self.error_count += 1
                    return None
        except Exception as e:
            print(f"{Fore.RED}❌ Ошибка отправки на сервер: {e}")
            self.error_count += 1
            return None

    async def get_gitlab_summary_from_server(self, dev):
        """Get GitLab facts from server"""
        try:
            print(f"{Fore.CYAN}🤖 Requesting GitLab facts for user: {dev}")

            async with self.session.get(
                    f"{SERVER_URL}/facts/{dev}?hours=24",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"{Fore.GREEN}✅ Facts data received")
                    return result
                else:
                    print(f"{Fore.RED}❌ Server error: {response.status}")
                    return None
        except Exception as e:
            print(f"{Fore.RED}❌ Error retrieving facts: {e}")
            return None

    async def send_to_server(self, message_data):
        """Отправляем данные на сервер (только для сохранения)"""
        try:
            print(f"{Fore.CYAN}📤 Отправка сообщения на сервер для сохранения...")

            async with self.session.post(
                    f"{SERVER_URL}/save_message",
                    json=message_data,
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"{Fore.GREEN}✅ Сообщение сохранено на сервере")
                    return result
                else:
                    print(f"{Fore.RED}❌ Ошибка сервера: {response.status}")
                    self.error_count += 1
                    return None
        except Exception as e:
            print(f"{Fore.RED}❌ Ошибка отправки на сервер: {e}")
            self.error_count += 1
            return None

    async def process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатываем сообщение (только сохраняем, не отвечаем)"""
        message = update.message
        if not message:
            return

        self.message_count += 1

        # Логируем полученное сообщение
        user = message.from_user
        chat = message.chat

        print(f"\n{Fore.YELLOW}📨 Новое сообщение #{self.message_count}")
        print(f"👤 От: {user.first_name} (@{user.username or 'без username'})")
        print(f"💬 Чат: {chat.title or 'Приватный чат'} (ID: {chat.id})")
        print(f"📝 Текст: {message.text or '[Не текстовое сообщение]'}")
        print(f"⏰ Время: {message.date}")

        # Собираем данные о сообщении
        message_data = {
            "message_id": message.message_id,
            "timestamp": message.date.isoformat(),
            "chat": {
                "id": chat.id,
                "title": chat.title or "Private chat",
                "type": chat.type
            },
            "user": {
                "id": user.id,
                "username": user.username or "No username",
                "first_name": user.first_name,
                "last_name": user.last_name
            },
            "content": message.text or "[Non-text message]",
            "message_type": "text" if message.text else "media"
        }

        # Отправляем на сервер только для сохранения
        await self.send_to_server(message_data)
        print(f"{Fore.BLUE}ℹ️ Сообщение сохранено. Автоматические ответы отключены.")



    async def get_facts_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /get_facts_for_user command"""
        if len(context.args) < 1:
            await update.message.reply_text("❌ Please specify a username. Usage: /get_facts_for_user username")
            return

        username = context.args[0]
        await update.message.reply_text(f"🔍 Getting GitLab facts for {username}...")

        try:
            facts_data = await self.get_gitlab_summary_from_server(username)

            if facts_data and facts_data.get('status') == 'success':
                formatted_response = self.format_facts_response(facts_data['facts'])
                await update.message.reply_text(formatted_response)
            else:
                await update.message.reply_text("❌ Could not retrieve facts for this user")

        except Exception as e:
            print(f"{Fore.RED}❌ Error in get_facts_command: {e}")
            await update.message.reply_text("❌ Error retrieving facts")

    def format_facts_response(self, facts: dict) -> str:
        response = f"📊 Активность в гитлабе для разработчика {facts['username']}\n"
        response += f"⏰ За последние {facts['period_hours']} часов\n\n"
        response += f"📈 Все события: {facts['total_events']}\n\n"

        if facts['event_summary']:
            response += "📋 Детали:\n"
            for event_type, count in facts['event_summary'].items():
                response += f"  • {event_type}: {count}\n"

        if facts.get('activities'):
            response += "\n🔄 Последние действия:\n"
            for activity in facts['activities'][:10]:  # Show last 10 activities
                response += f"  • {activity['description']} ({activity['timestamp']})\n"

        # Commit details
        if facts.get('commits'):
            response += f"\n💾 Commits ({len(facts['commits'])}):\n"
            for commit in facts['commits'][:10]:  # Show up to 10 commits
                response += f"  • {commit['message']} ({commit['id']})\n"

        # Merge Request details
        if facts.get('merge_requests'):
            response += f"\n🔀 Merge Requests ({len(facts['merge_requests'])}):\n"
            for mr in facts['merge_requests']:
                response += f"  • {mr['title']} ({mr['state']}) - {mr.get('url', 'No URL')}\n"

        # Issue details
        if facts.get('issues'):
            response += f"\n🐛 Issues ({len(facts['issues'])}):\n"
            for issue in facts['issues']:
                response += f"  • {issue['title']} ({issue['state']}) - {issue.get('url', 'No URL')}\n"

        # Repository information
        if facts['repositories']:
            response += f"\n🏷 Репозитории: {', '.join(facts['repositories'][:5])}"
            if len(facts['repositories']) > 5:
                response += f" and {len(facts['repositories']) - 5} more..."

        if facts['last_activity']:
            response += f"\n\n⏰ Last Activity: {facts['last_activity']}"

        return response

    async def check_timesheet_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Проверка таймшитов пользователя"""
        try:
            # Получаем аргументы команды
            if not context.args:
                await update.message.reply_text("❌ Укажите username: /check_timesheet username")
                return

            username = context.args[0]
            days_back = int(context.args[1]) if len(context.args) > 1 else 7

            await update.message.reply_text(f"🔍 Проверяю таймшит для {username}...")

            # Отправляем запрос на сервер
            async with self.session.get(
                    f"{SERVER_URL}/timesheet/check/{username}?days_back={days_back}",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:

                if response.status == 200:
                    data = await response.json()

                    if data['missing_entries_count'] > 0:
                        message = f"⚠️ Найдено {data['missing_entries_count']} пропущенных записей:\n\n"
                        for entry in data['missing_entries']:
                            message += f"📅 {entry['date']} - {entry['gitlab_events_count']} событий в GitLab\n"
                    else:
                        message = "✅ Все записи в таймшите заполнены корректно"

                    await update.message.reply_text(message)

                else:
                    await update.message.reply_text("❌ Ошибка при проверке таймшита")

        except Exception as e:
            print(f"{Fore.RED}❌ Ошибка при проверке таймшита: {e}")
            await update.message.reply_text("❌ Произошла ошибка при проверке")




    @staticmethod
    async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

        if update.message.chat.type == "private":
            await update.message.reply_text("🏓 Pong! Бот активен")
        else:
            await update.message.reply_text("ℹ️ Эта команда работает только в личных сообщениях с ботом")

    async def scheduled_timesheet_check(self, context: ContextTypes.DEFAULT_TYPE):
        """Scheduled job to check all devs' timesheets and nag if needed."""
        today = datetime.now().weekday()  # 0=Monday, 6=Sunday
        if today >= 5:  # Skip Sat (5) and Sun (6)
            print(f"{Fore.YELLOW}🛌 Weekend vibes—skipping timesheet nag.")
            return

        print(f"{Fore.MAGENTA}📊 Kicking off scheduled timesheet check...")

        # Load group chat ID
        try:
            with open('group_config.json', 'r') as f:
                config = json.load(f)
            group_chat_id = config.get('group_chat_id')
            if not group_chat_id:
                print(f"{Fore.RED}❌ No group chat ID found in config")
                return
        except FileNotFoundError:
            print(f"{Fore.RED}❌ group_config.json not found")
            return

        from config import DEVELOPER_SHEETS, TIMESHEET_CHECK_DAYS_BACK
        devs = list(DEVELOPER_SHEETS.keys())

        for dev in devs:
            try:
                # Use the server endpoint to check timesheet
                async with self.session.get(
                        f"{SERVER_URL}/timesheet/check/{dev}?days_back={TIMESHEET_CHECK_DAYS_BACK}",
                        timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data['missing_entries_count'] > 0:
                            # Send the reminder message from server
                            reminder_message = data.get('reminder_message',
                                                        f"⚠️ @{dev}, у вас пропущены записи в таблице!")

                            # Send to group chat
                            await context.bot.send_message(
                                chat_id=group_chat_id,
                                text=reminder_message
                            )
                            print(f"{Fore.GREEN}✅ Nagged {dev} in group chat.")
                        else:
                            print(f"{Fore.GREEN}✅ {dev}'s timesheet is clean— no spam.")
                    else:
                        print(f"{Fore.RED}❌ Server error for {dev}: {response.status}")
            except Exception as e:
                print(f"{Fore.RED}❌ Failed to check {dev}: {e}")

    async def chat_member_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle bot being added to a group"""
        chat_member = update.chat_member
        chat_id = update.effective_chat.id

        # Check if bot was added to the group
        if chat_member.new_chat_member.status == 'member':
            # Save the chat ID to a config file or database
            await self.save_group_chat_id(chat_id)
            await update.effective_chat.send_message(
                "🤖 Bot added to group! I'll send timesheet notifications here."
            )

    async def save_group_chat_id(self, chat_id):
        """Save group chat ID to a JSON file"""
        config = {}
        try:
            with open('group_config.json', 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            pass

        config['group_chat_id'] = chat_id

        with open('group_config.json', 'w') as f:
            json.dump(config, f)

        print(f"{Fore.GREEN}✅ Saved group chat ID: {chat_id}")


    async def get_gitlab_summary_for_date(self, username: str, date: str):
        """Get GitLab activity for a specific date"""
        try:
            # We'll get 48 hours of data to ensure we capture the specific date
            async with self.session.get(
                    f"{SERVER_URL}/facts/{username}?hours=48",
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('status') == 'success':
                        # Filter activities for the specific date
                        daily_activities = [
                            activity for activity in data['facts']['activities']
                            if activity['timestamp'].startswith(date)
                        ]
                        return {
                            'total_events': len(daily_activities),
                            'activities': daily_activities,
                            'event_summary': self._summarize_events(daily_activities)
                        }
            return None
        except Exception as e:
            print(f"{Fore.RED}❌ Error getting GitLab data for {date}: {e}")
            return None

    async def daily_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /daily command for submitting daily reports"""
        try:
            if not context.args:
                await update.message.reply_text(
                    "Пожалуйста, предоставьте ваш ежедневный отчет после команды /daily\n\n"
                    "Пример: /daily Worked on user auth, fixed login bug, reviewed 2 PRs"
                )
                return

            user = update.effective_user
            today = datetime.now().strftime("%Y-%m-%d")
            report_content = " ".join(context.args)

            # Send to server for processing
            report_data = {
                "username": user.username or user.first_name,
                "date": today,
                "content": report_content,
                "message_id": update.message.message_id
            }

            async with self.session.post(
                    f"{SERVER_URL}/daily/submit",
                    json=report_data,
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    await update.message.reply_text("Daily report submitted successfully!")
                    print(f"{Fore.GREEN}Daily report saved for {user.username}")
                else:
                    print(response.status)
                    await update.message.reply_text("Error saving daily report")

        except Exception as e:
            print(f"{Fore.RED}Error in daily command: {e}")
            await update.message.reply_text("Error processing daily report")

    async def send_daily_reminder(dev: str, date: str):
        """Send private reminder to user about missing daily report"""
        try:
            # First try to get Telegram ID from user_mapping table
            telegram_id = db_manager.get_telegram_id(dev)

            if not telegram_id:
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
                else:
                    print(f"{Fore.YELLOW}⚠️ Could not find Telegram ID for {dev}")
                    return

            if bot_manager and bot_manager.bot_instance:
                formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")

                reminder_message = f"""
    🔔 Daily Report Reminder!

    Hi! We noticed you haven't submitted your daily report for {formatted_date}.

    Please send your report using the /daily command:
    /daily Your report content here
    """

                await bot_manager.send_message_to_chat(telegram_id, reminder_message)
                print(f"{Fore.GREEN}📨 Sent private reminder to {dev} ({telegram_id})")
            else:
                print(f"{Fore.YELLOW}⚠️ Could not send reminder to {dev} - bot not available")

        except Exception as e:
            print(f"{Fore.RED}❌ Error sending reminder to {dev}: {e}")

    def _summarize_events(self, activities):
        """Summarize events by type"""
        summary = {}
        for activity in activities:
            event_type = activity['type']
            summary[event_type] = summary.get(event_type, 0) + 1
        return summary

    def format_morning_digest(self, username: str, date: str, gitlab_data: dict, timesheet_data: dict) -> str:
        """Format the morning digest message"""
        try:
            # Format date nicely
            formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")

            response = f"🌅 Morning Digest for @{username}\n"
            response += f"📅 {formatted_date}\n\n"

            # GitLab section
            response += "🚀 GitLab Activity:\n"
            if gitlab_data and gitlab_data['total_events'] > 0:
                response += f"   • Total events: {gitlab_data['total_events']}\n"
                for event_type, count in gitlab_data['event_summary'].items():
                    response += f"   • {event_type}: {count}\n"

                # Show a few sample activities
                response += "\n   Recent activities:\n"
                for activity in gitlab_data['activities'][:3]:  # Show first 3 activities
                    response += f"   • {activity['description']}\n"
            else:
                response += "   • No GitLab activity recorded\n"

            # Timesheet section
            response += "\n⏰ Timesheet Entry:\n"
            if timesheet_data and timesheet_data.get('has_entry', False):
                entry = timesheet_data.get('entry', {})
                response += f"   • Hours: {entry.get('hours', 0)}\n"
                response += f"   • Description: {entry.get('description', 'No description')}\n"
            else:
                response += "   • No timesheet entry found\n"

            return response

        except Exception as e:
            print(f"{Fore.RED}❌ Error formatting morning digest: {e}")
            return "❌ Error formatting morning digest"

    def run(self):
        """Launch the bot – may the odds be ever in your favor."""
        print(f"{Fore.CYAN}🚀 Bot launching... Hold onto your butts")
        print(f"🔡 Server: {SERVER_URL}")
        print(f"🔑 Token: {BOT_TOKEN[:10]}... (hope it's not leaked)")
        print(f"{Fore.YELLOW}⚠️ Auto responses off – manual labor only")
        application = Application.builder().token(BOT_TOKEN).build()

        self.bot_instance = application.bot

        # Handlers – add more if you dare
        application.add_handler(CommandHandler("ping", self.ping_command))
        application.add_handler(CommandHandler("health", self.health_command))
        application.add_handler(CommandHandler("get_actions", self.get_facts_command))
        application.add_handler(CommandHandler("timesheet", self.check_timesheet_command))
        application.add_handler(CommandHandler("digest", self.manual_digest_command))
        application.add_handler(CommandHandler("configure_digest", self.configure_digest_command))
        application.add_handler(CommandHandler("list_digests", self.list_digests_command))
        application.add_handler(CommandHandler("morning_digest", self.morning_digest_command))
        application.add_handler(ChatMemberHandler(self.chat_member_handler, ChatMemberHandler.CHAT_MEMBER))
        application.add_handler(CommandHandler("daily", self.daily_command))
        db_manager.add_user_mapping("root", 444086551)
        telegram_id = db_manager.get_telegram_id("root")
        print(f"{Fore.GREEN}✅ Retrieved mapping: root -> {telegram_id}")
        application.add_handler(
            MessageHandler(
                filters.TEXT,
                self.process_message
            )
        )
        # Session and registration – don't skip or bot goes boom
        application.job_queue.run_once(lambda _: asyncio.create_task(self.start_session()), 0)
        application.job_queue.run_once(lambda _: asyncio.create_task(self.register_bot_with_server()), 5)

        # Scheduled checks – because devs need nagging
        from datetime import time
        from config import TIMESHEET_CHECK_TIME

        hour, minute = map(int, TIMESHEET_CHECK_TIME.split(':'))
        check_time = time(hour=hour, minute=minute)

        application.job_queue.run_daily(
            self.scheduled_timesheet_check,
            time=check_time,
            days=(0, 1, 2, 3, 4)  # Weekdays only, weekends are for regrets
        )

        print(f"{Fore.GREEN}✅ Timesheet nags scheduled at {TIMESHEET_CHECK_TIME} (weekdays – party's over)")
        print(
            f"{Fore.GREEN}🤖 Bot alive. Commands: /digest, /configure_digest, /list_digests, /morning_digest, /timesheet, /health, /ping")

        try:
            application.run_polling()
        except KeyboardInterrupt:
            print(f"{Fore.RED}⛔ Bot killed. Hope it wasn't mid-digest")
        finally:
            asyncio.run(self.close_session())


if __name__ == "__main__":
    bot = TelegramBotMonitor()
    bot.run()