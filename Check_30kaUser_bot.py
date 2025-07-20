import os
import pymysql
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, ChatJoinRequestHandler

# Настройки из окружения
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
GROUP_ID = int(os.environ["GROUP_ID"])

# Flask-приложение
app = Flask(__name__)

# Подключение к MySQL
def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

# Нормализация ФИО
def normalize_fio(raw_fio):
    parts = raw_fio.strip().lower().split()
    if len(parts) == 3:
        parts = parts[:2]  # Убираем отчество
    return set(parts)

# Проверка выпускника
def check_user(fio, year, klass):
    fio_set = normalize_fio(fio)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT fio FROM users WHERE year = %s AND klass = %s", (year, klass))
            rows = cursor.fetchall()
            for row in rows:
                db_fio_set = normalize_fio(row['fio'])
                if fio_set == db_fio_set:
                    return True
    finally:
        conn.close()
    return False

# Парсинг входного текста — настроить под твою форму ввода
def parse_text(text):
    # Пример: "ФИО: Иван Петров\nГод: 2015\nКласс: 3"
    lines = text.split('\n')
    data = {}
    for line in lines:
        if ':' in line:
            key, val = line.split(':', 1)
            data[key.strip().lower()] = val.strip()
    return data.get('фио'), data.get('год'), data.get('класс')

# Обработка заявки
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.chat_join_request.bio or ""
    fio, year, klass = parse_text(text)

    if not (fio and year and klass):
        await context.bot.decline_chat_join_request(update.chat.id, update.chat_join_request.from_user.id)
        return

    if check_user(fio, year, klass):
        await context.bot.approve_chat_join_request(update.chat.id, update.chat_join_request.from_user.id)
    else:
        await context.bot.decline_chat_join_request(update.chat.id, update.chat_join_request.from_user.id)

# Telegram application
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(ChatJoinRequestHandler(handle_join_request))

# Flask endpoint for Telegram webhook
@app.post("/")
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put(update)
    return "ok"

# Установка webhook при запуске
@app.before_first_request
def setup_webhook():
    telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/")

# Запуск
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
