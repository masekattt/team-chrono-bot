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

# ========== КОНФИГ ==========
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN missing")

SHEET_ID = "1pKNd-VfzUEK3A7D-p1lZTGpvZHtO7UPIRevn4YZqSSQ"  # ЗАМЕНИТЕ НА ВАШ

TIMEZONE = pytz.timezone("Europe/Moscow")
REMINDER_HOUR = 19
REMINDER_MINUTE = 0

GALLERY = [
    "https://images.unsplash.com/photo-1506784983877-45594efa4cbe",
]

# ========== GOOGLE SHEETS ==========
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

# ========== FSM ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class ChronoState(StatesGroup):
    waiting_for_text = State()
    waiting_for_edit_text = State()  # новое состояние для ожидания текста при редактировании

# ========== РАБОТА С ТАБЛИЦЕЙ ==========
def get_user_name(tg_id):
    try:
        cell = users_ws.find(str(tg_id))
        return users_ws.cell(cell.row, 2).value
    except:
        return None

def save_user(tg_id, name):
    users_ws.append_row([tg_id, name])

def get_today_record(name):
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        if row["ФИО"] == name and str(row["Дата и время создания"]).startswith(today):
            return idx, row["Текст"]
    return None

def create_record(name, text):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.append_row([name, text, now, ""])

def get_all_user_dates(name):
    """Возвращает список всех дат, за которые у пользователя есть записи"""
    records = chrono_ws.get_all_records()
    dates = []
    for row in records:
        if row["ФИО"] == name:
            date_str = str(row["Дата и время создания"]).split()[0]
            if date_str not in dates:
                dates.append(date_str)
    return sorted(dates, reverse=True)  # свежие сверху

def get_record_by_date(name, target_date):
    records = chrono_ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        created_date = str(row["Дата и время создания"]).split()[0]
        if row["ФИО"] == name and created_date == target_date:
            return idx, row["Текст"]
    return None

def update_record(row_idx, new_text):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    chrono_ws.update(f"B{row_idx}", [[new_text]])
    chrono_ws.update(f"D{row_idx}", [[now]])

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
async def add_chrono(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    if get_today_record(name):
        await msg.answer("❌ Сегодня уже введено. Используйте ✏️ Изменить часы")
        return
    await msg.answer("📝 Отправь список задач на сегодня (многострочный):")
    await state.set_state(ChronoState.waiting_for_text)
    await state.update_data(full_name=name)

@dp.message(ChronoState.waiting_for_text)
async def save_chrono(msg: types.Message, state: FSMContext):
    if len(msg.text.strip()) < 5:
        await msg.answer("Слишком коротко. Напиши хотя бы одно дело.")
        return
    data = await state.get_data()
    create_record(data["full_name"], msg.text)
    await msg.answer("✅ Запись сохранена!", reply_markup=main_kb)
    await state.clear()

@dp.message(lambda m: m.text == "✏️ Изменить часы")
async def edit_chrono_choose_date(msg: types.Message, state: FSMContext):
    name = get_user_name(msg.from_user.id)
    if not name:
        await msg.answer("Сначала /start")
        return
    
    dates = get_all_user_dates(name)
    if not dates:
        await msg.answer("❌ У тебя пока нет записей. Сначала введи часы через ✍️ Ввести часы")
        return
    
    # Создаём кнопки с датами (по 3 в ряд)
    keyboard = []
    row = []
    for i, date in enumerate(dates):
        row.append(InlineKeyboardButton(text=date, callback_data=f"edit_{date}"))
        if (i + 1) % 3 == 0 or i == len(dates) - 1:
            keyboard.append(row)
            row = []
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="edit_cancel")])
    
    await msg.answer("📅 Выбери дату, за которую хочешь изменить часы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(lambda c: c.data and c.data.startswith("edit_"))
async def process_edit_date(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "edit_cancel":
        await callback.message.edit_text("❌ Отменено", reply_markup=None)
        await callback.message.answer("Главное меню:", reply_markup=main_kb)
        return
    
    date_str = callback.data.replace("edit_", "")
    name = get_user_name(callback.from_user.id)
    
    record = get_record_by_date(name, date_str)
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
        await msg.answer("❌ Слишком коротко. Отправь нормальный список задач (минимум 5 символов).")
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
        
        users = users_ws.get_all_records()
        for user in users:
            tg_id = int(user["telegram_id"])
            name = user["full_name"]
            if not get_today_record(name):
                try:
                    img = random.choice(GALLERY)
                    await bot.send_photo(tg_id, img, caption=f"🔔 {name}, внеси часы за сегодня!")
                    await bot.send_message(tg_id, "Кнопки внизу:", reply_markup=main_kb)
                except:
                    pass

# ========== ЗАПУСК ==========
async def main():
    asyncio.create_task(reminder_loop())
    print("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
