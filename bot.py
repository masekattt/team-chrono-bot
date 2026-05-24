import asyncio
import logging
import os
import re
import json
from datetime import datetime, timedelta
from threading import Thread
from typing import List, Optional, Dict, Any

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from flask import Flask
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
CREDENTIALS_JSON = os.getenv('CREDENTIALS_JSON')
PORT = int(os.getenv('PORT', 10000))

# Инициализация Flask для health check
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is working"

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# Инициализация Google Sheets
def init_google_sheets():
    """Инициализация подключения к Google Sheets"""
    try:
        credentials_dict = json.loads(CREDENTIALS_JSON)
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            credentials_dict, scope
        )
        client = gspread.authorize(credentials)
        return client.open_by_key(SHEET_ID)
    except Exception as e:
        logger.error(f"Ошибка подключения к Google Sheets: {e}")
        raise

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Глобальные переменные
spreadsheet = None
templates_cache = []
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Состояния FSM
class RegistrationStates(StatesGroup):
    waiting_for_name = State()

class TimeEntryStates(StatesGroup):
    waiting_for_date_choice = State()
    waiting_for_custom_date = State()
    managing_tasks = State()
    waiting_for_template_choice = State()
    waiting_for_hours = State()
    waiting_for_custom_activity = State()
    waiting_for_edit_line_number = State()
    waiting_for_edit_new_text = State()
    waiting_for_delete_line_number = State()

class EditStates(StatesGroup):
    waiting_for_date = State()
    managing_tasks = State()
    waiting_for_template_choice = State()
    waiting_for_hours = State()
    waiting_for_custom_activity = State()
    waiting_for_edit_line_number = State()
    waiting_for_edit_new_text = State()
    waiting_for_delete_line_number = State()

# Вспомогательные функции
def load_templates():
    """Загрузка шаблонов из Google Sheets"""
    global templates_cache
    try:
        templates_sheet = spreadsheet.worksheet('Templates')
        templates = templates_sheet.col_values(1)[1:]  # Пропускаем заголовок
        templates_cache = [t for t in templates if t.strip()]
        logger.info(f"Загружено {len(templates_cache)} шаблонов")
    except Exception as e:
        logger.error(f"Ошибка загрузки шаблонов: {e}")
        templates_cache = []

def get_user_name(telegram_id: int) -> Optional[str]:
    """Получение имени пользователя по telegram_id"""
    try:
        users_sheet = spreadsheet.worksheet('Users')
        users = users_sheet.get_all_records()
        for user in users:
            if user['telegram_id'] == telegram_id:
                return user['full_name']
        return None
    except Exception as e:
        logger.error(f"Ошибка получения имени пользователя: {e}")
        return None

def register_user(telegram_id: int, full_name: str):
    """Регистрация нового пользователя"""
    try:
        users_sheet = spreadsheet.worksheet('Users')
        users_sheet.append_row([telegram_id, full_name])
        logger.info(f"Зарегистрирован пользователь: {full_name} (ID: {telegram_id})")
    except Exception as e:
        logger.error(f"Ошибка регистрации пользователя: {e}")
        raise

def check_existing_entry(telegram_id: int, date: str) -> bool:
    """Проверка существования записи за указанную дату"""
    try:
        chrono_sheet = spreadsheet.worksheet('Chrono')
        records = chrono_sheet.get_all_records()
        user_name = get_user_name(telegram_id)
        if not user_name:
            return False
        
        for record in records:
            if record['ФИО'] == user_name:
                record_date = record['Дата и время создания'].split()[0] if record['Дата и время создания'] else ''
                if record_date == date:
                    return True
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки записи: {e}")
        return False

