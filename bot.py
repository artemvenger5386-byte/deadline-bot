import asyncio
import os
import threading
import asyncpg
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from flask import Flask

load_dotenv()

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
                CREATE TABLE IF NOT EXISTS users (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
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
            print("✅ Все таблицы готовы")

async def ensure_user(chat_id: int, username: str, full_name: str):
    """Создаёт пользователя, если его нет"""
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (chat_id, username, full_name) VALUES ($1, $2, $3) ON CONFLICT (chat_id) DO UPDATE SET username = $2, full_name = $3",
                chat_id, username, full_name
            )

async def add_xp(chat_id: int, amount: int):
    """Добавляет XP и обновляет уровень"""
    pool = await get_db_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT xp, level FROM users WHERE chat_id = $1", chat_id)
        if not row:
            return None
        new_xp = row['xp'] + amount
        new_level = (new_xp // 100) + 1
        await conn.execute(
            "UPDATE users SET xp = $1, level = $2 WHERE chat_id = $3",
            new_xp, new_level, chat_id
        )
        if new_level > row['level']:
            return new_level
    return None

async def log_action(chat_id: int, user_name: str, action: str, details: str = ""):
    pool = await get_db_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO activity_logs (chat_id, user_name, action, details) VALUES ($1, $2, $3, $4)",
                chat_id, user_name, action, details
            )

# --- КЛАВИАТУРЫ ---

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить дедлайн")],
            [KeyboardButton(text="📋 Мои дедлайны")],
            [KeyboardButton(text="✅ Выполнить"), KeyboardButton(text="🗑 Удалить")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🏆 Рейтинг")],
            [KeyboardButton(text="🔍 Проверить напоминания")],
            [KeyboardButton(text=" Справка")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие"
    )
    return keyboard

def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="👥 Пользователи")],
            [KeyboardButton(text="📋 Логи")],
            [KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="◀️ Главное меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-панель"
    )
    return keyboard

# --- ОБРАБОТЧИКИ КНОПОК ---

@dp.message(lambda message: message.text == "➕ Добавить дедлайн")
async def btn_add(message: types.Message):
    await message.answer(
        " <b>Добавить дедлайн</b>\n\n"
        "Формат: <code>/add Предмет ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>/add Математика 15.10.2026</code>\n\n"
        "🎁 +10 XP за каждый дедлайн!",
        parse_mode="HTML"
    )

@dp.message(lambda message: message.text == "📋 Мои дедлайны")
async def btn_list(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, subject, deadline_date, is_done FROM deadlines WHERE chat_id = $1 ORDER BY deadline_date",
            message.chat.id
        )
    if not rows:
        await message.answer("📭 Нет активных дедлайнов.")
        return
    text = " <b>Твои дедлайны:</b>\n\n"
    for row in rows:
        status = "✅" if row['is_done'] else "⏳"
        text += f"{row['id']}. {status} {row['subject']} — {row['deadline_date']}\n"
    text += "\nИспользуй /done <ID> или /delete <ID>"
    await message.answer(text, parse_mode="HTML")

@dp.message(lambda message: message.text == "✅ Выполнить")
async def btn_done(message: types.Message):
    await message.answer(
        "✅ <b>Отметить выполненным</b>\n\n"
        "Формат: <code>/done ID</code>\n\n"
        "Пример: <code>/done 5</code>\n\n"
        " +50 XP за выполнение!",
        parse_mode="HTML"
    )

@dp.message(lambda message: message.text == "🗑 Удалить")
async def btn_delete(message: types.Message):
    await message.answer(
        "🗑 <b>Удалить дедлайн</b>\n\n"
        "Формат: <code>/delete ID</code>\n\n"
        "Пример: <code>/delete 5</code>",
        parse_mode="HTML"
    )

@dp.message(lambda message: message.text == "👤 Профиль")
async def btn_profile(message: types.Message):
    await cmd_profile(message)

@dp.message(lambda message: message.text == "🏆 Рейтинг")
async def btn_rating(message: types.Message):
    await cmd_rating(message)

@dp.message(lambda message: message.text == "🔍 Проверить напоминания")
async def btn_check(message: types.Message):
    await message.answer("🔍 <b>Проверяю дедлайны...</b>", parse_mode="HTML")
    await send_reminders()
    await message.answer("✅ <b>Проверка завершена!</b>", parse_mode="HTML")

