import asyncio
import html
import os
import re
import threading
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from flask import Flask

load_dotenv()

ADMIN_ID = int(os.getenv('ADMIN_ID', '1752124938'))
TZ = ZoneInfo(os.getenv('TIMEZONE', 'Europe/Moscow'))
REMINDER_INTERVAL = 60 * 60

# --- Награды XP ---
XP_ADD_DEADLINE = 5
XP_DONE_DEADLINE = 15

# Шкала уровней
LEVEL_THRESHOLDS = [0, 100, 300, 600, 1000]

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()
db_pool = None


class RegisterStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_group = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_givexp_id = State()
    waiting_for_givexp_amount = State()
    waiting_for_takexp_id = State()
    waiting_for_takexp_amount = State()
    waiting_for_userdeadlines_id = State()


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def today_date():
    return datetime.now(TZ).date()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# --- ГРУППЫ ---
LATIN_TO_CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")

GROUP_TYPES = {
    "МО": "Менеджмент организации",
}

GROUP_RE = re.compile(r"^([А-ЯЁ]{1,6})(\d{2})(\d{0,2})$")


def parse_group(raw: str):
    value = re.sub(r"[\s\-_]", "", raw.upper()).translate(LATIN_TO_CYR)
    m = GROUP_RE.match(value)
    if not m:
        return None
    prefix, yy, num = m.groups()
    return value, prefix, 2000 + int(yy), num


def describe_group(group_number) -> str:
    if not group_number:
        return "Не указана"
    parsed = parse_group(group_number)
    if not parsed:
        return esc(group_number)
    value, prefix, year, _ = parsed
    now = datetime.now(TZ)
    course = now.year - year + (1 if now.month >= 9 else 0)
    parts = []
    if prefix in GROUP_TYPES:
        parts.append(GROUP_TYPES[prefix])
    parts.append(f"набор {year}")
    if 1 <= course <= 6:
        parts.append(f"{course} курс")
    return f"{esc(value)} ({', '.join(parts)})"


def address_name(full_name, fallback: str = "") -> str:
    parts = (full_name or "").split()
    if len(parts) >= 3:
        return f"{parts[1]} {parts[2]}"
    if len(parts) == 2:
        return parts[1]
    return fallback or (full_name or "")


TITLES = {1: "🌱 Новичок", 2: "📚 Студент", 3: "🎓 Отличник", 4: "🏆 Магистр", 5: "👑 Легенда"}


def get_title(level: int) -> str:
    return TITLES.get(level, "⚡ Бог продуктивности") if level <= 5 else "⚡ Бог продуктивности"


def level_for_xp(xp: int) -> int:
    level = 1
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            level = i + 1
    return level


def level_progress(xp: int):
    level = level_for_xp(xp)
    if level >= len(LEVEL_THRESHOLDS):
        return 100, None
    current = LEVEL_THRESHOLDS[level - 1]
    nxt = LEVEL_THRESHOLDS[level]
    return int((xp - current) * 100 / (nxt - current)), nxt - xp


def levels_scale_text() -> str:
    return "\n".join(
        f"• {i + 1} ур. — {get_title(i + 1)} — от {t} XP" for i, t in enumerate(LEVEL_THRESHOLDS)
    )


def level_sql_case() -> str:
    parts = [f"WHEN xp >= {t} THEN {i + 1}" for i, t in reversed(list(enumerate(LEVEL_THRESHOLDS)))]
    return "CASE " + " ".join(parts) + " END"


async def send_long(message: types.Message, text: str, **kwargs):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 4000:
            await message.answer(chunk, **kwargs)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, **kwargs)


async def report_error(message: types.Message, where: str, e: Exception):
    print(f"Ошибка {where}: {e!r}")
    traceback.print_exc()
    if is_admin(message.chat.id):
        await message.answer(
            f" <b>Ошибка {where}:</b>\n<code>{esc(repr(e))[:500]}</code>", parse_mode="HTML"
        )


# --- БАЗА ДАННЫХ ---

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = await asyncpg.create_pool(
                os.getenv('DATABASE_URL'),
                min_size=2,
                max_size=5,
                statement_cache_size=0,
            )
            print("✅ Подключено к Supabase")
        except Exception as e:
            print(f"⚠️ Ошибка подключения к БД: {e}")
    return db_pool


async def init_db():
    pool = await get_db_pool()
    if not pool:
        print("⚠️ БД недоступна, таблицы не созданы")
        return
    for attempt in range(3):
        try:
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
                        group_number TEXT,
                        is_registered BOOLEAN DEFAULT FALSE,
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
                migrations = [
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS chat_id BIGINT",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS subject TEXT",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS deadline_date TEXT",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS is_done BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS last_reminder TEXT",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS description TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS group_number TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_registered BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp INTEGER DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                    "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS chat_id BIGINT",
                    "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS user_name TEXT",
                    "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS action TEXT",
                    "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS details TEXT",
                    "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                    "ALTER TABLE users ALTER COLUMN group_number TYPE TEXT USING group_number::TEXT",
                    "UPDATE users SET xp = 0 WHERE xp IS NULL",
                    "UPDATE users SET level = 1 WHERE level IS NULL",
                    "UPDATE users SET is_registered = FALSE WHERE is_registered IS NULL",
                    "UPDATE deadlines SET is_done = FALSE WHERE is_done IS NULL",
                    f"UPDATE users SET level = {level_sql_case()}",
                ]
                for sql in migrations:
                    try:
                        await conn.execute(sql)
                    except Exception as e:
                        print(f"⚠️ Миграция не выполнена ({sql}): {e!r}")
                print("✅ Все таблицы готовы")
                return
        except Exception as e:
            print(f"⚠️ Попытка {attempt + 1} создания таблиц: {e}")
            await asyncio.sleep(2)
    print("❌ Не удалось создать таблицы после 3 попыток")