def get_entry_by_date(telegram_id: int, date: str) -> Optional[Dict]:
    """Получение записи за указанную дату"""
    try:
        chrono_sheet = spreadsheet.worksheet('Chrono')
        records = chrono_sheet.get_all_records()
        user_name = get_user_name(telegram_id)
        if not user_name:
            return None
        
        for i, record in enumerate(records):
            if record['ФИО'] == user_name:
                record_date = record['Дата и время создания'].split()[0] if record['Дата и время создания'] else ''
                if record_date == date:
                    return {
                        'row': i + 2,  # +2 из-за заголовка и 0-based индекса
                        'data': record
                    }
        return None
    except Exception as e:
        logger.error(f"Ошибка получения записи: {e}")
        return None

def save_new_entry(telegram_id: int, date: str, text: str):
    """Сохранение новой записи"""
    try:
        chrono_sheet = spreadsheet.worksheet('Chrono')
        user_name = get_user_name(telegram_id)
        if not user_name:
            raise ValueError("Пользователь не найден")
        
        creation_time = f"{date} 23:59:59"
        chrono_sheet.append_row([user_name, text, creation_time, ''])
        logger.info(f"Сохранена запись для {user_name} за {date}")
    except Exception as e:
        logger.error(f"Ошибка сохранения записи: {e}")
        raise

def update_entry(row_number: int, text: str):
    """Обновление существующей записи"""
    try:
        chrono_sheet = spreadsheet.worksheet('Chrono')
        current_time = datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')
        chrono_sheet.update(f'B{row_number}', [[text]])
        chrono_sheet.update(f'D{row_number}', [[current_time]])
        logger.info(f"Обновлена запись в строке {row_number}")
    except Exception as e:
        logger.error(f"Ошибка обновления записи: {e}")
        raise

def parse_tasks_text(text: str) -> List[str]:
    """Разбор текста задач на отдельные строки"""
    if not text.strip():
        return []
    return [line.strip() for line in text.split('\n') if line.strip()]

def format_tasks_list(tasks: List[str]) -> str:
    """Форматирование списка задач для отображения"""
    if not tasks:
        return "(пусто)"
    
    formatted = []
    for i, task in enumerate(tasks, 1):
        # Убираем существующую нумерацию если есть
        task_clean = re.sub(r'^\d+\.\s*', '', task)
        formatted.append(f"{i}. {task_clean}")
    return '\n'.join(formatted)

def add_task_to_list(tasks: List[str], task_text: str) -> List[str]:
    """Добавление задачи в список"""
    tasks.append(task_text)
    return tasks

def edit_task_in_list(tasks: List[str], line_number: int, new_text: str) -> List[str]:
    """Редактирование задачи в списке"""
    if 0 <= line_number < len(tasks):
        tasks[line_number] = new_text
    return tasks

def delete_task_from_list(tasks: List[str], line_number: int) -> List[str]:
    """Удаление задачи из списка"""
    if 0 <= line_number < len(tasks):
        tasks.pop(line_number)
    return tasks

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Ввести часы")],
            [KeyboardButton(text="Изменить часы")]
        ],
        resize_keyboard=True
    )

def get_date_choice_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура выбора даты"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Вчера")],
            [KeyboardButton(text="Другая дата"), KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )

def get_task_management_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура управления задачами"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить задачу")],
            [KeyboardButton(text="Редактировать строку"), KeyboardButton(text="Удалить строку")],
            [KeyboardButton(text="Готово"), KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )

def get_add_task_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура добавления задачи"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Шаблон"), KeyboardButton(text="Другие активности")],
            [KeyboardButton(text="Назад к списку")]
        ],
        resize_keyboard=True
    )

