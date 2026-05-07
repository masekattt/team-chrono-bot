import os
import random
import asyncio
import gspread
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from oauth2client.service_account import ServiceAccountCredentials
import pytz
import json

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная BOT_TOKEN не установлена!")

# ЗАМЕНИТЕ НА ВАШ ID ТАБЛИЦЫ
SHEET_ID = "1pKNd-VfzUEK3A7D-p1lZTGpvZHtO7UPIRevn4YZqSSQ"

TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 19
REMINDER_MINUTE = 0

GALLERY = [
    "https://example.com/image1.jpg",
    "https://example.com/image2.jpg",
]

# ========== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==========
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

credentials_json = os.environ.get("CREDENTIALS_JSON")
if credentials_json:
    creds_dict = json.loads(credentials_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    print("✅ Credentials загружены из переменной")
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    print("✅ Credentials загружены из файла")

client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID)
users_ws = sheet.worksheet("Users")
chrono_ws = sheet.worksheet("Chrono")

# ========== КНОПКИ ==========
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Ввести часы")],
        [KeyboardButton(text="✏️ Изменить часы")]
    ],
    resize_keyboard=True
)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_text = State()
    waiting_for_edit_date = State()
    waiting_for_new_text = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (работа с индексами столбцов) ==========
# Столбцы в листе Chrono:
# A (1) = ФИО
# B (2) = Текст
# C (3) = Дата и время создания
# D (4) = Дата и время изменения

def get_user_name(telegram_id: int):
    try:
        cell = users_ws.find(str(telegram_id))
        return users_ws.cell(cell.row, 2).value
    except:
        return None

def save_user(telegram_id: int, full_name: str):
    users_ws.append_row([telegram_id, full_name])

def get_today_record(full_name: str):
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    all_records = chrono_ws.get_all_values()
    for idx, row in enumerate(all_records, start=1):
        if idx == 1:  # пропускаем заголовки
            continue
        if len(row) >= 3:
            row_name = row[0]  # столбец A (ФИО)
            row_date = row[2]  # столбец C (дата создания)
            if row_name == full_name and row_date.startswith(today_str):
                return [idx, row[1]]  # возвращаем номер строки и текст из B
    return None

def create_record(full_name: str, text: str):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.append_row([full_name, text, now, ""])

def update_record(row_idx: int, new_text: str):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.update(f"B{row_idx}", new_text)
    chrono_ws.update(f"D{row_idx}", now)

def get_record_by_date(full_name: str, target_date: str):
    all_records = chrono_ws.get_all_values()
    for idx, row in enumerate(all_records, start=1):
        if idx == 1:
            continue
        if len(row) >= 3:
            row_name = row[0]      # A: ФИО
            row_date = row[2]      # C: дата создания, берём только первые 10 символов (YYYY-MM-DD)
            if row_name == full_name and row_date.startswith(target_date):
                return [idx, row[1]]  # возвращаем номер строки и текст из B
    return None

# ========== ХЕНДЛЕРЫ ==========
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    name = get_user_name(tg_id)
    if name:
        await message.answer(f"С возвращением, {name}!", reply_markup=main_kb)
    else:
        await message.answer("Привет! Как тебя зовут? (напиши ФИО)")
        await state.set_state(RegisterState.waiting_for_name)