async def ensure_user(chat_id: int, username: str, full_name: str):
    pool = await get_db_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (chat_id, username, full_name) VALUES ($1, $2, $3) "
                    "ON CONFLICT (chat_id) DO UPDATE SET username = $2",
                    chat_id, username, full_name
                )
        except Exception as e:
            print(f"Ошибка ensure_user: {e}")


async def is_registered(chat_id: int) -> bool:
    pool = await get_db_pool()
    if not pool:
        return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_registered FROM users WHERE chat_id = $1", chat_id)
            return row['is_registered'] if row else False
    except Exception as e:
        print(f"Ошибка is_registered: {e}")
        return False


async def get_user_info(chat_id: int):
    pool = await get_db_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT full_name, username, group_number, xp, level FROM users WHERE chat_id = $1",
                chat_id
            )
    except Exception as e:
        print(f"Ошибка get_user_info: {e}")
        return None


def format_user_name(full_name: str, username: str) -> str:
    if full_name and username and username != "anon":
        return f"{esc(full_name)} (@{esc(username)})"
    elif full_name:
        return esc(full_name)
    elif username and username != "anon":
        return f"@{esc(username)}"
    return "Аноним"


async def add_xp(chat_id: int, amount: int):
    """Начисляет (или списывает, если amount отрицательный) XP."""
    pool = await get_db_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE users SET xp = GREATEST(0, xp + $1) WHERE chat_id = $2 RETURNING xp",
                amount, chat_id
            )
            if not row:
                return None
            new_xp = row['xp']
            old_level = level_for_xp(max(0, new_xp - amount))
            new_level = level_for_xp(new_xp)
            await conn.execute("UPDATE users SET level = $1 WHERE chat_id = $2", new_level, chat_id)
        if new_level != old_level:
            return new_level
    except Exception as e:
        print(f"Ошибка add_xp: {e}")
    return None


def xp_message(amount: int, new_level) -> str:
    if amount >= 0:
        msg = f"\n\n <b>+{amount} XP</b> получено!"
    else:
        msg = f"\n\n📉 <b>{amount} XP</b> списано!"
    if new_level:
        msg += f"\n🎉 <b>Новый уровень {new_level}!</b> {get_title(new_level)}"
    return msg


async def log_action(chat_id: int, user_name: str, action: str, details: str = ""):
    pool = await get_db_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO activity_logs (chat_id, user_name, action, details) VALUES ($1, $2, $3, $4)",
                    chat_id, user_name, action, details
                )
        except Exception as e:
            print(f"Ошибка log_action: {e}")


# --- КЛАВИАТУРЫ ---

BTN_ADD = "➕ Добавить дедлайн"
BTN_LIST = "📋 Мои дедлайны"
BTN_DONE = "✅ Выполнить"
BTN_DELETE = "🗑 Удалить"
BTN_PROFILE = "👤 Профиль"
BTN_RATING = "🏆 Рейтинг"
BTN_LEVELS = "🏅 Звания и уровни"
BTN_CHECK = "🔍 Проверить напоминания"
BTN_HELP = "📖 Справка"
BTN_STATS = "📊 Статистика"
BTN_USERS = "👥 Пользователи"
BTN_LOGS = "📋 Логи"
BTN_BROADCAST = "📢 Рассылка"
BTN_ANALYTICS = "📈 Аналитика"
BTN_GIVEXP = "💰 Начислить XP"
BTN_TAKEXP = "📉 Списать XP"
BTN_USERDEADLINES = "📋 Дедлайны юзера"
BTN_BACK = "◀️ Главное меню"


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_DONE), KeyboardButton(text=BTN_DELETE)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_RATING)],
            [KeyboardButton(text=BTN_LEVELS)],
            [KeyboardButton(text=BTN_CHECK)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие"
    )


def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_ANALYTICS)],
            [KeyboardButton(text=BTN_USERS), KeyboardButton(text=BTN_LOGS)],
            [KeyboardButton(text=BTN_BROADCAST)],
            [KeyboardButton(text=BTN_GIVEXP), KeyboardButton(text=BTN_TAKEXP)],
            [KeyboardButton(text=BTN_USERDEADLINES)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-панель"
    )


# --- ПРОВЕРКА РЕГИСТРАЦИИ ---

async def check_registered(message: types.Message) -> bool:
    if is_admin(message.chat.id):
        await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)
        return True
    if not await is_registered(message.chat.id):
        await message.answer(
            "⚠️ <b>Сначала зарегистрируйся!</b>\n\n"
            "Используй команду /register, чтобы ввести ФИО и номер группы.\n"
            "После регистрации станут доступны все функции.",
            parse_mode="HTML"
        )
        return False
    return True


# --- РЕГИСТРАЦИЯ ---

@dp.message(Command('cancel'))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    kb = get_admin_keyboard() if is_admin(message.chat.id) else get_main_keyboard()
    await message.answer("❌ Действие отменено.", reply_markup=kb)


