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
    return "Bot is working", 200

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port)

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN is not set")

SHEET_ID = "1A2b3C4d5E6f7G8h9I0j"

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
        [KeyboardButton(text="Ввести часы")],
        [KeyboardButton(text="Изменить часы")]
    ],
    resize_keyboard=True
)

date_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня")],
        [KeyboardButton(text="Вчера")],
        [KeyboardButton(text="Другая дата")]
    ],
    resize_keyboard=True
)

def get_main_list_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить задачу")],
            [KeyboardButton(text="Редактировать строку")],
            [KeyboardButton(text="Удалить строку")],
            [KeyboardButton(text="Готово")],
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )

def get_add_method_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Шаблон")],
            [KeyboardButton(text="Свой текст")],
            [KeyboardButton(text="Назад к списку")]
        ],
        resize_keyboard=True
    )

# ========== ШАБЛОНЫ ==========
TEMPLATES_SHEET_NAME = "Templates"

def load_templates_from_sheet():
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
    templates = load_templates_from_sheet()
    keyboard = []
    row = []
    for i, template in enumerate(templates):
        row.append(KeyboardButton(text=template))
        if len(row) == 2 or i == len(templates) - 1:
            keyboard.append(row)
            row = []
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_date = State()
    waiting_for_custom_date = State()
    waiting_for_edit_date = State()
    waiting_for_main = State()
    waiting_for_add_method = State()
    waiting_for_template_hours = State()
    waiting_for_custom_text = State()
    waiting_for_edit_line_num = State()
    waiting_for_edit_line_text = State()
    waiting_for_delete_line_num = State()

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
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        created_date = str(row.get("Дата и время создания", "")).split()[0]
        if row.get("ФИО") == name and created_date == target_date:
            return idx, row.get("Текст", "")
    return None

def create_record(name, text, date_override=None):
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

def add_line_to_list(accumulated, task_name, hours_str):
    lines = accumulated.strip().split('\n') if accumulated else []
    next_num = len([l for l in lines if l.strip()]) + 1
    new_line = f"{next_num}. {task_name} - {hours_str} часа"
    return new_line

async def show_main_menu(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "").strip()
    
    if not accumulated:
        await msg.answer(
            "Список задач пуст. Нажми Добавить задачу",
            reply_markup=get_main_list_keyboard()
        )
    else:
        await msg.answer(
            f"Текущий список:\n{accumulated}\n\nВыбери действие:",
            reply_markup=get_main_list_keyboard()
        )

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
        await msg.answer("Привет! Как тебя зовут?")
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
@dp.message(lambda m: m.text == "Ввести часы")
async def add_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    existing = get_record_by_date_and_name(name, today)
    if existing:
        await msg.answer("За сегодня уже введено. Используй Изменить часы")
        return
    
    await msg.answer("Выбери дату:", reply_markup=date_choice_kb)
    await state.set_state(ChronoState.waiting_for_date)
    await state.update_data(full_name=name, is_edit=False, accumulated_text="")

@dp.message(ChronoState.waiting_for_date)
async def process_date_choice(msg: types.Message, state: FSMContext):
    choice = msg.text
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    data = await state.get_data()
    name = data["full_name"]
    
    if choice == "Сегодня":
        target_date = today
    elif choice == "Вчера":
        target_date = yesterday
    elif choice == "Другая дата":
        await msg.answer(
            "Введи дату в формате ГГГГ-ММ-ДД:",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_custom_date)
        return
    else:
        await msg.answer("Используй кнопки")
        return
    
    existing = get_record_by_date_and_name(name, target_date)
    if existing:
        await msg.answer(f"Запись за {target_date} уже существует", reply_markup=main_kb)
        await state.clear()
        return
    
    await state.update_data(target_date=target_date)
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

@dp.message(ChronoState.waiting_for_custom_date)
async def process_custom_date(msg: types.Message, state: FSMContext):
    date_str = msg.text.strip()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except:
        await msg.answer("Неверный формат. Используй ГГГГ-ММ-ДД")
        return
    
    if date_str > today:
        await msg.answer("Нельзя вводить часы за будущие даты", reply_markup=main_kb)
        await state.clear()
        return
    
    data = await state.get_data()
    name = data["full_name"]
    
    existing = get_record_by_date_and_name(name, date_str)
    if existing:
        await msg.answer(f"Запись за {date_str} уже существует", reply_markup=main_kb)
        await state.clear()
        return
    
    await state.update_data(target_date=date_str)
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

