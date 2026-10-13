import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

# Загружаем токен из файла .env
load_dotenv()

# Создаём бота (без прокси, через VPN)
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

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
        "(пока это тестовая версия, скоро добавим дедлайны!)"
    )

# Главная функция — запускает бота
async def main():
    print("🚀 Запускаю бота...")
    print("✅ Бот успешно запущен! Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())