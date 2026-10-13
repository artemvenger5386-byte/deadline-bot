import asyncio
import os
import asyncpg
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
    return db_pool

async def init_db():
    pool = await get_db_pool()
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
    print("✅ Таблица deadlines готова в Supabase")

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(f"Привет, {message.from_user.first_name}! 👋\n\nЯ бот для управления дедлайнами.\nИспользуй /help, чтобы узнать команды.")

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(" Команды:\n/start — начать\n/help — справка\n/add <предмет> <ДД.ММ.ГГГГ> — добавить дедлайн\n/list — список дедлайнов\n/done <ID> — отметить выполненным\n/delete <ID> — удалить дедлайн\n/check — проверить напоминания")

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
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO deadlines (chat_id, subject, deadline_date) VALUES ($1, $2, $3)", message.chat.id, subject, date_str)
    
    await message.answer(f"✅ Дедлайн добавлен: {subject} на {date_str}")

@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, subject, deadline_date, is_done FROM deadlines WHERE chat_id = $1 ORDER BY deadline_date", message.chat.id)
    
    if not rows:
        await message.answer("📭 Нет активных дедлайнов.")
        return
    
    text = "📋 Твои дедлайны:\n\n"
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
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE deadlines SET is_done = TRUE WHERE id = $1 AND chat_id = $2", deadline_id, message.chat.id)
    
    if result == "UPDATE 0":
        await message.answer(" Дедлайн не найден")
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
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM deadlines WHERE id = $1 AND chat_id = $2", deadline_id, message.chat.id)
    
    if result == "DELETE 0":
        await message.answer("❌ Дедлайн не найден")
    else:
        await message.answer(f"🗑 Дедлайн #{deadline_id} удалён!")

@dp.message(Command('check'))
async def cmd_check(message: types.Message):
    await message.answer("🔍 Проверяю дедлайны...")
    await send_reminders()
    await message.answer("✅ Проверка завершена!")

async def send_reminders():
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    
    pool = await get_db_pool()
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

async def handle(request):
    try:
        await send_reminders()
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")
    return web.Response(text="Bot is running! Reminders checked.")

async def start_http_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f" HTTP server started on port {port}")

async def main():
    try:
        await init_db()
    except Exception as e:
        print(f"⚠️ Ошибка инициализации БД: {e}")
    print("🚀 Запускаю бота...")
    await asyncio.gather(dp.start_polling(bot), start_http_server())

if __name__ == '__main__':
    asyncio.run(main())