# ---------- ИЗМЕНЕНИЕ ЧАСОВ ----------
@dp.message(lambda m: m.text == "Изменить часы")
async def edit_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    await msg.answer(
        "Введи дату в формате ГГГГ-ММ-ДД:",
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
        await msg.answer("Неверный формат. Используй ГГГГ-ММ-ДД")
        return
    
    data = await state.get_data()
    name = data["full_name"]
    
    record = get_record_by_date_and_name(name, date_str)
    if not record:
        await msg.answer(f"Нет записей за {date_str}", reply_markup=main_kb)
        await state.clear()
        return
    
    row_idx, old_text = record
    await state.update_data(
        target_date=date_str,
        row_idx=row_idx,
        is_edit=True,
        accumulated_text=old_text + "\n" if old_text else ""
    )
    
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

# ---------- ОСНОВНОЕ МЕНЮ ----------
@dp.message(ChronoState.waiting_for_main)
async def main_list_handler(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    
    if msg.text == "Готово":
        final_text = data.get("accumulated_text", "").strip()
        if not final_text:
            await msg.answer("Список пуст. Добавь хотя бы одну задачу.")
            return
        
        name = data["full_name"]
        target_date = data["target_date"]
        
        if data.get("is_edit"):
            update_record(data["row_idx"], final_text)
            await msg.answer(f"Запись за {target_date} обновлена!", reply_markup=main_kb)
        else:
            create_record(name, final_text, date_override=target_date)
            await msg.answer(f"Запись за {target_date} сохранена!", reply_markup=main_kb)
        
        await state.clear()
        return
    
    if msg.text == "Отмена":
        await msg.answer("Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    if msg.text == "Добавить задачу":
        await msg.answer(
            "Выбери способ:",
            reply_markup=get_add_method_keyboard()
        )
        await state.set_state(ChronoState.waiting_for_add_method)
        return
    
    if msg.text == "Редактировать строку":
        accumulated = data.get("accumulated_text", "")
        lines = [l for l in accumulated.split('\n') if l.strip()]
        
        if not lines:
            await msg.answer("Нет строк для редактирования")
            await show_main_menu(msg, state)
            return
        
        numbered = "\n".join([f"{i+1}. {line}" for i, line in enumerate(lines)])
        await msg.answer(
            f"Введи номер строки для редактирования:\n{numbered}",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_edit_line_num)
        return
    
    if msg.text == "Удалить строку":
        accumulated = data.get("accumulated_text", "")
        lines = [l for l in accumulated.split('\n') if l.strip()]
        
        if not lines:
            await msg.answer("Нет строк для удаления")
            await show_main_menu(msg, state)
            return
        
        numbered = "\n".join([f"{i+1}. {line}" for i, line in enumerate(lines)])
        await msg.answer(
            f"Введи номер строки для удаления:\n{numbered}",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_delete_line_num)
        return
    
    await msg.answer("Используй кнопки", reply_markup=get_main_list_keyboard())

# ---------- ДОБАВЛЕНИЕ ЗАДАЧ ----------
@dp.message(ChronoState.waiting_for_add_method)
async def add_method_handler(msg: types.Message, state: FSMContext):
    if msg.text == "Назад к списку":
        await show_main_menu(msg, state)
        await state.set_state(ChronoState.waiting_for_main)
        return
    
    if msg.text == "Шаблон":
        await msg.answer("Выбери шаблон:", reply_markup=get_template_keyboard())
        await state.set_state(ChronoState.waiting_for_template_hours)
        return
    
    if msg.text == "Свой текст":
        await msg.answer(
            "Напиши задачу в формате: Название - часы\n"
            "Пример: Документация - 1.5\n\n"
            "Можно написать несколько задач, каждую с новой строки.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(ChronoState.waiting_for_custom_text)
        return

@dp.message(ChronoState.waiting_for_template_hours)
async def template_choice_handler(msg: types.Message, state: FSMContext):
    templates = load_templates_from_sheet()
    
    if msg.text in templates:
        await state.update_data(current_template=msg.text)
        await msg.answer(f"Введи часы для: {msg.text}")
        # Остаёмся в том же состоянии, но теперь ждём число
        return
    
    # Если это число (часы) для предыдущего шаблона
    try:
        hours_clean = msg.text.replace(',', '.')
        hours_float = float(hours_clean)
        if hours_float <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("Выбери шаблон или введи число часов", reply_markup=get_template_keyboard())
        return
    
    # Получаем сохранённый шаблон
    data = await state.get_data()
    template = data.get("current_template")
    if not template:
        await msg.answer("Сначала выбери шаблон", reply_markup=get_template_keyboard())
        return
    
    # Добавляем строку
    accumulated = data.get("accumulated_text", "")
    hours_str = str(hours_float).replace('.', ',')
    new_line = add_line_to_list(accumulated, template, hours_str)
    
    if accumulated:
        accumulated += "\n" + new_line
    else:
        accumulated = new_line
    
    await state.update_data(accumulated_text=accumulated, current_template=None)
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

@dp.message(ChronoState.waiting_for_custom_text)
async def custom_text_handler(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Слишком коротко")
        return
    
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    
    # Разбиваем на строки
    lines = accumulated.strip().split('\n') if accumulated else []
    next_num = len([l for l in lines if l.strip()]) + 1
    
    custom_lines = text.split('\n')
    new_lines = []
    for line in custom_lines:
        if line.strip():
            new_lines.append(f"{next_num}. {line.strip()}")
            next_num += 1
    
    if accumulated:
        accumulated += "\n" + "\n".join(new_lines)
    else:
        accumulated = "\n".join(new_lines)
    
    await state.update_data(accumulated_text=accumulated)
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

# ---------- РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ ----------
@dp.message(ChronoState.waiting_for_edit_line_num)
async def edit_line_num_handler(msg: types.Message, state: FSMContext):
    try:
        line_num = int(msg.text.strip()) - 1
    except:
        await msg.answer("Введи номер строки")
        return
    
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    lines = [l for l in accumulated.split('\n') if l.strip()]
    
    if line_num < 0 or line_num >= len(lines):
        await msg.answer(f"Строка {line_num+1} не найдена. Всего строк: {len(lines)}")
        return
    
    await state.update_data(edit_line_num=line_num)
    await msg.answer(
        f"Текущая строка:\n{lines[line_num]}\n\nВведи новый текст:"
    )
    await state.set_state(ChronoState.waiting_for_edit_line_text)

@dp.message(ChronoState.waiting_for_edit_line_text)
async def edit_line_text_handler(msg: types.Message, state: FSMContext):
    new_text = msg.text.strip()
    if len(new_text) < 3:
        await msg.answer("Слишком коротко")
        return
    
    data = await state.get_data()
    line_num = data.get("edit_line_num")
    accumulated = data.get("accumulated_text", "")
    lines = [l for l in accumulated.split('\n') if l.strip()]
    
    lines[line_num] = new_text
    new_accumulated = "\n".join(lines)
    await state.update_data(accumulated_text=new_accumulated)
    
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

@dp.message(ChronoState.waiting_for_delete_line_num)
async def delete_line_num_handler(msg: types.Message, state: FSMContext):
    try:
        line_num = int(msg.text.strip()) - 1
    except:
        await msg.answer("Введи номер строки")
        return
    
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    lines = [l for l in accumulated.split('\n') if l.strip()]
    
    if line_num < 0 or line_num >= len(lines):
        await msg.answer(f"Строка {line_num+1} не найдена. Всего строк: {len(lines)}")
        return
    
    deleted = lines.pop(line_num)
    
    # Перенумеровываем
    renumbered = []
    for i, line in enumerate(lines, 1):
        parts = line.split('. ', 1)
        if len(parts) > 1:
            renumbered.append(f"{i}. {parts[1]}")
        else:
            renumbered.append(f"{i}. {line}")
    
    new_accumulated = "\n".join(renumbered)
    await state.update_data(accumulated_text=new_accumulated)
    
    await msg.answer(f"Удалено: {deleted}")
    await show_main_menu(msg, state)
    await state.set_state(ChronoState.waiting_for_main)

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
                    await bot.send_photo(tg_id, img, caption=f"Напоминание {name}, внеси часы за сегодня!")
                    await bot.send_message(tg_id, "Нажми Ввести часы", reply_markup=main_kb)
            except Exception as e:
                print(f"Ошибка напоминания: {e}")

# ---------- ЗАПУСК ----------
async def main():
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    asyncio.create_task(reminder_loop())
    print("Бот и веб-сервер запущены")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
