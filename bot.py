import os
import random
import asyncio
import gspread
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from oauth2client.service_account import ServiceAccountCredentials
import pytz
import json

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER (ЧТОБЫ НЕ БЫЛО TIMEOUT) ==========
from flask import Flask
import threading

web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Бот работает!", 200

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port)

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

SHEET_ID = "1pKNd-VfzUEK3A7D-p1lZTGpvZHtO7UPIRevn4YZqSSQ"  # ⚠️ ЗАМЕНИТЕ НА ВАШ ID ТАБЛИЦЫ

TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 19
REMINDER_MINUTE = 0

# ГАЛЕРЕЯ КАРТИНОК ДЛЯ НАПОМИНАНИЙ (замените ссылки)
GALLERY = [
    "https://images.unsplash.com/photo-1506784983877-45594efa4cbe",
    "https://images.unsplash.com/photo-1506784983877-45594efa4cbe",
    "https://images.unsplash.com/photo-1506784983877-45594efa4cbe",
]

# ========== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==========
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

creds_json = os.environ.get("CREDENTIALS_JSON")
if creds_json:
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    print("✅ Credentials из CREDENTIALS_JSON")
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    print("✅ Credentials из файла")

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

# Клавиатура для выбора: сегодня или вчера
choose_day_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Сегодня")],
        [KeyboardButton(text="📆 Вчера")],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_which_day = State()      # Сегодня или вчера?
    waiting_for_text = State()           # Ожидание текста
    waiting_for_edit_date = State()      # Выбор даты для редактирования
    waiting_for_edit_text = State()      # Ожидание нового текста

# ========== РАБОТА С ТАБЛИЦЕЙ ==========
def get_user_name(tg_id):
    try:
        cell = users_ws.find(str(tg_id))
        return users_ws.cell(cell.row, 2).value
    except:
        return None

def save_user(tg_id, name):
    users_ws.append_row([tg_id, name])

def get_record_by_date_and_name(name, target_date):
    """Возвращает (row_idx, текст) если есть запись за указанную дату"""
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        created_date = str(row.get("Дата и время создания", "")).split()[0]
        if row.get("ФИО") == name and created_date == target_date:
            return idx, row.get("Текст", "")
    return None

def create_record(name, text, date_override=None):
    """Создаёт запись. Если date_override указан (YYYY-MM-DD), использует его для даты создания"""
    if date_override:
        # Для заполнения за вчера — ставим дату вчера, время 23:59:59
        dt = datetime.strptime(date_override, "%Y-%m-%d")
        record_datetime = dt.replace(hour=23, minute=59, second=59)
        record_str = record_datetime.strftime("%Y-%m-%d %H:%M:%S")
    else:
        record_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    
    chrono_ws.append_row([name, text, record_str, ""])

def update_record(row_idx, new_text):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    # Правильный порядок: сначала значения, потом диапазон
    chrono_ws.update([[new_text]], f"B{row_idx}")
    chrono_ws.update([[now]], f"D{row_idx}")

def get_all_user_dates(name):
    """Возвращает список всех дат, за которые у пользователя есть записи"""
    records = chrono_ws.get_all_records()
    dates = []
    for row in records:
        if row.get("ФИО") == name:
            date_str = str(row.get("Дата и время создания", "")).split()[0]
            if date_str and date_str not in dates:
                dates.append(date_str)
    return sorted(dates, reverse=True)

# ========== ХЕНДЛЕРЫ ==========
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    name = get_user_name(tg_id)
    if name:
        await msg.answer(f"С возвращением, {name}!", reply_markup=main_kb)
    else:
        await msg.answer("Привет! Как тебя зовут? (напиши ФИО)")
        await state.set_state(RegisterState.waiting_for_name)

@dp.message(RegisterState.waiting_for_name)
async def process_name(msg: types.Message, state: FSMContext):
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым")
        return
    save_user(msg.from_user.id, name)
    await msg.answer(f"Принято, {name}!", reply_markup=main_kb)
    await state.clear()

@dp.message(lambda m: m.text == "✍️ Ввести часы")
async def add_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    await state.update_data(full_name=name)
    await msg.answer(
        "📅 За какую дату хочешь внести часы?",
        reply_markup=choose_day_kb
    )
    await state.set_state(ChronoState.waiting_for_which_day)

