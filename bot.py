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
import re

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
    raise ValueError("BOT_TOKEN не установлен")

SHEET_ID = "1pKNd-VfzUEK3A7D-p1lZTGpvZHtO7UPIRevn4YZqSSQ"
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
    print("Учетные данные из CREDENTIALS_JSON")
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    print("Учетные данные из файла")

client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID)
users_ws = sheet.worksheet("Users")
chrono_ws = sheet.worksheet("Chrono")
templates_ws = sheet.worksheet("Templates")

# ========== КНОПКИ ==========
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Ввести часы")],
        [KeyboardButton(text="✏️ Изменить часы")]
    ],
    resize_keyboard=True
)

# Клавиатура для выбора: сегодня, вчера или ввести дату
choose_day_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Сегодня")],
        [KeyboardButton(text="📆 Вчера")],
        [KeyboardButton(text="📝 Ввести дату")],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_which_day = State()      # Сегодня, Вчера или Ввести дату
    waiting_for_custom_date = State()    # Ожидание ручного ввода даты
    waiting_for_text = State()           # Ожидание текста (новый режим)
    waiting_for_edit_date = State()      # Выбор даты для редактирования
    waiting_for_edit_text = State()      # Ожидание нового текста
    # Новые состояния для конструктора задач
    building_task = State()              # Построение списка задач
    editing_task = State()               # Редактирование существующей записи
    selecting_line_to_edit = State()     # Выбор строки для редактирования
    editing_line = State()               # Редактирование конкретной строки
    selecting_line_to_delete = State()   # Выбор строки для удаления

# ========== РАБОТА С ШАБЛОНАМИ ==========
def get_templates():
    """Получает список шаблонов из таблицы Templates (столбец name)"""
    try:
        names = templates_ws.col_values(1)
        templates = [name.strip() for name in names if name.strip() and name.strip().lower() != "name"]
        return templates
    except Exception as e:
        print(f"Ошибка загрузки шаблонов: {e}")
        return []