@dp.message(Command('register'))
async def cmd_register(message: types.Message, state: FSMContext):
    if await is_registered(message.chat.id):
        await message.answer("✅ Ты уже зарегистрирован! Используй /profile для просмотра профиля.")
        return
    await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)
    await message.answer(
        "📝 <b>Регистрация</b>\n\n"
        "Введи своё ФИО:\n"
        "Пример: <code>Иванов Иван Иванович</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(RegisterStates.waiting_for_name)


@dp.message(RegisterStates.waiting_for_name, F.text)
async def process_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if full_name.startswith("/"):
        await message.answer("Введи ФИО текстом или отправь /cancel для отмены.")
        return
    if len(full_name) < 3 or len(full_name) > 100:
        await message.answer("❌ ФИО должно быть длиной от 3 до 100 символов. Попробуй ещё раз.")
        return
    await state.update_data(full_name=full_name)
    await message.answer(
        f"✅ ФИО сохранено: <b>{esc(full_name)}</b>\n\n"
        "Теперь введи номер группы:\n"
        "Пример: <code>МО250</code>, <code>ГМУ230</code>, <code>ЭО240</code>\n"
        "(буквы направления + год поступления + номер)",
        parse_mode="HTML"
    )
    await state.set_state(RegisterStates.waiting_for_group)


@dp.message(RegisterStates.waiting_for_group, F.text)
async def process_group(message: types.Message, state: FSMContext):
    data = await state.get_data()
    full_name = data.get('full_name')
    group_number = message.text.strip()
    if group_number.startswith("/"):
        await message.answer("Введи номер группы текстом или отправь /cancel для отмены.")
        return
    parsed = parse_group(group_number)
    if not parsed or not (2015 <= parsed[2] <= today_date().year):
        await message.answer(
            "❌ Не понял номер группы.\n\n"
            "Формат: буквы направления + год поступления (2 цифры) + номер.\n"
            "Примеры: <code>МО250</code>, <code>ГМУ230</code>, <code>ЭО240</code>",
            parse_mode="HTML"
        )
        return
    group_number = parsed[0]
    if not full_name:
        await state.clear()
        await message.answer("⚠️ Что-то пошло не так. Начни заново: /register")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна. Попробуй позже.")
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET full_name = $1, group_number = $2, is_registered = TRUE WHERE chat_id = $3",
                full_name, group_number, message.chat.id
            )
    except Exception as e:
        print(f"Ошибка регистрации: {e!r}")
        await message.answer("❌ Не удалось сохранить данные. Попробуй ещё раз.")
        return
    await state.clear()
    await log_action(message.chat.id, full_name, "register", f"Группа: {group_number}")
    await message.answer(
        f"✅ <b>Регистрация завершена, {esc(address_name(full_name))}!</b>\n\n"
        f"👤 ФИО: {esc(full_name)}\n"
        f"🎓 Группа: {describe_group(group_number)}\n\n"
        f"Теперь тебе доступны все команды!\n"
        f"Используй /help для просмотра списка.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


# --- ЗВАНИЯ И УРОВНИ (для всех) ---

@dp.message(Command('levels'))
@dp.message(F.text == BTN_LEVELS)
async def cmd_levels(message: types.Message):
    pool = await get_db_pool()
    user_xp = 0
    user_level = 1
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT xp FROM users WHERE chat_id = $1", message.chat.id)
                if row:
                    user_xp = row['xp']
                    user_level = level_for_xp(user_xp)
        except Exception:
            pass

    text = (
        "╔══════════════════════════╗\n"
        "        🏅 <b>ЗВАНИЯ И УРОВНИ</b>\n"
        "╚══════════════════════════╝\n\n"
        "Выполняй дедлайны, получай XP и повышай свой статус!\n\n"
        f"{levels_scale_text()}\n\n"
    )
    
    if await is_registered(message.chat.id) or user_xp > 0:
        progress, to_next = level_progress(user_xp)
        bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
        to_next_text = f"Осталось {to_next} XP до следующего уровня!" if to_next else "Ты достиг максимального уровня! 🎉"
        
        text += (
            "──────────────────────\n"
            f"👤 <b>Твой текущий прогресс:</b>\n"
            f"⭐ XP: {user_xp} | Уровень: {user_level} ({get_title(user_level)})\n"
            f"📊 [{bar}] {progress}%\n"
            f"💡 {to_next_text}\n\n"
        )
    
    text += (
        "💰 <b>Как получить XP:</b>\n"
        f"• Добавить дедлайн: +{XP_ADD_DEADLINE} XP\n"
        f"• Выполнить дедлайн: +{XP_DONE_DEADLINE} XP\n\n"
        "💡 <i>Совет: регулярное выполнение задач — ключ к званию Легенды!</i>"
    )
    
    await message.answer(text, parse_mode="HTML")


# --- АДМИН: FSM ---

@dp.message(AdminStates.waiting_for_broadcast, F.text)
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    await state.clear()
    await do_broadcast(message, message.text.strip())


@dp.message(AdminStates.waiting_for_givexp_id, F.text)
async def process_givexp_id(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    if message.text.strip().startswith("/"):
        await message.answer("Введи ID пользователя числом или /cancel для отмены.")
        return
    try:
        target_id = int(message.text.strip())
        await state.update_data(target_id=target_id)
        await message.answer("Теперь введи количество XP для начисления:\nПример: <code>50</code>", parse_mode="HTML")
        await state.set_state(AdminStates.waiting_for_givexp_amount)
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуй ещё раз или /cancel для отмены.")


@dp.message(AdminStates.waiting_for_givexp_amount, F.text)
async def process_givexp_amount(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    if message.text.strip().startswith("/"):
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=get_admin_keyboard())
        return
    try:
        amount = int(message.text.strip())
        data = await state.get_data()
        target_id = data['target_id']
        await state.clear()
        
        new_level = await add_xp(target_id, amount)
        if new_level:
            await message.answer(
                f"✅ Начислено {amount} XP пользователю {target_id}.\n"
                f"🎉 Новый уровень: {new_level} ({get_title(new_level)})!",
                reply_markup=get_admin_keyboard()
            )
        else:
            await message.answer(
                f"✅ Начислено {amount} XP пользователю {target_id}.\n"
                f"Уровень не изменился.",
                reply_markup=get_admin_keyboard()
            )
        await log_action(message.chat.id, "admin", "givexp", f"User: {target_id}, XP: +{amount}")
    except ValueError:
        await message.answer("❌ Количество XP должно быть числом. Попробуй ещё раз или /cancel для отмены.")


@dp.message(AdminStates.waiting_for_takexp_id, F.text)
async def process_takexp_id(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    if message.text.strip().startswith("/"):
        await message.answer("Введи ID пользователя числом или /cancel для отмены.")
        return
    try:
        target_id = int(message.text.strip())
        await state.update_data(target_id=target_id)
        await message.answer("Теперь введи количество XP для списания:\nПример: <code>50</code>", parse_mode="HTML")
        await state.set_state(AdminStates.waiting_for_takexp_amount)
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуй ещё раз или /cancel для отмены.")


@dp.message(AdminStates.waiting_for_takexp_amount, F.text)
async def process_takexp_amount(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    if message.text.strip().startswith("/"):
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=get_admin_keyboard())
        return
    try:
        amount = int(message.text.strip())
        data = await state.get_data()
        target_id = data['target_id']
        await state.clear()
        
        new_level = await add_xp(target_id, -amount)
        if new_level:
            await message.answer(
                f"📉 Списано {amount} XP у пользователя {target_id}.\n"
                f"⚠️ Новый уровень: {new_level} ({get_title(new_level)})",
                reply_markup=get_admin_keyboard()
            )
        else:
            await message.answer(
                f"📉 Списано {amount} XP у пользователя {target_id}.\n"
                f"Уровень не изменился.",
                reply_markup=get_admin_keyboard()
            )
        await log_action(message.chat.id, "admin", "takexp", f"User: {target_id}, XP: -{amount}")
    except ValueError:
        await message.answer("❌ Количество XP должно быть числом. Попробуй ещё раз или /cancel для отмены.")


@dp.message(AdminStates.waiting_for_userdeadlines_id, F.text)
async def process_userdeadlines_id(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await state.clear()
        return
    if message.text.strip().startswith("/"):
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=get_admin_keyboard())
        return
    try:
        target_id = int(message.text.strip())
        await state.clear()
        await show_user_deadlines(message, target_id)
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуй ещё раз или /cancel для отмены.")


async def show_user_deadlines(message: types.Message, target_chat_id: int):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна")
        return
    try:
        async with pool.acquire() as conn:
            user = await conn.fetchrow("SELECT full_name, username, group_number FROM users WHERE chat_id = $1", target_chat_id)
            name = user['full_name'] or user['username'] or "Неизвестно" if user else "Неизвестно"
            group = user['group_number'] if user else None
            
            rows = await conn.fetch(
                "SELECT id, subject, deadline_date, is_done, description FROM deadlines WHERE chat_id = $1 ORDER BY is_done, to_date(deadline_date, 'DD.MM.YYYY')",
                target_chat_id
            )
        
        if not rows:
            await message.answer(
                f"📭 У пользователя {esc(name)} (ID: {target_chat_id}) нет дедлайнов.",
                reply_markup=get_admin_keyboard()
            )
            return
        
        group_text = f" (гр. {esc(group)})" if group else ""
        text = f"📋 <b>Дедлайны пользователя:</b>\n{esc(name)}{group_text} (ID: {target_chat_id})\n\n"
        for row in rows:
            status = "✅" if row['is_done'] else ""
            text += f"{status} #{row['id']} {esc(row['subject'])} ({row['deadline_date']})\n"
            if row.get('description'):
                text += f"   📝 {esc(row['description'])}\n"
            text += "\n"
        
        await send_long(message, text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "просмотра дедлайнов юзера", e)


# --- ОБЫЧНЫЕ КОМАНДЫ ---

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.chat.id, message.from_user.username or "anon", message.from_user.first_name)

    if is_admin(message.chat.id) or await is_registered(message.chat.id):
        user_info = await get_user_info(message.chat.id)
        if user_info:
            display_name = esc(address_name(user_info['full_name'], message.from_user.first_name))
        else:
            display_name = esc(message.from_user.first_name)
        text = (
            f"╔══════════════════════════╗\n"
            f"   🎉 Привет, {display_name}!\n"
            f"══════════════════════════╝\n\n"
            f"Я <b>DeadLine Bot</b> — твой личный помощник в учёбе \n\n"
            f"✨ <b>Что я умею:</b>\n"
            f"• Управлять дедлайнами одной командой\n"
            f"• Напоминать о сроках автоматически\n"
            f"• Считать твой рейтинг и уровень\n"
            f"• Показывать топ студентов группы\n\n"
            f"🎁 <b>Бонус:</b> получай XP за каждое действие и повышай уровень!\n\n"
            f" Выбери действие в меню ниже:"
        )
    else:
        text = (
            f"╔══════════════════════════╗\n"
            f"   👋 Привет, {esc(message.from_user.first_name)}!\n"
            f"╚══════════════════════════╝\n\n"
            f"Я <b>DeadLine Bot</b> — бот для управления дедлайнами 🎓\n\n"
            f"⚠️ <b>Сначала зарегистрируйся!</b>\n\n"
            f"Используй команду /register, чтобы ввести ФИО и номер группы.\n"
            f"После регистрации станут доступны все функции.\n\n"
            f"Используй /help, чтобы узнать команды."
        )
    # ВСЕГДА показываем обычное меню
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
        "• /help — эта справка\n"
        "• /register — регистрация (ФИО и группа)\n"
        "• /cancel — отменить текущее действие\n\n"
        "📚 <b>Дедлайны (после регистрации):</b>\n"
        f"• /add &lt;предмет&gt; &lt;ДД.ММ.ГГГГ&gt; [описание] — добавить (+{XP_ADD_DEADLINE} XP)\n"
        "• /list — список дедлайнов\n"
        f"• /done &lt;ID&gt; — отметить выполненным (+{XP_DONE_DEADLINE} XP)\n"
        "• /delete &lt;ID&gt; — удалить\n"
        "• /check — проверить напоминания\n\n"
        "🏆 <b>Рейтинг и профиль:</b>\n"
        "• /profile — мой профиль и уровень\n"
        "• /rating — топ-10 студентов\n"
        "• /levels — звания и уровни\n\n"
        f"📈 <b>Уровни:</b>\n{levels_scale_text()}\n\n"
        "💡 <b>Совет:</b> выполняй дедлайны вовремя, чтобы получать больше XP!"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command('add'))
async def cmd_add(message: types.Message):
    if not await check_registered(message):
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ <b>Формат:</b> /add &lt;предмет&gt; &lt;ДД.ММ.ГГГГ&gt; [описание]\n\n"
            "Пример: <code>/add Математика 15.10.2026 Решить задачи 1-10 из учебника</code>\n\n"
            "Описание необязательно, но полезно!",
            parse_mode="HTML"
        )
        return

    parts = args[2].split()
    date_str = parts[0]
    description = " ".join(parts[1:]) if len(parts) > 1 else ""

    subject = args[1]
    if len(subject) > 100:
        await message.answer("❌ Название предмета слишком длинное (макс. 100 символов).")
        return
    if len(description) > 500:
        await message.answer("❌ Описание слишком длинное (макс. 500 символов).")
        return

    try:
        deadline = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используй ДД.ММ.ГГГГ")
        return
    if deadline < today_date():
        await message.answer("❌ Эта дата уже прошла. Укажи сегодняшнюю или будущую дату.")
        return

    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO deadlines (chat_id, subject, deadline_date, description) VALUES ($1, $2, $3, $4)",
                message.chat.id, subject, date_str, description
            )
        new_level = await add_xp(message.chat.id, XP_ADD_DEADLINE)

        desc_text = f"\n📝 {esc(description)}" if description else ""
        await message.answer(
            f"✅ <b>Дедлайн добавлен!</b>\n\n📚 {esc(subject)}\n📅 {date_str}{desc_text}"
            f"{xp_message(XP_ADD_DEADLINE, new_level)}",
            parse_mode="HTML"
        )
        await log_action(message.chat.id, message.from_user.username or "anon", "add", f"{subject} {date_str}")
    except Exception as e:
        print(f"Ошибка add: {e}")
        await message.answer("❌ Ошибка при добавлении дедлайна")


