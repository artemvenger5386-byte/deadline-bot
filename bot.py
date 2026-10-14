import asyncio
import os
import threading
import asyncpg
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from flask import Flask

load_dotenv()

# 🔒 ТВОЙ АДМИН ID
ADMIN_ID = 1752124938

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()
db_pool = None

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
            print("✅ Подключено к Supabase")
        except Exception as e:
            print(f"⚠️ Ошибка подключения к БД: {e}")
    return db_pool

async def init_db():
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS deadlines (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    subject TEXT,
                    deadline_date TEXT,
                    is_done BOOLEAN DEFAULT FALSE,
                    last_reminder TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_name TEXT,
                    action TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            print("✅ Таблицы готовы")

async def log_action(chat_id: int, user_name: str, action: str, details: str = ""):
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO activity_logs (chat_id, user_name, action, details) VALUES ($1, $2, $3, $4)",
                chat_id, user_name, action, details
            )

# --- ОБЫЧНЫЕ КОМАНДЫ ---
@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(f"Привет, {message.from_user.first_name}! 👋\nЯ бот для управления дедлайнами.\nИспользуй /help, чтобы узнать команды.")
    await log_action(message.chat.id, message.from_user.username or "anon", "start")

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer("📖 Команды:\n/start — начать\n/help — справка\n/add <предмет> <ДД.ММ.ГГГГ> — добавить дедлайн\n/list — список дедлайнов\n/done <ID> — отметить выполненным\n/delete <ID> — удалить дедлайн\n/check — проверить напоминания")

@dp.message(Command('add'))
async def cmd_add(message: types.Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer(" Формат: /add <предмет> <ДД.ММ.ГГГГ>\nПример: /add Математика 15.10.2026")
        return
    subject, date_str = args[1], args[2]
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используй ДД.ММ.ГГГГ")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO deadlines (chat_id, subject, deadline_date) VALUES ($1, $2, $3)", message.chat.id, subject, date_str)
    await message.answer(f"✅ Дедлайн добавлен: {subject} на {date_str}")
    await log_action(message.chat.id, message.from_user.username or "anon", "add", f"{subject} {date_str}")

@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, subject, deadline_date, is_done FROM deadlines WHERE chat_id = $1 ORDER BY deadline_date", message.chat.id)
    if not rows:
        await message.answer("📭 Нет активных дедлайнов.")
        return
    text = "📋 Твои дедлайны:\n"
    for row in rows:
        status = "✅" if row['is_done'] else "⏳"
        text += f"{row['id']}. {status} {row['subject']} — {row['deadline_date']}\n"
    text += "\nИспользуй /done <ID> или /delete <ID>"
    await message.answer(text)

