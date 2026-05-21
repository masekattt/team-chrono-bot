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

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
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

SHEET_ID = "1A2b3C4d5E6f7G8h9I0j"  # ⚠️ ЗАМЕНИТЕ НА ВАШ ID ТАБЛИЦЫ

TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 19
REMINDER_MINUTE = 0

GALLERY = [
    "https://images.unsplash.com/photo-1506784983877-45594efa4cbe",
]

# ========== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==========
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

creds_json = os.environ.get("CREDENTIALS_JSON")
if creds_json:
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)

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

# Клавиатура для выбора даты (сегодня/вчера/другая)
date_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Сегодня")],
        [KeyboardButton(text="📆 Вчера")],
        [KeyboardButton(text="📅 Другая дата")]
    ],
    resize_keyboard=True
)

# ========== ШАБЛОНЫ ИЗ GOOGLE TABLES ==========
TEMPLATES_SHEET_NAME = "Templates"

def load_templates_from_sheet():
    """Загружает шаблоны из Google Таблицы (колонка 'name')"""
    try:
        templates_ws = sheet.worksheet(TEMPLATES_SHEET_NAME)
        records = templates_ws.get_all_records()
        templates = []
        for row in records:
            name = row.get("name", "").strip()
            if name:
                templates.append(name)
        if not templates:
            templates = ["Очная встреча", "Проверка багов", "Написание кода"]
        return templates
    except Exception as e:
        print(f"Ошибка загрузки шаблонов: {e}")
        return ["Очная встреча", "Проверка багов", "Написание кода"]

def get_template_keyboard():
    """Создаёт клавиатуру с шаблонами задач"""
    templates = load_templates_from_sheet()
    keyboard = []
    row = []
    for i, template in enumerate(templates):
        row.append(KeyboardButton(text=template))
        if len(row) == 2 or i == len(templates) - 1:
            keyboard.append(row)
            row = []
    
    keyboard.append([KeyboardButton(text="✅ Готово")])
    keyboard.append([KeyboardButton(text="❌ Отмена")])
    
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_which_day = State()       # Выбор даты (сегодня/вчера/другая)
    waiting_for_custom_date = State()     # Ввод произвольной даты
    waiting_for_edit_date = State()       # Ввод даты для редактирования
    waiting_for_template_builder = State() # Выбор шаблонов
    waiting_for_hours_input = State()     # Ввод часов

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
    """Создаёт запись. Если date_override указан, использует его"""
    if date_override:
        dt = datetime.strptime(date_override, "%Y-%m-%d")
        record_datetime = dt.replace(hour=23, minute=59, second=59)
        record_str = record_datetime.strftime("%Y-%m-%d %H:%M:%S")
    else:
        record_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    
    chrono_ws.append_row([name, text, record_str, ""])

def update_record(row_idx, new_text):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.update([[new_text]], f"B{row_idx}")
    chrono_ws.update([[now]], f"D{row_idx}")

# ========== ХЕНДЛЕРЫ ==========
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------- РЕГИСТРАЦИЯ ----------
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

# ---------- ВВОД ЧАСОВ ----------
@dp.message(lambda m: m.text == "✍️ Ввести часы")
async def add_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    # Проверяем, есть ли уже запись за сегодня
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    existing = get_record_by_date_and_name(name, today)
    if existing:
        await msg.answer("❌ За сегодня уже введено. Используй «✏️ Изменить часы»")
        return
    
    await msg.answer(
        "📅 За какую дату хочешь внести часы?",
        reply_markup=date_choice_kb
    )
    await state.set_state(ChronoState.waiting_for_which_day)
    await state.update_data(full_name=name, is_edit=False, accumulated_text="")

@dp.message(ChronoState.waiting_for_which_day)
async def process_date_choice(msg: types.Message, state: FSMContext):
    choice = msg.text
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    data = await state.get_data()
    name = data["full_name"]
    
    if choice == "✅ Сегодня":
        target_date = today
        await proceed_with_date(msg, state, target_date, name)
    elif choice == "📆 Вчера":
        target_date = yesterday
        await proceed_with_date(msg, state, target_date, name)
    elif choice == "📅 Другая дата":
        await msg.answer(
            "📅 Введи дату в формате ГГГГ-ММ-ДД (например 2026-05-21):",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_custom_date)
    else:
        await msg.answer("❌ Используй кнопки для выбора даты")
        return

@dp.message(ChronoState.waiting_for_custom_date)
async def process_custom_date(msg: types.Message, state: FSMContext):
    date_str = msg.text.strip()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except:
        await msg.answer("❌ Неверный формат. Используй ГГГГ-ММ-ДД")
        return
    
    if date_str > today:
        await msg.answer("❌ Нельзя вводить часы за будущие даты", reply_markup=main_kb)
        await state.clear()
        return
    
    data = await state.get_data()
    name = data["full_name"]
    await proceed_with_date(msg, state, date_str, name)