def get_templates_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с шаблонами"""
    buttons = []
    row = []
    for i, template in enumerate(templates_cache):
        row.append(InlineKeyboardButton(text=template, callback_data=f"template_{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_template")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def validate_date(date_text: str) -> bool:
    """Проверка корректности даты"""
    try:
        date_obj = datetime.strptime(date_text, '%Y-%m-%d').date()
        today = datetime.now(MOSCOW_TZ).date()
        return date_obj <= today
    except ValueError:
        return False

def validate_hours(hours_text: str) -> bool:
    """Проверка корректности ввода часов"""
    try:
        hours_text = hours_text.replace(',', '.')
        hours = float(hours_text)
        return hours > 0
    except ValueError:
        return False

# Обработчики команд
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    telegram_id = message.from_user.id
    
    # Проверяем, зарегистрирован ли пользователь
    user_name = get_user_name(telegram_id)
    
    if user_name:
        await message.answer(
            f"С возвращением, {user_name}!",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "Привет! Как тебя зовут? (напиши ФИО)",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(RegistrationStates.waiting_for_name)

@router.message(RegistrationStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    """Обработка ввода имени при регистрации"""
    full_name = message.text.strip()
    
    if len(full_name) < 2:
        await message.answer("Имя не может быть пустым или слишком коротким. Введи ФИО:")
        return
    
    telegram_id = message.from_user.id
    
    try:
        register_user(telegram_id, full_name)
        await message.answer(
            f"Принято, {full_name}!",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
    except Exception as e:
        await message.answer("Произошла ошибка при регистрации. Попробуй позже.")
        logger.error(f"Ошибка регистрации: {e}")

# Обработчики для "Ввести часы"
@router.message(F.text == "Ввести часы")
async def start_enter_time(message: Message, state: FSMContext):
    """Начало ввода часов"""
    await message.answer(
        "За какую дату хочешь внести часы?",
        reply_markup=get_date_choice_keyboard()
    )
    await state.set_state(TimeEntryStates.waiting_for_date_choice)

@router.message(TimeEntryStates.waiting_for_date_choice, F.text.in_(["Сегодня", "Вчера", "Другая дата", "Отмена"]))
async def process_date_choice(message: Message, state: FSMContext):
    """Обработка выбора даты"""
    if message.text == "Отмена":
        await message.answer("Операция отменена.", reply_markup=get_main_keyboard())
        await state.clear()
        return
    
    today = datetime.now(MOSCOW_TZ).date()
    
    if message.text == "Сегодня":
        selected_date = today.strftime('%Y-%m-%d')
    elif message.text == "Вчера":
        selected_date = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    elif message.text == "Другая дата":
        await message.answer(
            "Введи дату в формате ГГГГ-ММ-ДД (например, 2026-05-21):",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(TimeEntryStates.waiting_for_custom_date)
        return
    
    # Проверяем существующую запись
    telegram_id = message.from_user.id
    if check_existing_entry(telegram_id, selected_date):
        await message.answer(
            f"Запись за {selected_date} уже существует. Используй 'Изменить часы'",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    # Создаем новую запись
    await state.update_data(selected_date=selected_date, tasks=[])
    await show_task_list(message, state, [])

async def show_task_list(message: Message, state: FSMContext, tasks: List[str]):
    """Показать список задач и меню управления"""
    formatted_tasks = format_tasks_list(tasks)
    
    await message.answer(
        f"Текущий список:\n{formatted_tasks}\n\nВыбери действие:",
        reply_markup=get_task_management_keyboard()
    )
    await state.set_state(TimeEntryStates.managing_tasks)

@router.message(TimeEntryStates.waiting_for_custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    """Обработка ввода произвольной даты"""
    date_text = message.text.strip()
    
    if not validate_date(date_text):
        await message.answer("Неверный формат даты или нельзя ввести будущую дату. Используй ГГГГ-ММ-ДД:")
        return
    
    telegram_id = message.from_user.id
    if check_existing_entry(telegram_id, date_text):
        await message.answer(
            f"Запись за {date_text} уже существует. Используй 'Изменить часы'",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    await state.update_data(selected_date=date_text, tasks=[])
    await show_task_list(message, state, [])

# Управление задачами при создании новой записи
@router.message(TimeEntryStates.managing_tasks, F.text == "Добавить задачу")
async def add_task_menu(message: Message, state: FSMContext):
    """Меню добавления задачи"""
    await message.answer(
        "Выбери способ добавления:",
        reply_markup=get_add_task_keyboard()
    )

@router.message(TimeEntryStates.managing_tasks, F.text == "Шаблон")
async def show_templates(message: Message, state: FSMContext):
    """Показать шаблоны"""
    if not templates_cache:
        await message.answer("Шаблоны не загружены. Используй 'Другие активности'.")
        return
    
    await message.answer(
        "Выбери шаблон:",
        reply_markup=get_templates_keyboard()
    )
    await state.set_state(TimeEntryStates.waiting_for_template_choice)

@router.callback_query(TimeEntryStates.waiting_for_template_choice, F.data.startswith("template_"))
async def process_template_choice(callback_query, state: FSMContext):
    """Обработка выбора шаблона"""
    template_index = int(callback_query.data.split("_")[1])
    
    if 0 <= template_index < len(templates_cache):
        template_name = templates_cache[template_index]
        await state.update_data(current_template=template_name)
        
        await callback_query.message.edit_text(
            f"Выбран шаблон: {template_name}"
        )
        await callback_query.message.answer(
            f"Введи количество часов для '{template_name}':"
        )
        await state.set_state(TimeEntryStates.waiting_for_hours)
    else:
        await callback_query.answer("Шаблон не найден")

@router.callback_query(TimeEntryStates.waiting_for_template_choice, F.data == "cancel_template")
async def cancel_template_choice(callback_query, state: FSMContext):
    """Отмена выбора шаблона"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    await callback_query.message.edit_text("Выбор шаблона отменён")
    await show_task_list(callback_query.message, state, tasks)