def create_templates_keyboard(templates, show_done=True):
    """Создает клавиатуру с шаблонами"""
    keyboard = []
    row = []
    for template in templates:
        row.append(InlineKeyboardButton(
            text=template,
            callback_data=f"template_{template}"
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="➕ Другая активность", callback_data="template_other")])
    if show_done:
        keyboard.append([InlineKeyboardButton(text="✅ Готово", callback_data="done_building")])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_building")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def create_edit_keyboard():
    """Создает клавиатуру для управления задачами"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add_task")],
        [InlineKeyboardButton(text="✏️ Редактировать строку", callback_data="edit_line")],
        [InlineKeyboardButton(text="🗑 Удалить строку", callback_data="delete_line")],
        [InlineKeyboardButton(text="💾 Сохранить", callback_data="finish_recording")]
    ])
    return keyboard

def create_line_selection_keyboard(lines, action):
    """Создает клавиатуру для выбора строки"""
    keyboard = []
    for i, line in enumerate(lines):
        preview = line[:35] + "..." if len(line) > 35 else line
        keyboard.append([InlineKeyboardButton(
            text=f"{i+1}. {preview}",
            callback_data=f"{action}_{i}"
        )])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_selection")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ========== РАБОТА С ЗАПИСЯМИ ==========
def parse_tasks_to_text(tasks):
    """Преобразует список задач в текст с нумерацией"""
    if not tasks:
        return ""
    text_lines = []
    for i, task in enumerate(tasks, 1):
        text_lines.append(f"{i}. {task}")
    return "\n".join(text_lines)

def parse_text_to_tasks(text):
    """Преобразует текст с нумерацией обратно в список задач"""
    if not text:
        return []
    tasks = []
    lines = text.strip().split('\n')
    for line in lines:
        cleaned = re.sub(r'^\d+\.\s*', '', line.strip())
        if cleaned:
            tasks.append(cleaned)
    return tasks

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

# ========== СОЗДАНИЕ ЗАПИСИ (НОВЫЙ КОНСТРУКТОР) ==========
@dp.message(lambda m: m.text == "✍️ Ввести часы")
async def add_chrono_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    await state.update_data(full_name=name, tasks=[], edit_mode=False)
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
        
        await state.update_data(target_date=target_date)
        templates = get_templates()
        
        if templates:
            await msg.answer(
                f"📝 Создание записи за {target_date}\n\n"
                f"Выбери задачу или используй «Другая активность»:",
                reply_markup=create_templates_keyboard(templates)
            )
        else:
            await msg.answer(
                f"📝 Отправь список задач за {target_date} (многострочный):\n\n"
                f"Пример:\n1. Общался с ИИ - 2 ч\n2. Резал морковь - 24 ч",
                reply_markup=types.ReplyKeyboardRemove()
            )
        await state.set_state(ChronoState.building_task)
        
    elif choice == "📆 Вчера":
        target_date = yesterday
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
        
        await state.update_data(target_date=target_date)
        templates = get_templates()
        
        if templates:
            await msg.answer(
                f"📝 Создание записи за {target_date}\n\n"
                f"Выбери задачу или используй «Другая активность»:",
                reply_markup=create_templates_keyboard(templates)
            )
        else:
            await msg.answer(
                f"📝 Отправь список задач за {target_date} (многострочный):\n\n"
                f"Пример:\n1. Общался с ИИ - 2 ч\n2. Резал морковь - 24 ч",
                reply_markup=types.ReplyKeyboardRemove()
            )
        await state.set_state(ChronoState.building_task)
        
    elif choice == "📝 Ввести дату":
        await msg.answer(
            "📅 Введи дату в формате ГГГГ-ММ-ДД\n\n"
            "Пример: 2024-12-31",
            reply_markup=cancel_kb
        )
        await state.set_state(ChronoState.waiting_for_custom_date)
    else:
        await msg.answer("Пожалуйста, используй кнопки")

@dp.message(ChronoState.waiting_for_custom_date)
async def process_custom_date(msg: types.Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await msg.answer("❌ Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    try:
        datetime.strptime(msg.text, "%Y-%m-%d")
        target_date = msg.text
        data = await state.get_data()
        name = data["full_name"]
        
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
        
        await state.update_data(target_date=target_date)
        templates = get_templates()
        
        if templates:
            await msg.answer(
                f"📝 Создание записи за {target_date}\n\n"
                f"Выбери задачу или используй «Другая активность»:",
                reply_markup=create_templates_keyboard(templates)
            )
        else:
            await msg.answer(
                f"📝 Отправь список задач за {target_date} (многострочный):\n\n"
                f"Пример:\n1. Общался с ИИ - 2 ч\n2. Резал морковь - 24 ч",
                reply_markup=types.ReplyKeyboardRemove()
            )
        await state.set_state(ChronoState.building_task)
        
    except ValueError:
        await msg.answer("❌ Неверный формат даты. Используй ГГГГ-ММ-ДД\nПример: 2024-12-31")

# ========== КОНСТРУКТОР ЗАДАЧ ==========
@dp.callback_query(ChronoState.building_task)
async def process_template_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "cancel_building":
        await callback.message.edit_text("❌ Отменено")
        await callback.message.answer("Главное меню:", reply_markup=main_kb)
        await state.clear()
        return
    
    if callback.data == "done_building":
        await finish_building(callback.message, state)
        return
    
    data = await state.get_data()
    tasks = data.get("tasks", [])
    
    if callback.data == "template_other":
        await callback.message.edit_text(
            "📝 Отправь свою задачу в формате:\nДело - Часы\n\n"
            "Пример: Проверка кода - 1.5\n\n"
            "Или нажми Отмена",
            reply_markup=cancel_kb
        )
        return
    
    if callback.data.startswith("template_"):
        template_name = callback.data.replace("template_", "")
        tasks.append(f"{template_name} - ")
        await state.update_data(tasks=tasks)
        
        tasks_text = parse_tasks_to_text(tasks)
        templates = get_templates()
        
        await callback.message.edit_text(
            f"📅 Дата: {data.get('target_date')}\n\n"
            f"📋 Текущий список:\n{tasks_text}\n\n"
            f"⬇️ Выбери следующую задачу или укажи часы для последней:",
            reply_markup=create_templates_keyboard(templates, show_done=True)
        )

@dp.message(ChronoState.building_task)
async def process_task_hours(msg: types.Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await msg.answer("❌ Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    data = await state.get_data()
    tasks = data.get("tasks", [])
    
    if not tasks:
        await msg.answer("❌ Сначала выбери задачу из кнопок ниже")
        return
    
    last_task = tasks[-1]
    if last_task.endswith(" - "):
        hours_input = msg.text.strip()
        try:
            # Поддерживаем ввод с запятой или точкой
            hours = float(hours_input.replace(',', '.'))
            if hours < 0 or hours > 24:
                await msg.answer("❌ Часы должны быть от 0 до 24")
                return
            
            # Форматируем часы (целые без .0)
            if hours == int(hours):
                hours_str = str(int(hours))
            else:
                hours_str = str(hours)
            
            tasks[-1] = f"{last_task}{hours_str} ч"
            await state.update_data(tasks=tasks)
            
            tasks_text = parse_tasks_to_text(tasks)
            templates = get_templates()
            
            await msg.answer(
                f"📅 Дата: {data.get('target_date')}\n\n"
                f"📋 Текущий список:\n{tasks_text}\n\n"
                f"⬇️ Выбери следующую задачу или нажми Готово:",
                reply_markup=create_templates_keyboard(templates, show_done=True)
            )
        except ValueError:
            await msg.answer("❌ Неверный формат часов. Отправь число (например, 2 или 1.5)")
    else:
        await msg.answer("❌ Что-то пошло не так. Начни заново через «✍️ Ввести часы»")

async def finish_building(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tasks = data.get("tasks", [])
    target_date = data.get("target_date")
    name = data.get("full_name")
    edit_mode = data.get("edit_mode", False)
    row_idx = data.get("original_row_idx")
    
    if not tasks:
        await message.answer("❌ Задачи не добавлены. Отменено.", reply_markup=main_kb)
        await state.clear()
        return
    
    # Проверяем, все ли задачи имеют часы
    incomplete_tasks = [t for t in tasks if t.endswith(" - ")]
    if incomplete_tasks:
        await message.answer(
            f"❌ Укажи часы для:\n{chr(10).join(incomplete_tasks)}\n\n"
            f"Отправь часы для последней задачи"
        )
        return
    
    tasks_text = parse_tasks_to_text(tasks)
    
    if edit_mode and row_idx:
        update_record(row_idx, tasks_text)
        await message.answer(
            f"✅ Запись за {target_date} обновлена!\n\n{tasks_text}",
            reply_markup=main_kb
        )
    else:
        create_record(name, tasks_text, target_date)
        await message.answer(
            f"✅ Запись за {target_date} сохранена!\n\n{tasks_text}",
            reply_markup=main_kb
        )
    await state.clear()

# ========== РЕДАКТИРОВАНИЕ ЗАПИСЕЙ (С КОНСТРУКТОРОМ) ==========
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
    
    # Создаём кнопки с датами
    keyboard = []
    row = []
    for date in dates[:12]:
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
    
    if callback.data == "edit_cancel":
        await callback.message.edit_text("❌ Отменено")
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
    tasks = parse_text_to_tasks(old_text)
    
    await state.update_data(
        target_date=date_str,
        tasks=tasks,
        original_row_idx=row_idx,
        original_text=old_text,
        edit_mode=True
    )
    
    tasks_text = parse_tasks_to_text(tasks)
    await callback.message.edit_text(
        f"📅 Редактирование записи за {date_str}\n\n"
        f"📋 Текущий список:\n{tasks_text}\n\n"
        f"⬇️ Выбери действие:",
        reply_markup=create_edit_keyboard()
    )
    await state.set_state(ChronoState.editing_task)

@dp.callback_query(ChronoState.editing_task)
async def handle_edit_actions(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "add_task":
        templates = get_templates()
        await callback.message.edit_text(
            "📝 Выбери задачу для добавления:",
            reply_markup=create_templates_keyboard(templates, show_done=False)
        )
        await state.set_state(ChronoState.building_task)
        
    elif callback.data == "edit_line":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        if not tasks:
            await callback.message.edit_text("❌ Нет задач для редактирования")
            return
        
        await callback.message.edit_text(
            "✏️ Выбери строку для редактирования:",
            reply_markup=create_line_selection_keyboard(tasks, "select_edit")
        )
        await state.set_state(ChronoState.selecting_line_to_edit)
        
    elif callback.data == "delete_line":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        if not tasks:
            await callback.message.edit_text("❌ Нет задач для удаления")
            return
        
        await callback.message.edit_text(
            "🗑 Выбери строку для удаления:",
            reply_markup=create_line_selection_keyboard(tasks, "select_delete")
        )
        await state.set_state(ChronoState.selecting_line_to_delete)
        
    elif callback.data == "finish_recording":
        await finish_building(callback.message, state)

@dp.callback_query(ChronoState.selecting_line_to_edit)
async def process_edit_line_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "cancel_selection":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await callback.message.edit_text(
            f"📅 Редактирование записи за {data.get('target_date')}\n\n"
            f"📋 Текущий список:\n{tasks_text}\n\n"
            f"⬇️ Выбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(ChronoState.editing_task)
        return
    
    if callback.data.startswith("select_edit_"):
        line_index = int(callback.data.replace("select_edit_", ""))
        await state.update_data(editing_line_index=line_index)
        
        await callback.message.edit_text(
            f"✏️ Редактируем строку {line_index + 1}\n"
            f"Отправь новый текст в формате: Дело - Часы\n\n"
            f"Пример: Встреча - 2\n\n"
            f"Или нажми Отмена",
            reply_markup=cancel_kb
        )
        await state.set_state(ChronoState.editing_line)

@dp.callback_query(ChronoState.selecting_line_to_delete)
async def process_delete_line_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "cancel_selection":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await callback.message.edit_text(
            f"📅 Редактирование записи за {data.get('target_date')}\n\n"
            f"📋 Текущий список:\n{tasks_text}\n\n"
            f"⬇️ Выбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(ChronoState.editing_task)
        return
    
    if callback.data.startswith("select_delete_"):
        line_index = int(callback.data.replace("select_delete_", ""))
        data = await state.get_data()
        tasks = data.get("tasks", [])
        
        if 0 <= line_index < len(tasks):
            deleted_task = tasks.pop(line_index)
            await state.update_data(tasks=tasks)
            
            tasks_text = parse_tasks_to_text(tasks)
            await callback.message.edit_text(
                f"🗑 Удалено: {deleted_task}\n\n"
                f"📋 Текущий список:\n{tasks_text}\n\n"
                f"⬇️ Выбери действие:",
                reply_markup=create_edit_keyboard()
            )
            await state.set_state(ChronoState.editing_task)

@dp.message(ChronoState.editing_line)
async def save_edited_line(msg: types.Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await msg.answer(
            f"📋 Текущий список:\n{tasks_text}\n\n⬇️ Выбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(ChronoState.editing_task)
        return
    
    data = await state.get_data()
    tasks = data.get("tasks", [])
    line_index = data.get("editing_line_index")
    
    if " - " not in msg.text:
        await msg.answer("❌ Неверный формат. Используй: Дело - Часы\nПример: Встреча - 2")
        return
    
    try:
        parts = msg.text.split(" - ")
        activity = parts[0].strip()
        hours = parts[1].replace("ч", "").replace("hours", "").strip()
        hours_value = float(hours.replace(',', '.'))
        
        if hours_value < 0 or hours_value > 24:
            await msg.answer("❌ Часы должны быть от 0 до 24")
            return
        
        if hours_value == int(hours_value):
            hours_str = str(int(hours_value))
        else:
            hours_str = str(hours_value)
        
        new_task = f"{activity} - {hours_str} ч"
        tasks[line_index] = new_task
        await state.update_data(tasks=tasks)
        
        tasks_text = parse_tasks_to_text(tasks)
        await msg.answer(
            f"✅ Строка обновлена!\n\n📋 Текущий список:\n{tasks_text}\n\n⬇️ Выбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(ChronoState.editing_task)
    except ValueError:
        await msg.answer("❌ Ошибка: неверный формат часов. Используй: Дело - Часы\nПример: Встреча - 2")

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
                    await bot.send_photo(tg_id, img, caption=f"🔔 Напоминание, {name}! Внеси часы за сегодня!")
                    await bot.send_message(tg_id, "Нажми «✍️ Ввести часы»", reply_markup=main_kb)
            except Exception as e:
                print(f"Ошибка напоминания: {e}")

# ========== ЗАПУСК ==========
async def main():
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    asyncio.create_task(reminder_loop())
    
    print("✅ Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