@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    if not await check_registered(message):
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, subject, deadline_date, is_done, description FROM deadlines WHERE chat_id = $1 "
                "ORDER BY is_done, to_date(deadline_date, 'DD.MM.YYYY')",
                message.chat.id
            )
                if not rows:
            await message.answer(
                "📭 <b>Дедлайнов пока нет.</b>\n\n"
                "Добавь первый: <code>/add Предмет ДД.ММ.ГГГГ [описание]</code>\n\n"
                "Пример: <code>/add Математика 15.10.2026 Решить задачи 1-10</code>",
                parse_mode="HTML"
            )
            return
        text = "╔══════════════════════════╗\n        📋 <b>ТВОИ ДЕДЛАЙНЫ</b>\n╚══════════════════════════╝\n\n"
        for row in rows:
            status = "✅" if row['is_done'] else "⏳"
            text += f"{status} <b>#{row['id']}</b> {esc(row['subject'])}\n    {row['deadline_date']}\n"
            if row.get('description'):
                text += f"   📝 {esc(row['description'])}\n"
            text += "\n"
        text += "💡 Используй /done &lt;ID&gt; или /delete &lt;ID&gt;"
        await send_long(message, text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "списка дедлайнов", e)
        await message.answer("❌ Не удалось получить список дедлайнов")