async def proceed_with_date(msg: types.Message, state: FSMContext, target_date: str, name: str):
    """Продолжает после выбора даты: проверяет дубликат и показывает шаблоны"""
    # Проверяем, нет ли уже записи за эту дату
    existing = get_record_by_date_and_name(name, target_date)
    if existing:
        await msg.answer(
            f"❌ Запись за {target_date} уже существует. Используй «✏️ Изменить часы»",
            reply_markup=main_kb
        )
        await state.clear()
        return
    
    await state.update_data(target_date=target_date)
    await msg.answer(
        f"📅 *{target_date}*\n\n"
        f"➕ Нажимай на кнопки с задачами.\n"
        f"После каждой кнопки введи количество часов.\n"
        f"Когда закончишь — нажми «✅ Готово»",
        parse_mode="Markdown",
        reply_markup=get_template_keyboard()
    )
    await state.set_state(ChronoState.waiting_for_template_builder)

# ---------- ИЗМЕНЕНИЕ ЧАСОВ ----------
@dp.message(lambda m: m.text == "✏️ Изменить часы")
async def edit_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    await msg.answer(
        "📅 Введи дату в формате ГГГГ-ММ-ДД (например 2026-05-21):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(ChronoState.waiting_for_edit_date)
    await state.update_data(full_name=name)

@dp.message(ChronoState.waiting_for_edit_date)
async def process_date_for_edit(msg: types.Message, state: FSMContext):
    date_str = msg.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except:
        await msg.answer("❌ Неверный формат. Используй ГГГГ-ММ-ДД")
        return
    
    data = await state.get_data()
    name = data["full_name"]
    
    record = get_record_by_date_and_name(name, date_str)
    if not record:
        await msg.answer(f"❌ Нет записей за {date_str}", reply_markup=main_kb)
        await state.clear()
        return
    
    row_idx, old_text = record
    await state.update_data(
        target_date=date_str,
        row_idx=row_idx,
        old_text=old_text,
        is_edit=True,
        accumulated_text=old_text + "\n" if old_text else ""
    )
    
    await msg.answer(
        f"📅 *{date_str}*\n\n"
        f"📋 *Текущий текст:*\n{old_text}\n\n"
        f"➕ Нажимай на кнопки, чтобы добавить новые задачи.\n"
        f"После каждой кнопки введи количество часов.\n"
        f"Когда закончишь — нажми «✅ Готово»",
        parse_mode="Markdown",
        reply_markup=get_template_keyboard()
    )
    await state.set_state(ChronoState.waiting_for_template_builder)

# ---------- КОНСТРУКТОР ЗАДАЧ ----------
@dp.message(ChronoState.waiting_for_template_builder)
async def template_handler(msg: types.Message, state: FSMContext):
    templates = load_templates_from_sheet()
    
    # Готово — сохраняем
    if msg.text == "✅ Готово":
        data = await state.get_data()
        final_text = data.get("accumulated_text", "").strip()
        
        if not final_text:
            await msg.answer("❌ Не добавлено ни одной задачи. Нажми на шаблон, чтобы добавить.")
            return
        
        name = data["full_name"]
        target_date = data["target_date"]
        
        if data.get("is_edit"):
            update_record(data["row_idx"], final_text)
            await msg.answer(f"✅ Запись за {target_date} обновлена!", reply_markup=main_kb)
        else:
            create_record(name, final_text, date_override=target_date)
            await msg.answer(f"✅ Запись за {target_date} сохранена!", reply_markup=main_kb)
        
        await state.clear()
        return
    
    # Отмена
    if msg.text == "❌ Отмена":
        await msg.answer("❌ Ввод отменён", reply_markup=main_kb)
        await state.clear()
        return
    
    # Нажатие на шаблон
    if msg.text in templates:
        await state.update_data(current_template=msg.text)
        await msg.answer(
            f"✏️ Введи количество часов для: *{msg.text}*",
            parse_mode="Markdown",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_hours_input)
    else:
        await msg.answer("❌ Нажми на кнопку с названием задачи")

@dp.message(ChronoState.waiting_for_hours_input)
async def hours_input_handler(msg: types.Message, state: FSMContext):
    hours = msg.text.strip()
    
    # Валидация числа
    try:
        hours_clean = hours.replace(',', '.')
        hours_float = float(hours_clean)
        if hours_float <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи корректное число часов (например: 2, 1.5, 3.75)")
        return
    
    data = await state.get_data()
    template = data.get("current_template")
    
    if not template:
        await msg.answer(
            "❌ Что-то пошло не так. Пожалуйста, выбери задачу заново:",
            reply_markup=get_template_keyboard()
        )
        await state.set_state(ChronoState.waiting_for_template_builder)
        return
    
    accumulated = data.get("accumulated_text", "")
    
    # Нумерация
    lines = accumulated.strip().split('\n') if accumulated else []
    next_num = len([l for l in lines if l.strip()]) + 1
    
    hours_str = str(hours_float).replace('.', ',')
    new_line = f"{next_num}. {template} — {hours_str} часа"
    
    if accumulated:
        accumulated += "\n" + new_line
    else:
        accumulated = new_line
    
    await state.update_data(accumulated_text=accumulated, current_template=None)
    
    await msg.answer(
        f"📝 *Текущий список:*\n{accumulated}\n\n"
        f"✅ Добавлено! Выбери следующую задачу или нажми «Готово»:",
        parse_mode="Markdown",
        reply_markup=get_template_keyboard()
    )
    await state.set_state(ChronoState.waiting_for_template_builder)

# ---------- НАПОМИНАНИЯ ----------
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
            except Exception as e:
                print(f"Ошибка напоминания: {e}")

# ---------- ЗАПУСК ----------
async def main():
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    asyncio.create_task(reminder_loop())
    print("✅ Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