@router.message(TimeEntryStates.waiting_for_hours)
async def process_hours_input(message: Message, state: FSMContext):
    """Обработка ввода часов для шаблона"""
    hours_text = message.text.strip()
    
    if not validate_hours(hours_text):
        await message.answer("Введи корректное число часов (например: 2, 1.5, 3.75):")
        return
    
    data = await state.get_data()
    template_name = data.get('current_template', '')
    tasks = data.get('tasks', [])
    
    hours = hours_text.replace(',', '.')
    task_text = f"{template_name} - {hours} часа"
    tasks = add_task_to_list(tasks, task_text)
    
    await state.update_data(tasks=tasks)
    await message.answer(f"Добавлено: {template_name} - {hours} часа")
    await show_task_list(message, state, tasks)

@router.message(TimeEntryStates.managing_tasks, F.text == "Другие активности")
async def custom_activity(message: Message, state: FSMContext):
    """Ввод произвольной активности"""
    await message.answer(
        "Расскажи, что выходящего за рамки ты сегодня сделал.\n\n"
        "Формат: Название задачи - часы\n"
        "Пример: Документация - 1,5 часа\n\n"
        "Ты можешь написать сразу несколько задач, каждую с новой строки.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(TimeEntryStates.waiting_for_custom_activity)

@router.message(TimeEntryStates.waiting_for_custom_activity)
async def process_custom_activity(message: Message, state: FSMContext):
    """Обработка произвольной активности"""
    text = message.text.strip()
    lines = text.split('\n')
    
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    for line in lines:
        if line.strip():
            tasks = add_task_to_list(tasks, line.strip())
    
    await state.update_data(tasks=tasks)
    await message.answer(f"Добавлено задач: {len(lines)}")
    await show_task_list(message, state, tasks)

@router.message(TimeEntryStates.managing_tasks, F.text == "Назад к списку")
async def back_to_task_list(message: Message, state: FSMContext):
    """Возврат к списку задач"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    await show_task_list(message, state, tasks)

@router.message(TimeEntryStates.managing_tasks, F.text == "Редактировать строку")
async def edit_line_start(message: Message, state: FSMContext):
    """Начало редактирования строки"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    if not tasks:
        await message.answer("Нет строк для редактирования")
        return
    
    formatted_tasks = format_tasks_list(tasks)
    await message.answer(
        f"Текущие строки:\n{formatted_tasks}\n\nВведи номер строки для редактирования:"
    )
    await state.set_state(TimeEntryStates.waiting_for_edit_line_number)

@router.message(TimeEntryStates.waiting_for_edit_line_number)
async def process_edit_line_number(message: Message, state: FSMContext):
    """Обработка номера строки для редактирования"""
    try:
        line_number = int(message.text.strip()) - 1
        data = await state.get_data()
        tasks = data.get('tasks', [])
        
        if 0 <= line_number < len(tasks):
            await state.update_data(edit_line=line_number)
            await message.answer(
                f"Текущий текст: {tasks[line_number]}\nВведи новый текст:"
            )
            await state.set_state(TimeEntryStates.waiting_for_edit_new_text)
        else:
            await message.answer(f"Строка не найдена. Всего строк: {len(tasks)}")
    except ValueError:
        await message.answer("Введи корректный номер строки")

@router.message(TimeEntryStates.waiting_for_edit_new_text)
async def process_edit_new_text(message: Message, state: FSMContext):
    """Обработка нового текста строки"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    line_number = data.get('edit_line', 0)
    
    new_text = message.text.strip()
    tasks = edit_task_in_list(tasks, line_number, new_text)
    
    await state.update_data(tasks=tasks, edit_line=None)
    await message.answer("Строка обновлена")
    await show_task_list(message, state, tasks)

@router.message(TimeEntryStates.managing_tasks, F.text == "Удалить строку")
async def delete_line_start(message: Message, state: FSMContext):
    """Начало удаления строки"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    if not tasks:
        await message.answer("Нет строк для удаления")
        return
    
    formatted_tasks = format_tasks_list(tasks)
    await message.answer(
        f"Текущие строки:\n{formatted_tasks}\n\nВведи номер строки для удаления:"
    )
    await state.set_state(TimeEntryStates.waiting_for_delete_line_number)

@router.message(TimeEntryStates.waiting_for_delete_line_number)
async def process_delete_line_number(message: Message, state: FSMContext):
    """Обработка удаления строки"""
    try:
        line_number = int(message.text.strip()) - 1
        data = await state.get_data()
        tasks = data.get('tasks', [])
        
        if 0 <= line_number < len(tasks):
            tasks = delete_task_from_list(tasks, line_number)
            await state.update_data(tasks=tasks)
            await message.answer("Строка удалена")
            await show_task_list(message, state, tasks)
        else:
            await message.answer(f"Строка не найдена. Всего строк: {len(tasks)}")
    except ValueError:
        await message.answer("Введи корректный номер строки")

@router.message(TimeEntryStates.managing_tasks, F.text == "Готово")
async def finish_entry(message: Message, state: FSMContext):
    """Завершение ввода и сохранение"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    selected_date = data.get('selected_date', '')
    
    if not tasks:
        await message.answer("Список задач пуст. Добавьте хотя бы одну задачу.")
        return
    
    # Форматируем текст для сохранения
    text_to_save = format_tasks_list(tasks)
    
    try:
        save_new_entry(message.from_user.id, selected_date, text_to_save)
        await message.answer(
            f"Запись за {selected_date} сохранена!",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
    except Exception as e:
        await message.answer("Ошибка при сохранении записи. Попробуй снова.")
        logger.error(f"Ошибка сохранения: {e}")

@router.message(TimeEntryStates.managing_tasks, F.text == "Отмена")
async def cancel_entry(message: Message, state: FSMContext):
    """Отмена ввода"""
    await message.answer("Операция отменена.", reply_markup=get_main_keyboard())
    await state.clear()

# Обработчики для "Изменить часы"
@router.message(F.text == "Изменить часы")
async def start_edit_time(message: Message, state: FSMContext):
    """Начало изменения часов"""
    await message.answer(
        "Введи дату записи, которую хочешь изменить (ГГГГ-ММ-ДД):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(EditStates.waiting_for_date)

@router.message(EditStates.waiting_for_date)
async def process_edit_date(message: Message, state: FSMContext):
    """Обработка даты для редактирования"""
    date_text = message.text.strip()
    
    if not validate_date(date_text):
        await message.answer("Неверный формат даты. Используй ГГГГ-ММ-ДД:")
        return
    
    telegram_id = message.from_user.id
    entry = get_entry_by_date(telegram_id, date_text)
    
    if not entry:
        await message.answer(
            f"Запись за {date_text} не найдена.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    # Загружаем существующие задачи
    existing_text = entry['data']['Текст']
    tasks = parse_tasks_text(existing_text)
    
    await state.update_data(
        selected_date=date_text,
        tasks=tasks,
        entry_row=entry['row']
    )
    
    formatted_tasks = format_tasks_list(tasks)
    await message.answer(
        f"Текущий список:\n{formatted_tasks}\n\nВыбери действие:",
        reply_markup=get_task_management_keyboard()
    )
    await state.set_state(EditStates.managing_tasks)

# Аналогичные обработчики для режима редактирования
@router.message(EditStates.managing_tasks, F.text == "Добавить задачу")
async def edit_add_task_menu(message: Message, state: FSMContext):
    """Меню добавления задачи при редактировании"""
    await message.answer(
        "Выбери способ добавления:",
        reply_markup=get_add_task_keyboard()
    )

@router.message(EditStates.managing_tasks, F.text == "Шаблон")
async def edit_show_templates(message: Message, state: FSMContext):
    """Показать шаблоны при редактировании"""
    if not templates_cache:
        await message.answer("Шаблоны не загружены. Используй 'Другие активности'.")
        return
    
    await message.answer(
        "Выбери шаблон:",
        reply_markup=get_templates_keyboard()
    )
    await state.set_state(EditStates.waiting_for_template_choice)

@router.callback_query(EditStates.waiting_for_template_choice, F.data.startswith("template_"))
async def edit_process_template_choice(callback_query, state: FSMContext):
    """Обработка выбора шаблона при редактировании"""
    template_index = int(callback_query.data.split("_")[1])
    
    if 0 <= template_index < len(templates_cache):
        template_name = templates_cache[template_index]
        await state.update_data(current_template=template_name)
        
        await callback_query.message.edit_text(
            f"Выбран шаблон: {template_name}"
        )
        await callback_query.message.answer(
            f"Введи количество часов для '{template_name}':"
        )
        await state.set_state(EditStates.waiting_for_hours)
    else:
        await callback_query.answer("Шаблон не найден")

@router.callback_query(EditStates.waiting_for_template_choice, F.data == "cancel_template")
async def edit_cancel_template_choice(callback_query, state: FSMContext):
    """Отмена выбора шаблона при редактировании"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    await callback_query.message.edit_text("Выбор шаблона отменён")
    await show_edit_task_list(callback_query.message, state, tasks)

async def show_edit_task_list(message: Message, state: FSMContext, tasks: List[str]):
    """Показать список задач при редактировании"""
    formatted_tasks = format_tasks_list(tasks)
    
    await message.answer(
        f"Текущий список:\n{formatted_tasks}\n\nВыбери действие:",
        reply_markup=get_task_management_keyboard()
    )
    await state.set_state(EditStates.managing_tasks)

@router.message(EditStates.waiting_for_hours)
async def edit_process_hours_input(message: Message, state: FSMContext):
    """Обработка ввода часов при редактировании"""
    hours_text = message.text.strip()
    
    if not validate_hours(hours_text):
        await message.answer("Введи корректное число часов (например: 2, 1.5, 3.75):")
        return
    
    data = await state.get_data()
    template_name = data.get('current_template', '')
    tasks = data.get('tasks', [])
    
    hours = hours_text.replace(',', '.')
    task_text = f"{template_name} - {hours} часа"
    tasks = add_task_to_list(tasks, task_text)
    
    await state.update_data(tasks=tasks)
    await message.answer(f"Добавлено: {template_name} - {hours} часа")
    await show_edit_task_list(message, state, tasks)

@router.message(EditStates.managing_tasks, F.text == "Другие активности")
async def edit_custom_activity(message: Message, state: FSMContext):
    """Ввод произвольной активности при редактировании"""
    await message.answer(
        "Расскажи, что выходящего за рамки ты сегодня сделал.\n\n"
        "Формат: Название задачи - часы\n"
        "Пример: Документация - 1,5 часа\n\n"
        "Ты можешь написать сразу несколько задач, каждую с новой строки.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(EditStates.waiting_for_custom_activity)

@router.message(EditStates.waiting_for_custom_activity)
async def edit_process_custom_activity(message: Message, state: FSMContext):
    """Обработка произвольной активности при редактировании"""
    text = message.text.strip()
    lines = text.split('\n')
    
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    for line in lines:
        if line.strip():
            tasks = add_task_to_list(tasks, line.strip())
    
    await state.update_data(tasks=tasks)
    await message.answer(f"Добавлено задач: {len(lines)}")
    await show_edit_task_list(message, state, tasks)

@router.message(EditStates.managing_tasks, F.text == "Назад к списку")
async def edit_back_to_task_list(message: Message, state: FSMContext):
    """Возврат к списку задач при редактировании"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    await show_edit_task_list(message, state, tasks)

@router.message(EditStates.managing_tasks, F.text == "Редактировать строку")
async def edit_line_start_edit(message: Message, state: FSMContext):
    """Начало редактирования строки при изменении"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    if not tasks:
        await message.answer("Нет строк для редактирования")
        return
    
    formatted_tasks = format_tasks_list(tasks)
    await message.answer(
        f"Текущие строки:\n{formatted_tasks}\n\nВведи номер строки для редактирования:"
    )
    await state.set_state(EditStates.waiting_for_edit_line_number)

@router.message(EditStates.waiting_for_edit_line_number)
async def edit_process_edit_line_number(message: Message, state: FSMContext):
    """Обработка номера строки при редактировании"""
    try:
        line_number = int(message.text.strip()) - 1
        data = await state.get_data()
        tasks = data.get('tasks', [])
        
        if 0 <= line_number < len(tasks):
            await state.update_data(edit_line=line_number)
            await message.answer(
                f"Текущий текст: {tasks[line_number]}\nВведи новый текст:"
            )
            await state.set_state(EditStates.waiting_for_edit_new_text)
        else:
            await message.answer(f"Строка не найдена. Всего строк: {len(tasks)}")
    except ValueError:
        await message.answer("Введи корректный номер строки")

@router.message(EditStates.waiting_for_edit_new_text)
async def edit_process_edit_new_text(message: Message, state: FSMContext):
    """Обработка нового текста строки при редактировании"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    line_number = data.get('edit_line', 0)
    
    new_text = message.text.strip()
    tasks = edit_task_in_list(tasks, line_number, new_text)
    
    await state.update_data(tasks=tasks, edit_line=None)
    await message.answer("Строка обновлена")
    await show_edit_task_list(message, state, tasks)

@router.message(EditStates.managing_tasks, F.text == "Удалить строку")
async def edit_delete_line_start(message: Message, state: FSMContext):
    """Начало удаления строки при редактировании"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    
    if not tasks:
        await message.answer("Нет строк для удаления")
        return
    
    formatted_tasks = format_tasks_list(tasks)
    await message.answer(
        f"Текущие строки:\n{formatted_tasks}\n\nВведи номер строки для удаления:"
    )
    await state.set_state(EditStates.waiting_for_delete_line_number)

@router.message(EditStates.waiting_for_delete_line_number)
async def edit_process_delete_line_number(message: Message, state: FSMContext):
    """Обработка удаления строки при редактировании"""
    try:
        line_number = int(message.text.strip()) - 1
        data = await state.get_data()
        tasks = data.get('tasks', [])
        
        if 0 <= line_number < len(tasks):
            tasks = delete_task_from_list(tasks, line_number)
            await state.update_data(tasks=tasks)
            await message.answer("Строка удалена")
            await show_edit_task_list(message, state, tasks)
        else:
            await message.answer(f"Строка не найдена. Всего строк: {len(tasks)}")
    except ValueError:
        await message.answer("Введи корректный номер строки")

@router.message(EditStates.managing_tasks, F.text == "Готово")
async def edit_finish_entry(message: Message, state: FSMContext):
    """Завершение редактирования и сохранение"""
    data = await state.get_data()
    tasks = data.get('tasks', [])
    entry_row = data.get('entry_row')
    
    if not tasks:
        await message.answer("Список задач пуст. Добавьте хотя бы одну задачу.")
        return
    
    # Форматируем текст для сохранения
    text_to_save = format_tasks_list(tasks)
    
    try:
        update_entry(entry_row, text_to_save)
        await message.answer(
            "Запись обновлена!",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
    except Exception as e:
        await message.answer("Ошибка при обновлении записи. Попробуй снова.")
        logger.error(f"Ошибка обновления: {e}")

@router.message(EditStates.managing_tasks, F.text == "Отмена")
async def edit_cancel_entry(message: Message, state: FSMContext):
    """Отмена редактирования"""
    await message.answer("Операция отменена.", reply_markup=get_main_keyboard())
    await state.clear()

# Функция отправки напоминаний
async def send_reminders():
    """Отправка напоминаний пользователям без записей за сегодня"""
    try:
        current_time = datetime.now(MOSCOW_TZ)
        
        # Проверяем, что сейчас 19:00
        if current_time.hour == 19 and current_time.minute == 0:
            logger.info("Запуск отправки напоминаний")
            
            today_date = current_time.strftime('%Y-%m-%d')
            
            # Получаем всех пользователей
            users_sheet = spreadsheet.worksheet('Users')
            users = users_sheet.get_all_records()
            
            # Получаем все записи за сегодня
            chrono_sheet = spreadsheet.worksheet('Chrono')
            all_records = chrono_sheet.get_all_records()
            
            # Собираем имена пользователей с записями за сегодня
            users_with_entries = set()
            for record in all_records:
                if record['Дата и время создания'] and record['Дата и время создания'].startswith(today_date):
                    users_with_entries.add(record['ФИО'])
            
            # Отправляем напоминания пользователям без записей
            for user in users:
                if user['full_name'] not in users_with_entries:
                    try:
                        await bot.send_message(
                            chat_id=user['telegram_id'],
                            text=f"Напоминание {user['full_name']}, внеси часы за сегодня!\n\nНажми 'Ввести часы'",
                            reply_markup=get_main_keyboard()
                        )
                        logger.info(f"Отправлено напоминание пользователю {user['full_name']}")
                        await asyncio.sleep(1)  # Небольшая задержка между сообщениями
                    except Exception as e:
                        logger.error(f"Ошибка отправки напоминания пользователю {user['telegram_id']}: {e}")
    
    except Exception as e:
        logger.error(f"Ошибка в функции отправки напоминаний: {e}")

async def main():
    """Главная функция запуска бота"""
    global spreadsheet
    
    # Инициализация Google Sheets
    spreadsheet = init_google_sheets()
    load_templates()
    
    # Запуск Flask в отдельном потоке
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    # Настройка планировщика для напоминаний
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(send_reminders, 'cron', minute='*')  # Проверка каждую минуту
    scheduler.start()
    
    # Запуск бота
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())