@dp.message(Command('done'))
async def cmd_done(message: types.Message):
    if not await check_registered(message):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer(" Формат: /done &lt;ID&gt;\nПример: <code>/done 5</code>", parse_mode="HTML")
        return
    try:
        deadline_id = int(args[1])
    except ValueError:
        await message.answer(" ID должен быть числом")
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE deadlines SET is_done = TRUE WHERE id = $1 AND chat_id = $2 AND is_done = FALSE",
                deadline_id, message.chat.id
            )
            if result == "UPDATE 0":
                exists = await conn.fetchval(
                    "SELECT 1 FROM deadlines WHERE id = $1 AND chat_id = $2", deadline_id, message.chat.id
                )
        if result == "UPDATE 0":
            if exists:
                await message.answer("ℹ️ Этот дедлайн уже отмечен выполненным.")
            else:
                await message.answer("❌ Дедлайн не найден")
            return
        new_level = await add_xp(message.chat.id, XP_DONE_DEADLINE)
        await message.answer(
            f"✅ <b>Дедлайн #{deadline_id} выполнен!</b>{xp_message(XP_DONE_DEADLINE, new_level)}",
            parse_mode="HTML"
        )
        await log_action(message.chat.id, message.from_user.username or "anon", "done", f"ID {deadline_id}")
    except Exception as e:
        print(f"Ошибка done: {e}")
        await message.answer("❌ Ошибка при выполнении команды")