@dp.message(lambda message: message.text == "📖 Справка")
async def btn_help(message: types.Message):
    await cmd_help(message)

@dp.message(lambda message: message.text == "◀️ Главное меню")
async def btn_back(message: types.Message):
    await message.answer(
        "🏠 <b>Главное меню</b>\n\nВыбери действие:",
        reply_markup=get_main_keyboard()
    )

# Админ-кнопки
@dp.message(lambda message: message.text == " Статистика")
async def btn_admin_stats(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_deadlines = await conn.fetchval("SELECT COUNT(*) FROM deadlines")
        active = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE is_done = FALSE")
    text = (
        f"📊 <b>Статистика:</b>\n\n"
        f" Пользователей: {total_users}\n"
        f"📋 Всего дедлайнов: {total_deadlines}\n"
        f" Активных: {active}"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(lambda message: message.text == " Пользователи")
async def btn_admin_users(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, full_name, xp, level FROM users ORDER BY xp DESC")
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    text = f"👥 <b>Пользователи ({len(rows)}):</b>\n\n"
    for row in rows:
        name = row['full_name'] or row['username'] or 'Аноним'
        text += f"• {name} — {row['xp']} XP (ур. {row['level']})\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(lambda message: message.text == " Логи")
async def btn_admin_logs(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_name, action, details, created_at FROM activity_logs ORDER BY created_at DESC LIMIT 15")
    if not rows:
        await message.answer("Логи пусты")
        return
    text = "📋 <b>Последние действия:</b>\n\n"
    for row in rows:
        time_str = row['created_at'].strftime("%d.%m %H:%M")
        text += f"[{time_str}] {row['user_name'] or 'anon'}: {row['action']}"
        if row['details']:
            text += f" — {row['details']}"
        text += "\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(lambda message: message.text == "📢 Рассылка")
async def btn_admin_broadcast(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await message.answer(
        " <b>Рассылка</b>\n\n"
        "Отправь текст сообщения, который нужно разослать всем.\n\n"
        "Или используй команду: <code>/broadcast текст</code>",
        parse_mode="HTML"
    )

# --- ОБЫЧНЫЕ КОМАНДЫ ---

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)
    text = (
        f"══════════════════════════╗\n"
        f"   🎉 Привет, {message.from_user.first_name}!\n"
        f"╚══════════════════════════╝\n\n"
        f"Я <b>DeadLine Bot</b> — твой личный помощник в учёбе 🎓\n\n"
        f"✨ <b>Что я умею:</b>\n"
        f"• Управлять дедлайнами одной командой\n"
        f"• Напоминать о сроках автоматически\n"
        f"• Считать твой рейтинг и уровень\n"
        f"• Показывать топ студентов группы\n\n"
        f" <b>Бонус:</b> получай XP за каждое действие и повышай уровень!\n\n"
        f"👇 Выбери действие в меню ниже:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    await log_action(message.chat.id, message.from_user.username or "anon", "start")

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    text = (
        "╔══════════════════════════╗\n"
        "        📖 СПРАВКА\n"
        "╚══════════════════════════╝\n\n"
        "🚀 <b>Основные команды:</b>\n"
        "• /start — приветствие\n"
        "• /help — эта справка\n\n"
        "📚 <b>Дедлайны:</b>\n"
        "• /add &lt;предмет&gt; &lt;ДД.ММ.ГГГГ&gt; — добавить (+10 XP)\n"
        "• /list — список дедлайнов\n"
        "• /done &lt;ID&gt; — отметить выполненным (+50 XP)\n"
        "• /delete &lt;ID&gt; — удалить\n"
        "• /check — проверить напоминания\n\n"
        "🏆 <b>Рейтинг и профиль:</b>\n"
        "• /profile — мой профиль и уровень\n"
        "• /rating — топ-10 группы\n\n"
        "💡 <b>Совет:</b> выполняй дедлайны вовремя, чтобы получать больше XP!"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('add'))
async def cmd_add(message: types.Message):
    await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)
    args = message.text.split()
    if len(args) < 3:
        await message.answer(" <b>Формат:</b> /add &lt;предмет&gt; &lt;ДД.ММ.ГГГГ&gt;\n\nПример: <code>/add Математика 15.10.2026</code>", parse_mode="HTML")
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
        await conn.execute("INSERT INTO deadlines (chat_id, subject, deadline_date) VALUES ($1, $2, $3)", message.chat.id, subject, date_str)
    
    # Начисляем +10 XP и сохраняем в БД
    new_level = await add_xp(message.chat.id, 10)
    xp_msg = "\n\n🎁 <b>+10 XP</b> получено!"
    if new_level:
        titles = {1: "🌱 Новичок", 2: "📚 Студент", 3: "🎓 Отличник", 4: "🏆 Магистр", 5: "👑 Легенда"}
        title = titles.get(min(new_level, 5), "⚡ Бог продуктивности")
        xp_msg += f"\n🎉 <b>Новый уровень {new_level}!</b> {title}"
    
    await message.answer(f"✅ <b>Дедлайн добавлен!</b>\n\n {subject}\n📅 {date_str}{xp_msg}", parse_mode="HTML")
    await log_action(message.chat.id, message.from_user.username or "anon", "add", f"{subject} {date_str}")

@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, subject, deadline_date, is_done FROM deadlines WHERE chat_id = $1 ORDER BY deadline_date", message.chat.id)
    if not rows:
        await message.answer("📭 <b>Нет активных дедлайнов.</b>\n\nДобавь первый: <code>/add Предмет ДД.ММ.ГГГГ</code>", parse_mode="HTML")
        return
    text = "╔══════════════════════════╗\n        📋 <b>ТВОИ ДЕДЛАЙНЫ</b>\n══════════════════════════╝\n\n"
    for row in rows:
        status = "✅" if row['is_done'] else "⏳"
        text += f"{status} <b>#{row['id']}</b> {row['subject']}\n    {row['deadline_date']}\n\n"
    text += "💡 Используй /done &lt;ID&gt; или /delete &lt;ID&gt;"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('done'))
async def cmd_done(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Формат: /done &lt;ID&gt;\nПример: <code>/done 5</code>", parse_mode="HTML")
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
        return
    
    # Начисляем +50 XP и сохраняем в БД
    new_level = await add_xp(message.chat.id, 50)
    xp_msg = "\n\n🎁 <b>+50 XP</b> получено!"
    if new_level:
        titles = {1: "🌱 Новичок", 2: "📚 Студент", 3: "🎓 Отличник", 4: "🏆 Магистр", 5: "👑 Легенда"}
        title = titles.get(min(new_level, 5), "⚡ Бог продуктивности")
        xp_msg += f"\n🎉 <b>Новый уровень {new_level}!</b> {title}"
    
    await message.answer(f"✅ <b>Дедлайн #{deadline_id} выполнен!</b>{xp_msg}", parse_mode="HTML")
    await log_action(message.chat.id, message.from_user.username or "anon", "done", f"ID {deadline_id}")

@dp.message(Command('delete'))
async def cmd_delete(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Формат: /delete &lt;ID&gt;\nПример: <code>/delete 5</code>", parse_mode="HTML")
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
        await message.answer(f"🗑 <b>Дедлайн #{deadline_id} удалён!</b>", parse_mode="HTML")

@dp.message(Command('check'))
async def cmd_check(message: types.Message):
    await message.answer("🔍 <b>Проверяю дедлайны...</b>", parse_mode="HTML")
    await send_reminders()
    await message.answer("✅ <b>Проверка завершена!</b>", parse_mode="HTML")

@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)
    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT xp, level FROM users WHERE chat_id = $1", message.chat.id)
        total = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE chat_id = $1", message.chat.id)
        done = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE chat_id = $1 AND is_done = TRUE", message.chat.id)
    total = total or 0
    done = done or 0
    active = total - done
    xp = row['xp'] if row else 0
    level = row['level'] if row else 1
    titles = {1: "🌱 Новичок", 2: "📚 Студент", 3: "🎓 Отличник", 4: "🏆 Магистр", 5: "👑 Легенда"}
    title = titles.get(min(level, 5), "⚡ Бог продуктивности")
    progress = xp % 100
    bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
    text = (
        f"╔══════════════════════════╗\n"
        f"        👤 <b>ТВОЙ ПРОФИЛЬ</b>\n"
        f"╚══════════════════════════╝\n\n"
        f"🏆 Уровень: <b>{level}</b> — {title}\n"
        f"⭐ XP: <b>{xp}</b> (до следующего: {100 - progress})\n"
        f"📊 Прогресс: [{bar}] {progress}%\n\n"
        f"📚 Статистика:\n"
        f"• Всего дедлайнов: {total}\n"
        f"• Активных: {active}\n"
        f"• Выполнено: {done}\n\n"
        f"💡 Добавляй и выполняй дедлайны, чтобы повышать уровень!"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('rating'))
async def cmd_rating(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, full_name, xp, level FROM users ORDER BY xp DESC LIMIT 10")
    if not rows:
        await message.answer("🏆 Рейтинг пока пуст. Будь первым!")
        return
    medals = ["", "🥈", "🥉"]
    text = "╔══════════════════════════╗\n         🏆 <b>ТОП-10 СТУДЕНТОВ</b>\n╚══════════════════════════╝\n\n"
    for i, row in enumerate(rows):
        prefix = medals[i] if i < 3 else f"  {i+1}."
        name = row['full_name'] or row['username'] or 'Аноним'
        text += f"{prefix} <b>{name}</b>\n   ⭐ {row['xp']} XP • Уровень {row['level']}\n\n"
    await message.answer(text, parse_mode="HTML")

# --- АДМИН-КОМАНДЫ ---

@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён! Эта команда только для администратора.")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна")
        return
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_deadlines = await conn.fetchval("SELECT COUNT(*) FROM deadlines")
        active = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE is_done = FALSE")
    text = (
        f"╔══════════════════════════╗\n"
        f"         📊 <b>АДМИН-ПАНЕЛЬ</b>\n"
        f"╚══════════════════════════╝\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"📋 Всего дедлайнов: <b>{total_deadlines}</b>\n"
        f"⏳ Активных: <b>{active}</b>\n\n"
        f" Выбери действие:"
    )
    await message.answer(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await log_action(message.chat.id, "admin", "admin_panel")

@dp.message(Command('users'))
async def cmd_users(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, full_name, xp, level FROM users ORDER BY xp DESC")
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    text = f"👥 <b>Пользователи ({len(rows)}):</b>\n\n"
    for row in rows:
        name = row['full_name'] or row['username'] or 'Аноним'
        text += f"• {name} — {row['xp']} XP (ур. {row['level']})\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('logs'))
async def cmd_logs(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_name, action, details, created_at FROM activity_logs ORDER BY created_at DESC LIMIT 15")
    if not rows:
        await message.answer("Логи пусты")
        return
    text = "📋 <b>Последние действия:</b>\n\n"
    for row in rows:
        time_str = row['created_at'].strftime("%d.%m %H:%M")
        text += f"[{time_str}] {row['user_name'] or 'anon'}: {row['action']}"
        if row['details']:
            text += f" — {row['details']}"
        text += "\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command('broadcast'))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    text_to_send = message.text.replace('/broadcast', '').strip()
    if not text_to_send:
        await message.answer("Формат: /broadcast &lt;текст&gt;", parse_mode="HTML")
        return
    pool = await get_db_pool()
    if not pool:
        return
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

# --- НАПОМИНАНИЯ ---

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
                    reminder_text = f"🔔 <b>СЕГОДНЯ дедлайн:</b> {row['subject']} ({row['deadline_date']})"
                elif deadline_date == tomorrow:
                    reminder_type = "tomorrow"
                    reminder_text = f"⏰ <b>ЗАВТРА дедлайн:</b> {row['subject']} ({row['deadline_date']})"
                elif deadline_date == day_after:
                    reminder_type = "day_after"
                    reminder_text = f" <b>Послезавтра дедлайн:</b> {row['subject']} ({row['deadline_date']})"
                if reminder_type and row['last_reminder'] != reminder_type:
                    try:
                        await bot.send_message(row['chat_id'], reminder_text, parse_mode="HTML")
                        await conn.execute("UPDATE deadlines SET last_reminder = $1 WHERE id = $2", reminder_type, row['id'])
                    except Exception as e:
                        print(f"Ошибка отправки: {e}")
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

# --- Flask HTTP-сервер ---
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
