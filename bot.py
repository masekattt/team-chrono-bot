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
        [KeyboardButton(text="Создать запись")],
        [KeyboardButton(text="Редактировать запись")]
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True
)

# ========== FSM СОСТОЯНИЯ ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class RecordState(StatesGroup):
    choosing_date = State()
    building_task = State()
    editing_task = State()
    selecting_line_to_edit = State()
    editing_line = State()
    selecting_line_to_delete = State()

# ========== РАБОТА С ШАБЛОНАМИ ==========
def get_templates():
    """Получает список шаблонов из таблицы Templates (только столбец name)"""
    try:
        # Получаем все значения из первого столбца (A)
        names = templates_ws.col_values(1)
        # Пропускаем заголовок если он есть
        templates = [name.strip() for name in names if name.strip() and name.strip().lower() != "name"]
        return templates
    except Exception as e:
        print(f"Ошибка загрузки шаблонов: {e}")
        return []

def create_templates_keyboard(templates):
    """Создает клавиатуру с шаблонами (по 2 кнопки в ряд)"""
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
    
    keyboard.append([InlineKeyboardButton(text="Другая активность", callback_data="template_other")])
    keyboard.append([InlineKeyboardButton(text="Готово", callback_data="done_building")])
    keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_building")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def create_edit_keyboard():
    """Создает клавиатуру для управления задачами"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить задачу", callback_data="add_task")],
        [InlineKeyboardButton(text="Редактировать строку", callback_data="edit_line")],
        [InlineKeyboardButton(text="Удалить строку", callback_data="delete_line")],
        [InlineKeyboardButton(text="Завершить", callback_data="finish_recording")]
    ])
    return keyboard

def create_line_delete_keyboard(lines):
    """Создает клавиатуру для выбора строки для удаления"""
    keyboard = []
    for i, line in enumerate(lines):
        preview = line[:30] + "..." if len(line) > 30 else line
        keyboard.append([InlineKeyboardButton(
            text=f"{i+1}. {preview}",
            callback_data=f"select_delete_{i}"
        )])
    keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="delete_cancel")])
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
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        created_date = str(row.get("Дата", "")).split()[0]
        if row.get("ФИО") == name and created_date == target_date:
            return idx, row.get("Текст", "")
    return None

def create_record(name, text, target_date):
    record_datetime = datetime.strptime(target_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    record_str = record_datetime.strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.append_row([name, text, record_str, ""])

def update_record(row_idx, new_text):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.update([[new_text]], f"B{row_idx}")
    chrono_ws.update([[now]], f"D{row_idx}")

def get_all_user_dates(name):
    records = chrono_ws.get_all_records()
    dates = []
    for row in records:
        if row.get("ФИО") == name:
            date_str = str(row.get("Дата", "")).split()[0]
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

# ========== СОЗДАНИЕ ЗАПИСИ ==========
@dp.message(lambda m: m.text == "Создать запись")
async def create_record_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала используй /start")
        return
    
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    date_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=today)],
            [KeyboardButton(text=yesterday)],
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )
    
    await state.update_data(full_name=name, tasks=[], edit_mode=False)
    await msg.answer(f"Выбери дату для записи:", reply_markup=date_kb)
    await state.set_state(RecordState.choosing_date)

@dp.message(RecordState.choosing_date)
async def process_date_selection(msg: types.Message, state: FSMContext):
    if msg.text == "Отмена":
        await msg.answer("Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    try:
        datetime.strptime(msg.text, "%Y-%m-%d")
        target_date = msg.text
    except:
        await msg.answer("Неверный формат даты. Используй ГГГГ-ММ-ДД или нажми на кнопку")
        return
    
    data = await state.get_data()
    name = data["full_name"]
    edit_mode = data.get("edit_mode", False)
    
    if not edit_mode:
        existing = get_record_by_date_and_name(name, target_date)
        if existing:
            row_idx, old_text = existing
            await msg.answer(
                f"Запись за {target_date} уже существует:\n\n{old_text}\n\n"
                f"Используй кнопку «Редактировать запись»",
                reply_markup=main_kb
            )
            await state.clear()
            return
    
    await state.update_data(target_date=target_date, tasks=[], original_row_idx=None)
    
    templates = get_templates()
    if templates:
        await msg.answer(
            f"Создание записи за {target_date}\n\n"
            f"Выбери задачу или используй «Другая активность»:",
            reply_markup=create_templates_keyboard(templates)
        )
    else:
        await msg.answer(
            f"Создание записи за {target_date}\n\n"
            f"Отправь задачу в формате: Дело - Часы\n"
            f"Пример: Встреча - 2",
            reply_markup=cancel_kb
        )
    await state.set_state(RecordState.building_task)

@dp.callback_query(RecordState.building_task)
async def process_template_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "cancel_building":
        await callback.message.edit_text("Отменено")
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
            "Отправь свою задачу в формате:\nДело - Часы\n\n"
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
            f"Дата: {data.get('target_date')}\n\n"
            f"Текущий список:\n{tasks_text}\n\n"
            f"Выбери следующую задачу или укажи часы для последней:",
            reply_markup=create_templates_keyboard(templates)
        )
        await state.set_state(RecordState.building_task)

@dp.message(RecordState.building_task)
async def process_task_hours(msg: types.Message, state: FSMContext):
    if msg.text == "Отмена":
        await msg.answer("Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    data = await state.get_data()
    tasks = data.get("tasks", [])
    
    if not tasks:
        await msg.answer("Сначала выбери задачу из кнопок ниже")
        return
    
    last_task = tasks[-1]
    if last_task.endswith(" - "):
        hours_input = msg.text.strip()
        try:
            hours = float(hours_input.replace(',', '.'))
            if hours < 0 or hours > 24:
                await msg.answer("Часы должны быть от 0 до 24")
                return
            
            if hours == int(hours):
                hours_str = str(int(hours))
            else:
                hours_str = str(hours)
            
            tasks[-1] = f"{last_task}{hours_str} ч"
            await state.update_data(tasks=tasks)
            
            tasks_text = parse_tasks_to_text(tasks)
            templates = get_templates()
            
            await msg.answer(
                f"Дата: {data.get('target_date')}\n\n"
                f"Текущий список:\n{tasks_text}\n\n"
                f"Выбери следующую задачу или нажми Готово:",
                reply_markup=create_templates_keyboard(templates)
            )
        except ValueError:
            await msg.answer("Неверный формат часов. Отправь число (например, 2 или 1.5)")
    else:
        await msg.answer("Что-то пошло не так. Начни заново через Создать запись")

async def finish_building(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tasks = data.get("tasks", [])
    target_date = data.get("target_date")
    name = data.get("full_name")
    row_idx = data.get("original_row_idx")
    edit_mode = data.get("edit_mode", False)
    
    if not tasks:
        await message.answer("Задачи не добавлены. Отменено.", reply_markup=main_kb)
        await state.clear()
        return
    
    incomplete_tasks = [t for t in tasks if t.endswith(" - ")]
    if incomplete_tasks:
        await message.answer(
            f"Укажи часы для:\n{chr(10).join(incomplete_tasks)}\n\n"
            f"Отправь часы для последней задачи"
        )
        return
    
    tasks_text = parse_tasks_to_text(tasks)
    
    if edit_mode and row_idx:
        update_record(row_idx, tasks_text)
        await message.answer(
            f"Запись за {target_date} обновлена!\n\n{tasks_text}",
            reply_markup=main_kb
        )
    else:
        create_record(name, tasks_text, target_date)
        await message.answer(
            f"Запись за {target_date} сохранена!\n\n{tasks_text}",
            reply_markup=main_kb
        )
    await state.clear()

# ========== РЕДАКТИРОВАНИЕ ЗАПИСИ ==========
@dp.message(lambda m: m.text == "Редактировать запись")
async def edit_record_start(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала используй /start")
        return
    
    dates = get_all_user_dates(name)
    if not dates:
        await msg.answer("Нет записей. Сначала создай запись через «Создать запись»")
        return
    
    date_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=date)] for date in dates[:10]] + [[KeyboardButton(text="Отмена")]],
        resize_keyboard=True
    )
    
    await state.update_data(full_name=name, edit_mode=True)
    await msg.answer("Выбери дату для редактирования:", reply_markup=date_kb)
    await state.set_state(RecordState.choosing_date)

# Переопределяем обработчик выбора даты для режима редактирования
@dp.message(RecordState.choosing_date)
async def process_edit_date_selection(msg: types.Message, state: FSMContext):
    if msg.text == "Отмена":
        await msg.answer("Отменено", reply_markup=main_kb)
        await state.clear()
        return
    
    data = await state.get_data()
    edit_mode = data.get("edit_mode", False)
    
    if edit_mode:
        target_date = msg.text
        name = data["full_name"]
        
        record = get_record_by_date_and_name(name, target_date)
        if not record:
            await msg.answer("Запись не найдена", reply_markup=main_kb)
            await state.clear()
            return
        
        row_idx, old_text = record
        tasks = parse_text_to_tasks(old_text)
        
        await state.update_data(
            target_date=target_date,
            tasks=tasks,
            original_row_idx=row_idx,
            original_text=old_text
        )
        
        tasks_text = parse_tasks_to_text(tasks)
        await msg.answer(
            f"Редактирование записи за {target_date}\n\n"
            f"Текущий список:\n{tasks_text}\n\n"
            f"Выбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(RecordState.editing_task)

@dp.callback_query(RecordState.editing_task)
async def handle_edit_actions(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "add_task":
        templates = get_templates()
        await callback.message.edit_text(
            "Выбери задачу для добавления:",
            reply_markup=create_templates_keyboard(templates)
        )
        await state.set_state(RecordState.building_task)
        
    elif callback.data == "edit_line":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        if not tasks:
            await callback.message.edit_text("Нет задач для редактирования")
            return
        
        # Создаем клавиатуру для выбора строки
        keyboard = []
        for i, task in enumerate(tasks):
            preview = task[:30] + "..." if len(task) > 30 else task
            keyboard.append([InlineKeyboardButton(
                text=f"{i+1}. {preview}",
                callback_data=f"select_edit_{i}"
            )])
        keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="edit_cancel")])
        
        await callback.message.edit_text(
            "Выбери строку для редактирования:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        await state.set_state(RecordState.selecting_line_to_edit)
        
    elif callback.data == "delete_line":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        if not tasks:
            await callback.message.edit_text("Нет задач для удаления")
            return
        
        await callback.message.edit_text(
            "Выбери строку для удаления:",
            reply_markup=create_line_delete_keyboard(tasks)
        )
        await state.set_state(RecordState.selecting_line_to_delete)
        
    elif callback.data == "finish_recording":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        target_date = data.get("target_date")
        row_idx = data.get("original_row_idx")
        
        tasks_text = parse_tasks_to_text(tasks)
        update_record(row_idx, tasks_text)
        
        await callback.message.edit_text(
            f"Запись за {target_date} обновлена!\n\n{tasks_text}"
        )
        await callback.message.answer("Главное меню:", reply_markup=main_kb)
        await state.clear()

@dp.callback_query(RecordState.selecting_line_to_edit)
async def process_line_selection_for_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "edit_cancel":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await callback.message.edit_text(
            f"Редактирование записи\n\nТекущий список:\n{tasks_text}\n\nВыбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(RecordState.editing_task)
        return
    
    if callback.data.startswith("select_edit_"):
        line_index = int(callback.data.replace("select_edit_", ""))
        await state.update_data(editing_line_index=line_index)
        
        await callback.message.edit_text(
            f"Редактируем строку {line_index + 1}\n"
            f"Отправь новый текст в формате: Дело - Часы\n\n"
            f"Пример: Встреча - 2",
            reply_markup=cancel_kb
        )
        await state.set_state(RecordState.editing_line)

@dp.callback_query(RecordState.selecting_line_to_delete)
async def process_line_selection_for_delete(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "delete_cancel":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await callback.message.edit_text(
            f"Редактирование записи\n\nТекущий список:\n{tasks_text}\n\nВыбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(RecordState.editing_task)
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
                f"Удалено: {deleted_task}\n\n"
                f"Текущий список:\n{tasks_text}\n\n"
                f"Выбери действие:",
                reply_markup=create_edit_keyboard()
            )
            await state.set_state(RecordState.editing_task)

@dp.message(RecordState.editing_line)
async def save_edited_line(msg: types.Message, state: FSMContext):
    if msg.text == "Отмена":
        data = await state.get_data()
        tasks = data.get("tasks", [])
        tasks_text = parse_tasks_to_text(tasks)
        await msg.answer(
            f"Текущий список:\n{tasks_text}\n\nВыбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(RecordState.editing_task)
        return
    
    data = await state.get_data()
    tasks = data.get("tasks", [])
    line_index = data.get("editing_line_index")
    
    if " - " not in msg.text:
        await msg.answer("Неверный формат. Используй: Дело - Часы")
        return
    
    try:
        parts = msg.text.split(" - ")
        activity = parts[0]
        hours = parts[1].replace("ч", "").replace("hours", "").strip()
        hours_value = float(hours.replace(',', '.'))
        
        if hours_value == int(hours_value):
            hours_str = str(int(hours_value))
        else:
            hours_str = str(hours_value)
        
        new_task = f"{activity} - {hours_str} ч"
        tasks[line_index] = new_task
        await state.update_data(tasks=tasks)
        
        tasks_text = parse_tasks_to_text(tasks)
        await msg.answer(
            f"Строка обновлена!\n\nТекущий список:\n{tasks_text}\n\nВыбери действие:",
            reply_markup=create_edit_keyboard()
        )
        await state.set_state(RecordState.editing_task)
    except Exception as e:
        await msg.answer(f"Ошибка: неверный формат. Используй: Дело - Часы (например, Встреча - 2)")

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
                    await bot.send_photo(tg_id, img, caption=f"Напоминание, {name}! Внеси часы за сегодня")
                    await bot.send_message(tg_id, "Нажми «Создать запись»", reply_markup=main_kb)
            except Exception as e:
                print(f"Ошибка напоминания: {e}")

# ========== ЗАПУСК ==========
async def main():
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    asyncio.create_task(reminder_loop())
    
    print("Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