@dp.message(Command('delete'))
async def cmd_delete(message: types.Message):
    if not await check_registered(message):
        return
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
        await message.answer("️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM deadlines WHERE id = $1 AND chat_id = $2",
                deadline_id, message.chat.id
            )
        if result == "DELETE 0":
            await message.answer("❌ Дедлайн не найден")
        else:
            await message.answer(f"🗑 <b>Дедлайн #{deadline_id} удалён!</b>", parse_mode="HTML")
            await log_action(message.chat.id, message.from_user.username or "anon", "delete", f"ID {deadline_id}")
    except Exception as e:
        print(f"Ошибка delete: {e}")
        await message.answer("❌ Ошибка при удалении")


@dp.message(Command('check'))
async def cmd_check(message: types.Message):
    if not await check_registered(message):
        return
    await message.answer("🔍 <b>Проверяю дедлайны...</b>", parse_mode="HTML")
    sent = await send_reminders(chat_id=message.chat.id)
    if sent:
        await message.answer(f"✅ <b>Проверка завершена!</b> Напоминаний отправлено: {sent}", parse_mode="HTML")
    else:
        await message.answer("✅ <b>Проверка завершена!</b> Новых напоминаний нет.", parse_mode="HTML")


@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    if not await check_registered(message):
        return
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT full_name, username, group_number, xp, level FROM users WHERE chat_id = $1",
                message.chat.id
            )
            total = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE chat_id = $1", message.chat.id)
            done = await conn.fetchval(
                "SELECT COUNT(*) FROM deadlines WHERE chat_id = $1 AND is_done = TRUE", message.chat.id
            )
        total = total or 0
        done = done or 0
        active = total - done
        xp = row['xp'] if row else 0
        display_name = format_user_name(row['full_name'] if row else None, row['username'] if row else None)
        group_number = describe_group(row['group_number'] if row else None)
        level = level_for_xp(xp)
        progress, to_next = level_progress(xp)
        bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
        to_next_text = f"до следующего: {to_next}" if to_next is not None else "максимальный уровень 🎉"
        text = (
            f"╔══════════════════════════╗\n"
            f"         <b>ТВОЙ ПРОФИЛЬ</b>\n"
            f"╚══════════════════════════╝\n\n"
            f"👤 <b>ФИО:</b> {display_name}\n"
            f" <b>Группа:</b> {group_number}\n"
            f"🏆 <b>Уровень:</b> {level} — {get_title(level)}\n"
            f"⭐ <b>XP:</b> {xp} ({to_next_text})\n"
            f" <b>Прогресс:</b> [{bar}] {progress}%\n\n"
            f"📚 <b>Статистика:</b>\n"
            f"• Всего дедлайнов: {total}\n"
            f"• Активных: {active}\n"
            f"• Выполнено: {done}\n\n"
            f"💡 Добавляй и выполняй дедлайны, чтобы повышать уровень!"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "профиля", e)
        await message.answer("❌ Не удалось загрузить профиль")


@dp.message(Command('rating'))
async def cmd_rating(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        await message.answer("⚠️ База данных недоступна.")
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT full_name, username, group_number, xp, level FROM users "
                "WHERE is_registered = TRUE ORDER BY xp DESC LIMIT 10"
            )
        if not rows:
            await message.answer("🏆 Рейтинг пока пуст. Зарегистрируйся через /register и стань первым!")
            return
        medals = ["", "🥈", "🥉"]
        text = "╔══════════════════════════╗\n         🏆 <b>ТОП-10 СТУДЕНТОВ</b>\n╚══════════════════════════╝\n\n"
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"  {i + 1}."
            display_name = format_user_name(row['full_name'], row['username'])
            group = f" (гр. {esc(row['group_number'])})" if row['group_number'] else ""
            text += f"{prefix} <b>{display_name}</b>{group}\n   ⭐ {row['xp']} XP • Уровень {row['level']}\n\n"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "рейтинга", e)
        await message.answer("❌ Не удалось загрузить рейтинг")


# --- АДМИН: логика ---

