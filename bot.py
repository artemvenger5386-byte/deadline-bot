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

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()
db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
            print("✅ Подключено к Supabase")
        except Exception as e:
            print(f"️ Ошибка подключения к БД: {e}")
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
                CREATE TABLE IF NOT EXISTS user_stats (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    total_deadlines INTEGER DEFAULT 0,
                    completed_deadlines INTEGER DEFAULT 0,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            print("✅ Таблицы готовы")

async def update_user_stats(chat_id: int, username: str, full_name: str, completed: bool = False):
    """Обновляет статистику пользователя"""
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            if completed:
                await conn.execute('''
                    INSERT INTO user_stats (chat_id, username, full_name, total_deadlines, completed_deadlines, xp)
                    VALUES ($1, $2, $3, 1, 1, 50)
                    ON CONFLICT (chat_id) DO UPDATE SET
                        total_deadlines = user_stats.total_deadlines + 1,
                        completed_deadlines = user_stats.completed_deadlines + 1,
                        xp = user_stats.xp + 50,
                        level = (user_stats.xp + 50) / 100 + 1
                ''', chat_id, username, full_name)
            else:
                await conn.execute('''
                    INSERT INTO user_stats (chat_id, username, full_name, total_deadlines)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (chat_id) DO UPDATE SET
                        total_deadlines = user_stats.total_deadlines + 1,
                        username = $2,
                        full_name = $3
                ''', chat_id, username, full_name)

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        "Я бот для управления дедлайнами.\n"
        "Используй /help, чтобы узнать команды."
    )
    # Обновляем статистику пользователя
    await update_user_stats(
        message.chat.id,
        message.from_user.username or "anon",
        message.from_user.first_name
    )

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 Команды:\n"
        "/start — начать\n"
        "/help — справка\n"
        "/add <предмет> <ДД.ММ.ГГГГ> — добавить дедлайн\n"
        "/list — список дедлайнов\n"
        "/done <ID> — отметить выполненным\n"
        "/delete <ID> — удалить дедлайн\n"
        "/check — проверить напоминания\n"
        "/rating — топ пользователей\n"
        "/profile — твоя статистика"
    )

@dp.message(Command('add'))
async def cmd_add(message: types.Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Формат: /add <предмет> <ДД.ММ.ГГГГ>\nПример: /add Математика 15.10.2026")
        return
    subject = args[1]
    date_str = args[2]
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
        await conn.execute(
            "INSERT INTO deadlines (chat_id, subject, deadline_date) VALUES ($1, $2, $3)",
            message.chat.id, subject, date_str
        )
    await message.answer(f"✅ Дедлайн добавлен: {subject} на {date_str}")

@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, subject, deadline_date, is_done FROM deadlines WHERE chat_id = $1 ORDER BY deadline_date",
            message.chat.id
        )
    if not rows:
        await message.answer("📭 Нет активных дедлайнов.")
        return
    text = " Твои дедлайны:\n"
    for row in rows:
        status = "✅" if row['is_done'] else ""
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
        result = await conn.execute(
            "UPDATE deadlines SET is_done = TRUE WHERE id = $1 AND chat_id = $2",
            deadline_id, message.chat.id
        )
        if result == "UPDATE 0":
            await message.answer("❌ Дедлайн не найден")
            return
        # Обновляем статистику
        await update_user_stats(
            message.chat.id,
            message.from_user.username or "anon",
            message.from_user.first_name,
            completed=True
        )
    await message.answer(f"✅ Дедлайн #{deadline_id} отмечен как выполненный! +50 XP")

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
        result = await conn.execute(
            "DELETE FROM deadlines WHERE id = $1 AND chat_id = $2",
            deadline_id, message.chat.id
        )
    if result == "DELETE 0":
        await message.answer("❌ Дедлайн не найден")
    else:
        await message.answer(f"🗑 Дедлайн #{deadline_id} удалён!")

@dp.message(Command('check'))
async def cmd_check(message: types.Message):
    await message.answer("🔍 Проверяю дедлайны...")
    await send_reminders()
    await message.answer("✅ Проверка завершена!")

@dp.message(Command('rating'))
async def cmd_rating(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username, full_name, xp, level, completed_deadlines FROM user_stats ORDER BY xp DESC LIMIT 10"
        )
    
    if not rows:
        await message.answer("📊 Рейтинг пока пуст. Будь первым!")
        return
    
    text = " ТОП-10 ПОЛЬЗОВАТЕЛЕЙ\n\n"
    medals = ["🥇", "🥈", ""]
    
    for i, row in enumerate(rows):
        if i < 3:
            medal = medals[i]
        else:
            medal = f"{i+1}."
        
        name = row['full_name'] or (row['username'] if row['username'] != "anon" else "Аноним")
        text += f"{medal} <b>{name}</b>\n"
        text += f"   ⭐ {row['xp']} XP | Уровень {row['level']} | ✅ {row['completed_deadlines']}\n\n"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_stats WHERE chat_id = $1",
            message.chat.id
        )
    
    if not row:
        await message.answer("📊 Твоя статистика пока пуста. Начни добавлять дедлайны!")
        return
    
    # Процент выполнения
    completion_rate = 0
    if row['total_deadlines'] > 0:
        completion_rate = int((row['completed_deadlines'] / row['total_deadlines']) * 100)
    
    # Прогресс до следующего уровня
    xp_for_next_level = row['level'] * 100
    xp_progress = row['xp'] % 100
    
    text = f"👤 <b>ТВОЙ ПРОФИЛЬ</b>\n\n"
    text += f"👤 Имя: {row['full_name'] or 'Аноним'}\n"
    text += f"⭐ XP: {row['xp']}\n"
    text += f" Уровень: {row['level']}\n"
    text += f"📊 Прогресс: {xp_progress}/100 XP\n"
    text += f" Всего дедлайнов: {row['total_deadlines']}\n"
    text += f"✅ Выполнено: {row['completed_deadlines']}\n"
    text += f" Процент выполнения: {completion_rate}%\n"
    
    await message.answer(text, parse_mode="HTML")

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
            rows = await conn.fetch(
                "SELECT id, chat_id, subject, deadline_date, last_reminder FROM deadlines WHERE is_done = FALSE"
            )
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
                    reminder_text = f" Послезавтра дедлайн: {row['subject']} ({row['deadline_date']})"
                if reminder_type and row['last_reminder'] != reminder_type:
                    try:
                        await bot.send_message(row['chat_id'], reminder_text)
                        await conn.execute(
                            "UPDATE deadlines SET last_reminder = $1 WHERE id = $2",
                            reminder_type, row['id']
                        )
                    except Exception as e:
                        print(f"Ошибка отправки: {e}")
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

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