@dp.message(Command('done'))
async def cmd_done(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Формат: /done <ID>\nПример: /done 5")
        return
    try:
        deadline_id = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE deadlines SET is_done = TRUE WHERE id = $1 AND chat_id = $2", deadline_id, message.chat.id)
    if result == "UPDATE 0":
        await message.answer("❌ Дедлайн не найден")
    else:
        await message.answer(f"✅ Дедлайн #{deadline_id} отмечен как выполненный!")

@dp.message(Command('delete'))
async def cmd_delete(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Формат: /delete <ID>\nПример: /delete 5")
        return
    try:
        deadline_id = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM deadlines WHERE id = $1 AND chat_id = $2", deadline_id, message.chat.id)
    if result == "DELETE 0":
        await message.answer("❌ Дедлайн не найден")
    else:
        await message.answer(f"🗑 Дедлайн #{deadline_id} удалён!")

@dp.message(Command('check'))
async def cmd_check(message: types.Message):
    await message.answer(" Проверяю дедлайны...")
    await send_reminders()
    await message.answer("✅ Проверка завершена!")

async def send_reminders():
    pool = await get_db_pool()
    if not pool:
        return
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, chat_id, subject, deadline_date, last_reminder FROM deadlines WHERE is_done = FALSE")
            for row in rows:
                try:
                    deadline_date = datetime.strptime(row['deadline_date'], "%d.%m.%Y").date()
                except ValueError:
                    continue
                reminder_type = None
                reminder_text = None
                if deadline_date == today:
                    reminder_type = "today"
                    reminder_text = f"🔔 СЕГОДНЯ дедлайн: {row['subject']} ({row['deadline_date']})"
                elif deadline_date == tomorrow:
                    reminder_type = "tomorrow"
                    reminder_text = f"⏰ ЗАВТРА дедлайн: {row['subject']} ({row['deadline_date']})"
                elif deadline_date == day_after:
                    reminder_type = "day_after"
                    reminder_text = f"📅 Послезавтра дедлайн: {row['subject']} ({row['deadline_date']})"
                if reminder_type and row['last_reminder'] != reminder_type:
                    try:
                        await bot.send_message(row['chat_id'], reminder_text)
                        await conn.execute("UPDATE deadlines SET last_reminder = $1 WHERE id = $2", reminder_type, row['id'])
                    except Exception as e:
                        print(f"Ошибка отправки: {e}")
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

# --- АДМИН-КОМАНДЫ (ТОЛЬКО ДЛЯ ТЕБЯ!) ---
@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён! Эта команда только для администратора.")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна")
        return
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(DISTINCT chat_id) FROM deadlines")
        total_deadlines = await conn.fetchval("SELECT COUNT(*) FROM deadlines")
        active = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE is_done = FALSE")
    text = f"📊 <b>Админ-панель</b>\n\n👥 Пользователей: <b>{total_users}</b>\n📋 Всего дедлайнов: <b>{total_deadlines}</b>\n⏳ Активных: <b>{active}</b>\n\n<b>Команды:</b>\n/users — список пользователей\n/logs — последние действия\n/broadcast <текст> — рассылка"
    await message.answer(text, parse_mode="HTML")
    await log_action(message.chat.id, "admin", "admin_panel")

@dp.message(Command('users'))
async def cmd_users(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, COUNT(*) as count FROM deadlines GROUP BY chat_id ORDER BY count DESC")
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    text = f" <b>Пользователи ({len(rows)}):</b>\n\n"
    for row in rows:
        text += f"• ID: <code>{row['chat_id']}</code> — {row['count']} дедлайнов\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('logs'))
async def cmd_logs(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_name, action, details, created_at FROM activity_logs ORDER BY created_at DESC LIMIT 15")
    if not rows:
        await message.answer("Логи пусты")
        return
    text = "📋 <b>Последние действия:</b>\n\n"
    for row in rows:
        time_str = row['created_at'].strftime("%d.%m %H:%M")
        text += f"[{time_str}] <b>{row['user_name'] or 'anon'}</b>: {row['action']}"
        if row['details']: text += f" — {row['details']}"
        text += "\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('broadcast'))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    text_to_send = message.text.replace('/broadcast', '').strip()
    if not text_to_send:
        await message.answer("Формат: /broadcast <текст сообщения>")
        return
    pool = await get_db_pool()
    if not pool: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT chat_id FROM deadlines")
    sent, failed = 0, 0
    for row in rows:
        try:
            await bot.send_message(row['chat_id'], f"📢 <b>Объявление:</b>\n\n{text_to_send}", parse_mode="HTML")
            sent += 1
        except:
            failed += 1
    await message.answer(f"✅ Рассылка завершена!\nОтправлено: {sent}\nОшибок: {failed}")
    await log_action(message.chat.id, "admin", "broadcast", text_to_send[:30])

# Flask HTTP-сервер
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running! Reminders checked."

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

async def main():
    try:
        await init_db()
    except Exception as e:
        print(f"⚠️ Ошибка init_db: {e}")
    print("🚀 Запускаю Telegram-бота...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 HTTP-сервер запущен")
    asyncio.run(main())