@dp.message(RegisterState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("Имя не может быть пустым.")
        return
    save_user(message.from_user.id, full_name)
    await message.answer(f"Отлично, {full_name}! Теперь ты можешь вносить часы.", reply_markup=main_kb)
    await state.clear()

@dp.message(lambda msg: msg.text == "✍️ Ввести часы")
async def add_chrono(message: types.Message, state: FSMContext):
    full_name = get_user_name(message.from_user.id)
    if not full_name:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    if get_today_record(full_name):
        await message.answer("❌ Ты уже вводил задачи сегодня. Используй кнопку «Изменить часы».")
        return
    await message.answer("📝 Отправь список задач на сегодня (каждую с новой строки):\nПример:\n1. Общался с ИИ — 2ч\n2. Резал морковь — 24ч")
    await state.set_state(ChronoState.waiting_for_text)
    await state.update_data(full_name=full_name)

@dp.message(ChronoState.waiting_for_text)
async def save_chrono(message: types.Message, state: FSMContext):
    if len(message.text.strip()) < 5:
        await message.answer("Слишком коротко. Напиши хотя бы одно дело.")
        return
    data = await state.get_data()
    create_record(data["full_name"], message.text)
    await message.answer("✅ Запись сохранена!", reply_markup=main_kb)
    await state.clear()

@dp.message(lambda msg: msg.text == "✏️ Изменить часы")
async def edit_chrono(message: types.Message, state: FSMContext):
    full_name = get_user_name(message.from_user.id)
    if not full_name:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    await message.answer("📅 Введи дату в формате ГГГГ-ММ-ДД (например 2026-05-07):")
    await state.set_state(ChronoState.waiting_for_edit_date)
    await state.update_data(full_name=full_name)

@dp.message(ChronoState.waiting_for_edit_date)
async def process_edit_date(msg: types.Message, state: FSMContext):
    date_str = msg.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except:
        await msg.answer("❌ Неверный формат. Пример: 2026-05-07")
        return

    data = await state.get_data()
    name = data["full_name"]
    record = get_record_by_date(name, date_str)

    if not record:
        await msg.answer(f"Нет записей за {date_str}", reply_markup=main_kb)
        await state.clear()
        return

    row_idx, old_text = record
    await msg.answer(f"📋 ТЕКУЩИЙ текст за {date_str}:\n\n{old_text}\n\n✏️ Отправь НОВЫЙ текст:")
    await state.set_state(ChronoState.waiting_for_new_text)
    await state.update_data(row_idx=row_idx)
    print(f"🔍 DEBUG: row_idx={row_idx} сохранён в FSM")  # ← ЛОГ

@dp.message(ChronoState.waiting_for_new_text)
async def save_edited_chrono(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    row_idx = data.get("row_idx")
    print(f"🔍 DEBUG: row_idx из FSM = {row_idx}")  # ← ЛОГ

    if row_idx is None:
        await msg.answer("❌ Ошибка: не найдена строка для редактирования. Попробуй ещё раз.", reply_markup=main_kb)
        await state.clear()
        return

    if len(msg.text.strip()) < 5:
        await msg.answer("Слишком коротко. Отправь нормальный список.")
        return

    try:
        update_record(row_idx, msg.text.strip())
        await msg.answer("✅ Запись обновлена!", reply_markup=main_kb)
        print(f"🔍 DEBUG: Запись {row_idx} успешно обновлена")  # ← ЛОГ
    except Exception as e:
        await msg.answer(f"❌ Ошибка при обновлении: {e}")
        print(f"🔍 DEBUG: Ошибка обновления — {e}")
    finally:
        await state.clear()

# ========== НАПОМИНАНИЯ В 19:00 ==========
async def send_reminders():
    while True:
        now = datetime.now(TIMEZONE)
        target = now.replace(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        
        users = users_ws.get_all_records()
        for user in users:
            tg_id = int(user["telegram_id"])
            full_name = user["full_name"]
            if get_today_record(full_name) is None:
                try:
                    img = random.choice(GALLERY)
                    await bot.send_photo(tg_id, img, caption=f"🔔 Напоминание: внеси часы за сегодня, {full_name}!")
                    await bot.send_message(tg_id, "Используй кнопки ниже:", reply_markup=main_kb)
                except Exception as e:
                    print(f"Ошибка напоминания {full_name}: {e}")

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
from aiohttp import web

async def health_check(request):
    """Эндпоинт для проверки работоспособности"""
    return web.Response(text="Бот работает!")

async def start_web_server():
    """Запускает минимальный веб-сервер на порту 8080"""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("✅ Веб-сервер запущен на порту 8080")

# ========== ЗАПУСК ==========
async def main():
    # Запускаем веб-сервер для Render
    await start_web_server()
    
    # Запускаем напоминания
    asyncio.create_task(send_reminders())
    
    print("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