async def admin_stats(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_registered = TRUE")
            total_all = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_deadlines = await conn.fetchval("SELECT COUNT(*) FROM deadlines")
            active = await conn.fetchval("SELECT COUNT(*) FROM deadlines WHERE is_done = FALSE")
        text = (
            f"📊 <b>Статистика:</b>\n\n"
            f"👥 Зарегистрировано: <b>{total_users}</b> / {total_all}\n"
            f"📋 Всего дедлайнов: <b>{total_deadlines}</b>\n"
            f"⏳ Активных: <b>{active}</b>"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "статистики", e)


async def admin_users(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chat_id, username, full_name, group_number, is_registered, xp, level "
                "FROM users ORDER BY xp DESC"
            )
        if not rows:
            await message.answer("Пользователей пока нет")
            return
        text = f"👥 <b>Пользователи ({len(rows)}):</b>\n\n"
        for row in rows:
            reg_status = "✅" if row['is_registered'] else "❌"
            display_name = format_user_name(row['full_name'], row['username'])
            text += f"{reg_status} ID: <code>{row['chat_id']}</code>\n"
            text += f"   👤 {display_name}"
            if row['group_number']:
                text += f" (гр. {esc(row['group_number'])})"
            text += f" — {row['xp']} XP (ур. {row['level']})\n\n"
        await send_long(message, text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "списка пользователей", e)


async def admin_logs(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_name, action, details, created_at FROM activity_logs "
                "ORDER BY created_at DESC LIMIT 15"
            )
        if not rows:
            await message.answer("Логи пусты")
            return
        text = "📋 <b>Последние действия:</b>\n\n"
        for row in rows:
            time_str = row['created_at'].strftime("%d.%m %H:%M")
            text += f"[{time_str}] {esc(row['user_name'] or 'anon')}: {esc(row['action'])}"
            if row['details']:
                text += f" — {esc(row['details'])}"
            text += "\n"
        await send_long(message, text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "логов", e)


async def do_broadcast(message: types.Message, text_to_send: str):
    if not text_to_send:
        await message.answer("Формат: /broadcast &lt;текст&gt;", parse_mode="HTML")
        return
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT chat_id FROM users WHERE is_registered = TRUE")
        sent, failed = 0, 0
        for row in rows:
            try:
                await bot.send_message(
                    row['chat_id'], f"📢 <b>Объявление:</b>\n\n{esc(text_to_send)}", parse_mode="HTML"
                )
                sent += 1
            except Exception as e:
                print(f"Ошибка рассылки {row['chat_id']}: {e}")
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(f"✅ Рассылка завершена!\nОтправлено: {sent}\nОшибок: {failed}")
        await log_action(message.chat.id, "admin", "broadcast", text_to_send[:30])
    except Exception as e:
        print(f"Ошибка broadcast: {e}")


async def admin_analytics(message: types.Message):
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT subject, COUNT(*) as count FROM deadlines GROUP BY subject ORDER BY count DESC LIMIT 5"
            )
        if not rows:
            await message.answer("📈 Пока недостаточно данных для аналитики.")
            return
        
        text = "📈 <b>ТОП-5 самых популярных предметов:</b>\n\n"
        for i, row in enumerate(rows):
            medal = ["🥇", "🥈", "", "4️⃣", "5️⃣"][i]
            text += f"{medal} <b>{esc(row['subject'])}</b> — {row['count']} дедлайн(ов)\n"
        
        text += "\n <i>Эти данные помогают понять, по каким предметам у студентов наибольшая нагрузка.</i>"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "аналитики", e)


# --- АДМИН-КОМАНДЫ ---

