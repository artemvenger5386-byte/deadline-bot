import asyncio
import os
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from aiohttp import web

# Загружаем токен из файла .env
load_dotenv()

# Создаём бота
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

# Функция для создания базы данных
async def init_db():
    async with aiosqlite.connect('deadlines.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                subject TEXT,
                deadline_date TEXT,
                created_at TEXT
            )
        ''')
        await db.commit()

# Команда /start
@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я бот для управления дедлайнами.\n"
        "Используй команду /help, чтобы узнать, что я умею."
    )

# Команда /help
@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 Доступные команды:\n"
        "/start — начать работу\n"
        "/help — справка\n"
        "/add <предмет> <ДД.ММ.ГГГГ> — добавить дедлайн\n"
        "/list — показать все дедлайны"
    )

# Команда /add
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
    
    async with aiosqlite.connect('deadlines.db') as db:
        await db.execute(
            "INSERT INTO deadlines (chat_id, subject, deadline_date, created_at) VALUES (?, ?, ?, ?)",
            (message.chat.id, subject, date_str, datetime.now().isoformat())
        )
        await db.commit()
    
    await message.answer(f"✅ Дедлайн добавлен: {subject} на {date_str}")

# Команда /list
@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    async with aiosqlite.connect('deadlines.db') as db:
        cursor = await db.execute(
            "SELECT subject, deadline_date FROM deadlines WHERE chat_id = ?",
            (message.chat.id,)
        )
        deadlines = await cursor.fetchall()
    
    if not deadlines:
        await message.answer("📭 Нет активных дедлайнов.")
        return
    
    text = "📋 Активные дедлайны:\n\n"
    for subj, date in deadlines:
        text += f"• {subj} — {date}\n"
    
    await message.answer(text)

# HTTP-сервер для Render (чтобы Render видел открытый порт)
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_http_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 HTTP server started on port {port}")

# Главная функция
async def main():
    await init_db()
    print(" База данных создана")
    print(" Запускаю бота...")
    
    # Запускаем HTTP-сервер и бота параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        start_http_server()
    )

if __name__ == '__main__':
    asyncio.run(main())
