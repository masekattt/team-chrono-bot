import os
import random
import asyncio
import gspread
from datetime import datetime, time, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from oauth2client.service_account import ServiceAccountCredentials
import pytz

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = "8640718496:AAGR9CZIzl1NFn4RPEoquqZAF65nQeOiBNw"
SHEET_ID = "1pKNd-VfzUEK3A7D-p1lZTGpvZHtO7UPIRevn4YZqSSQ"  # Из URL: https://docs.google.com/spreadsheets/d/ЭТОТ_ID/edit
TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 19
REMINDER_MINUTE = 0

# Галерея картинок (замените ссылки на свои)
GALLERY = [
    "https://example.com/image1.jpg",
    "https://example.com/image2.jpg",
    "https://example.com/image3.jpg",
]

# ========== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==========
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(r"C:\zxcvcxz\credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID)
users_ws = sheet.worksheet("Users")      # Лист с колонками: telegram_id, full_name
chrono_ws = sheet.worksheet("Chrono")    # Лист с колонками: ФИО, Текст, Дата_создания, Дата_изменения

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

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_user_name(telegram_id: int) -> str | None:
    """Возвращает ФИО пользователя по telegram_id из листа Users"""
    try:
        cell = users_ws.find(str(telegram_id))
        return users_ws.cell(cell.row, 2).value
    except:
        return None

def save_user(telegram_id: int, full_name: str):
    """Сохраняет нового пользователя в лист Users"""
    users_ws.append_row([telegram_id, full_name])

def get_today_record(full_name: str) -> list | None:
    """Проверяет, есть ли запись за сегодня (только по дате, без времени)"""
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):  # start=2 пропускает заголовки
        if row["ФИО"] == full_name and row["Дата и время создания"].startswith(today_str):
            return [idx, row["Текст"]]
    return None

def create_record(full_name: str, text: str):
    """Добавляет новую запись в Chrono"""
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.append_row([full_name, text, now, ""])

def update_record(row_idx: int, new_text: str):
    """Обновляет существующую запись: Текст и Дата_изменения"""
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.update(f"B{row_idx}", new_text)
    chrono_ws.update(f"D{row_idx}", now)

def get_record_by_date(full_name: str, target_date: str) -> tuple[int, str] | None:
    """Ищет запись по ФИО и конкретной дате (YYYY-MM-DD)"""
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        if row["ФИО"] == full_name and row["Дата и время создания"].startswith(target_date):
            return idx, row["Текст"]
    return None

async def send_reminders(bot: Bot):
    """Отправляет напоминание в 19:00 всем, у кого нет записи за сегодня"""
    now_moscow = datetime.now(TIMEZONE)
    if now_moscow.hour == REMINDER_HOUR and now_moscow.minute == REMINDER_MINUTE:
        users = users_ws.get_all_records()
        for user in users:
            tg_id = int(user["telegram_id"])
            full_name = user["full_name"]
            if get_today_record(full_name) is None:
                img = random.choice(GALLERY)
                await bot.send_photo(tg_id, img, caption=f"🔔 Напоминание: внеси часы за сегодня, {full_name}!")
                await bot.send_message(tg_id, "Используй кнопки ниже:", reply_markup=main_kb)

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
        await message.answer("Имя не может быть пустым. Напиши, пожалуйста.")
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
        await message.answer("Слишком коротко. Напиши хотя бы одно дело с часами.")
        return
    data = await state.get_data()
    create_record(data["full_name"], message.text)
    await message.answer("✅ Запись сохранена!")
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
async def process_edit_date(message: types.Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except:
        await message.answer("❌ Неверный формат. Используй ГГГГ-ММ-ДД")
        return
    data = await state.get_data()
    full_name = data["full_name"]
    record = get_record_by_date(full_name, date_str)
    if not record:
        await message.answer(f"❌ Нет записей за {date_str}.")
        await state.clear()
        return
    row_idx, old_text = record
    await message.answer(f"📋 Текущий текст за {date_str}:\n\n{old_text}\n\n✏️ Отправь новый текст (многострочный):")
    await state.set_state(ChronoState.waiting_for_new_text)
    await state.update_data(row_idx=row_idx)

@dp.message(ChronoState.waiting_for_new_text)
async def save_edited_chrono(message: types.Message, state: FSMContext):
    if len(message.text.strip()) < 5:
        await message.answer("Слишком коротко. Напиши нормальный список задач.")
        return
    data = await state.get_data()
    update_record(data["row_idx"], message.text)
    await message.answer("✅ Запись обновлена!")
    await state.clear()

# ========== ЗАПУСК НАПОМИНАНИЙ ==========
async def reminder_loop():
    await bot.send_message(chat_id="259122642", text="Бот запущен и ждёт 19:00")  # Замените на ваш ID
    while True:
        now_moscow = datetime.now(TIMEZONE)
        next_reminder = datetime.now(TIMEZONE).replace(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, second=0, microsecond=0)
        if now_moscow >= next_reminder:
            next_reminder += timedelta(days=1)
        sleep_seconds = (next_reminder - now_moscow).total_seconds()
        await asyncio.sleep(sleep_seconds)
        await send_reminders(bot)

async def main():
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())