@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await message.answer("🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\n👇 Выбери действие:",
                         reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await admin_stats(message)
    await log_action(message.chat.id, "admin", "admin_panel")


@dp.message(Command('users'))
async def cmd_users(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_users(message)


@dp.message(Command('logs'))
async def cmd_logs(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_logs(message)


@dp.message(Command('broadcast'))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    text_to_send = message.text.replace('/broadcast', '', 1).strip()
    await do_broadcast(message, text_to_send)


@dp.message(Command('dbcheck'))
async def cmd_dbcheck(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    pool = await get_db_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name IN ('users', 'deadlines', 'activity_logs') "
                "ORDER BY table_name, ordinal_position"
            )
        text = "🗄 <b>Структура БД:</b>\n"
        current = None
        for r in rows:
            if r['table_name'] != current:
                current = r['table_name']
                text += f"\n<b>{current}</b>\n"
            text += f"• {r['column_name']} — {r['data_type']}\n"
        await send_long(message, text, parse_mode="HTML")
    except Exception as e:
        await report_error(message, "dbcheck", e)


# --- ОБРАБОТЧИКИ КНОПОК ---

@dp.message(F.text == BTN_ADD)
async def btn_add(message: types.Message):
    if not await check_registered(message):
        return
    await message.answer(
        "➕ <b>Добавить дедлайн</b>\n\n"
        "Формат: <code>/add Предмет ДД.ММ.ГГГГ [описание]</code>\n\n"
        "Пример: <code>/add Математика 15.10.2026 Решить задачи 1-10</code>\n\n"
        "Описание необязательно, но полезно!\n\n"
        f"🎁 +{XP_ADD_DEADLINE} XP за каждый дедлайн!",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_LIST)
async def btn_list(message: types.Message):
    await cmd_list(message)


@dp.message(F.text == BTN_DONE)
async def btn_done(message: types.Message):
    if not await check_registered(message):
        return
    await message.answer(
        "✅ <b>Отметить выполненным</b>\n\n"
        "Формат: <code>/done ID</code>\n\n"
        "Пример: <code>/done 5</code>\n\n"
        f"🎁 +{XP_DONE_DEADLINE} XP за выполнение!",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_DELETE)
async def btn_delete(message: types.Message):
    if not await check_registered(message):
        return
    await message.answer(
        "🗑 <b>Удалить дедлайн</b>\n\n"
        "Формат: <code>/delete ID</code>\n\n"
        "Пример: <code>/delete 5</code>",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_PROFILE)
async def btn_profile(message: types.Message):
    await cmd_profile(message)


@dp.message(F.text == BTN_RATING)
async def btn_rating(message: types.Message):
    await cmd_rating(message)


@dp.message(F.text == BTN_LEVELS)
async def btn_levels(message: types.Message):
    await cmd_levels(message)


@dp.message(F.text == BTN_CHECK)
async def btn_check(message: types.Message):
    await cmd_check(message)


@dp.message(F.text == BTN_HELP)
async def btn_help(message: types.Message):
    await cmd_help(message)


@dp.message(F.text == BTN_BACK)
async def btn_back(message: types.Message):
    kb = get_admin_keyboard() if is_admin(message.chat.id) else get_main_keyboard()
    await message.answer(
        "🏠 <b>Главное меню</b>\n\nВыбери действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )


# Админ-кнопки
@dp.message(F.text == BTN_STATS)
async def btn_admin_stats(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_stats(message)


@dp.message(F.text == BTN_USERS)
async def btn_admin_users(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_users(message)


@dp.message(F.text == BTN_LOGS)
async def btn_admin_logs(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_logs(message)


@dp.message(F.text == BTN_BROADCAST)
async def btn_admin_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь следующим сообщением текст, который нужно разослать всем зарегистрированным.\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_ANALYTICS)
async def btn_admin_analytics(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await admin_analytics(message)


@dp.message(F.text == BTN_GIVEXP)
async def btn_admin_givexp(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await state.set_state(AdminStates.waiting_for_givexp_id)
    await message.answer(
        "💰 <b>Начислить XP пользователю</b>\n\n"
        "Введи ID пользователя (chat_id):\n"
        "Пример: <code>123456789</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_TAKEXP)
async def btn_admin_takexp(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await state.set_state(AdminStates.waiting_for_takexp_id)
    await message.answer(
        "📉 <b>Списать XP у пользователя</b>\n\n"
        "Введи ID пользователя (chat_id):\n"
        "Пример: <code>123456789</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_USERDEADLINES)
async def btn_admin_userdeadlines(message: types.Message, state: FSMContext):
    if not is_admin(message.chat.id):
        await message.answer("⛔ Доступ запрещён!")
        return
    await state.set_state(AdminStates.waiting_for_userdeadlines_id)
    await message.answer(
        "📋 <b>Просмотр дедлайнов пользователя</b>\n\n"
        "Введи ID пользователя (chat_id):\n"
        "Пример: <code>123456789</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )


# --- НАПОМИНАНИЯ ---

async def send_reminders(chat_id: int = None) -> int:
    pool = await get_db_pool()
    if not pool:
        return 0
    today = today_date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    sent = 0
    try:
        async with pool.acquire() as conn:
            if chat_id is None:
                rows = await conn.fetch(
                    "SELECT id, chat_id, subject, deadline_date, last_reminder, description "
                    "FROM deadlines WHERE is_done = FALSE"
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, chat_id, subject, deadline_date, last_reminder, description "
                    "FROM deadlines WHERE is_done = FALSE AND chat_id = $1",
                    chat_id
                )
        for row in rows:
            try:
                deadline_date = datetime.strptime(row['deadline_date'], "%d.%m.%Y").date()
            except ValueError:
                continue
            subject = esc(row['subject'])
            description = f"\n📝 {esc(row['description'])}" if row.get('description') else ""
            reminder_type = None
            reminder_text = None
            if deadline_date == today:
                reminder_type = "today"
                reminder_text = f"🔔 <b>СЕГОДНЯ дедлайн:</b> {subject} ({row['deadline_date']}){description}"
            elif deadline_date == tomorrow:
                reminder_type = "tomorrow"
                reminder_text = f"⏰ <b>ЗАВТРА дедлайн:</b> {subject} ({row['deadline_date']}){description}"
            elif deadline_date == day_after:
                reminder_type = "day_after"
                reminder_text = f"📅 <b>Послезавтра дедлайн:</b> {subject} ({row['deadline_date']}){description}"
            if reminder_type and row['last_reminder'] != reminder_type:
                try:
                    await bot.send_message(row['chat_id'], reminder_text, parse_mode="HTML")
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE deadlines SET last_reminder = $1 WHERE id = $2", reminder_type, row['id']
                        )
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"Ошибка отправки: {e}")
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")
    return sent


async def reminder_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await send_reminders()
        except Exception as e:
            print(f"Ошибка reminder_loop: {e}")
        await asyncio.sleep(REMINDER_INTERVAL)


# --- Flask ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

async def main():
    try:
        await init_db()
    except Exception as e:
        print(f"⚠️ Ошибка init_db: {e}")
    print("🚀 Запускаю Telegram-бота...")
    reminder_task = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await bot.session.close()
        if db_pool:
            await db_pool.close()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 HTTP-сервер запущен")
    asyncio.run(main())
