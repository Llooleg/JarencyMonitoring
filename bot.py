import logging
import asyncio
import aiohttp
import json
import time
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from colorama import Fore, Style, init
from config import SERVER_URL, BOT_TOKEN

init(autoreset=True)

# Настройка логирования
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
        #self.summary_attempts = {}  # Track summary attempts per chat
        self.MAX_DAILY_ATTEMPTS = 5  # Max attempts per day per chat
        #self.summary_limiter = RateLimiter(max_calls=3, period=120)

    async def start_session(self):
        """Создаем HTTP сессию""""""Создаем HTTP сессию"""
        import ssl

        # Create SSL context that accepts self-signed certificates
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # Use timeout configuration
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
        )

        print(f"{Fore.GREEN}✅ HTTP сессия создана (самоподписанные сертификаты разрешены)")

        # Проверяем доступность сервера
        await self.check_server_health()

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




    @staticmethod
    async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

        if update.message.chat.type == "private":
            await update.message.reply_text("🏓 Pong! Бот активен")
        else:
            await update.message.reply_text("ℹ️ Эта команда работает только в личных сообщениях с ботом")

    def run(self):
        """Запуск бота"""
        print(f"{Fore.CYAN}🚀 Запуск Telegram бота ...")
        print(f"📡 Сервер: {SERVER_URL}")
        print(f"🔑 Токен: {BOT_TOKEN[:10]}...")
        print(f"{Fore.YELLOW}⚠️ Автоматические ответы ОТКЛЮЧЕНЫ")


        application = Application.builder().token(BOT_TOKEN).build()

        # error handler FIRST
        #application.add_error_handler(self.error_handler)
        application.add_handler(CommandHandler("ping", self.ping_command ))


        application.add_handler(CommandHandler("health", self.health_command))

        application.add_handler(CommandHandler("get_actions", self.get_facts_command))
        #application.add_handler(CommandHandler("get_commits", self.get_detailed_commits_command))
        # Set up session
        application.job_queue.run_once(lambda _: asyncio.create_task(self.start_session()), 0)

        print(f"{Fore.GREEN}🤖 Бот запущен и ожидает сообщения...")
        print(f"{Fore.YELLOW}💡 Доступные команды: /summary, /prompt, /schedule, /stats, /health, /help")

        try:
            application.run_polling()
        except KeyboardInterrupt:
            print(f"{Fore.RED}⛔ Остановка бота...")
        finally:
            asyncio.run(self.close_session())


if __name__ == "__main__":
    bot = TelegramBotMonitor()
    bot.run()