@dp.message(ChronoState.waiting_for_which_day)
async def add_chrono_choose_day(msg: types.Message, state: FSMContext):
    choice = msg.text
    data = await state.get_data()
    name = data["full_name"]
    
    if choice == "❌ Отмена":
        await msg.answer("❌ Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    # Определяем дату
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    if choice == "✅ Сегодня":
        target_date = today
    elif choice == "📆 Вчера":
        target_date = yesterday
    else:
        await msg.answer("Пожалуйста, используй кнопки")
        return
    
    # Проверяем, есть ли уже запись за эту дату
    existing = get_record_by_date_and_name(name, target_date)
    if existing:
        row_idx, old_text = existing
        await msg.answer(
            f"❌ Запись за {target_date} уже существует:\n\n{old_text}\n\n"
            f"Используй кнопку «✏️ Изменить часы», чтобы отредактировать.",
            reply_markup=main_kb
        )
        await state.clear()
        return
    
    # Сохраняем дату и переходим к вводу текста
    await state.update_data(target_date=target_date)
    await msg.answer(
        f"📝 Отправь список задач за {target_date} (многострочный):\n\n"
        f"Пример:\n1. Общался с ИИ — 2ч\n2. Резал морковь — 24ч",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(ChronoState.waiting_for_text)

@dp.message(ChronoState.waiting_for_text)
async def save_chrono(msg: types.Message, state: FSMContext):
    if len(msg.text.strip()) < 5:
        await msg.answer("❌ Слишком коротко. Напиши хотя бы одно дело с часами.")
        return
    
    data = await state.get_data()
    name = data["full_name"]
    target_date = data.get("target_date")  # Может быть None для обычного ввода (но у нас теперь всегда есть)
    
    if target_date:
        create_record(name, msg.text, date_override=target_date)
        await msg.answer(f"✅ Запись за {target_date} сохранена!", reply_markup=main_kb)
    else:
        create_record(name, msg.text)
        await msg.answer(f"✅ Запись сохранена!", reply_markup=main_kb)
    
    await state.clear()

@dp.message(lambda m: m.text == "✏️ Изменить часы")
async def edit_chrono_choose_date(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    dates = get_all_user_dates(name)
    if not dates:
        await msg.answer("❌ У тебя пока нет записей. Сначала введи часы через «✍️ Ввести часы»")
        return
    
    # Создаём кнопки с датами (по 3 в ряд)
    keyboard = []
    row = []
    for date in dates[:12]:  # Показываем последние 12 дат, чтобы не загромождать
        row.append(InlineKeyboardButton(text=date, callback_data=f"edit_{date}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="edit_cancel")])
    
    await msg.answer("📅 Выбери дату для редактирования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.update_data(full_name=name)

@dp.callback_query(lambda c: c.data and c.data.startswith("edit_"))
async def process_edit_date(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # Защита от повторного вызова
    current_state = await state.get_state()
    if current_state == ChronoState.waiting_for_edit_text:
        await callback.message.answer("⏳ Ты уже редактируешь запись. Отправь новый текст.")
        return
    
    if callback.data == "edit_cancel":
        await callback.message.edit_text("❌ Отменено", reply_markup=None)
        await callback.message.answer("Главное меню:", reply_markup=main_kb)
        await state.clear()
        return
    
    date_str = callback.data.replace("edit_", "")
    data = await state.get_data()
    name = data.get("full_name") or get_user_name(callback.from_user.id)
    
    record = get_record_by_date_and_name(name, date_str)
    if not record:
        await callback.message.edit_text(f"❌ Ошибка: нет записей за {date_str}")
        return
    
    row_idx, old_text = record
    
    await callback.message.edit_text(
        f"📅 *{date_str}*\n\n"
        f"📋 *Текущий текст:*\n{old_text}\n\n"
        f"✏️ Отправь **НОВЫЙ** текст (многострочный):",
        parse_mode="Markdown"
    )
    
    await state.set_state(ChronoState.waiting_for_edit_text)
    await state.update_data(row_idx=row_idx, date_str=date_str)

@dp.message(ChronoState.waiting_for_edit_text)
async def save_edited_chrono(msg: types.Message, state: FSMContext):
    if len(msg.text.strip()) < 5:
        await msg.answer("❌ Слишком коротко. Отправь нормальный список задач.")
        return
    
    data = await state.get_data()
    update_record(data["row_idx"], msg.text)
    
    await msg.answer(f"✅ Запись за {data['date_str']} обновлена!", reply_markup=main_kb)
    await state.clear()

# ========== НАПОМИНАНИЯ ==========
async def reminder_loop():
    while True:
        now = datetime.now(TIMEZONE)
        target = now.replace(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        
        today_str = now.strftime("%Y-%m-%d")
        users = users_ws.get_all_records()
        
        for user in users:
            try:
                tg_id = int(user["telegram_id"])
                name = user["full_name"]
                existing = get_record_by_date_and_name(name, today_str)
                
                if not existing:
                    img = random.choice(GALLERY)
                    await bot.send_photo(tg_id, img, caption=f"🔔 {name}, внеси часы за сегодня!")
                    await bot.send_message(tg_id, "Нажми «✍️ Ввести часы»", reply_markup=main_kb)
                else:
                    # Уже ввёл — не беспокоим
                    pass
            except Exception as e:
                print(f"Ошибка напоминания: {e}")

# ========== ЗАПУСК ==========
async def main():
    # Запускаем веб-сервер для Render в фоновом потоке
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    # Запускаем напоминания
    asyncio.create_task(reminder_loop())
    
    print("✅ Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
