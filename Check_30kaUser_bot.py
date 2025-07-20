import os
import pymysql
import logging
from contextlib import contextmanager
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, ChatJoinRequestHandler

# Загружаем переменные окружения из .env файла (для локальной разработки)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # В production dotenv может отсутствовать

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Вспомогательная функция для безопасного получения переменных окружения
def get_env_var(var_name, default=None, var_type=str):
    """
    Безопасно получает переменную окружения с обработкой пустых строк
    """
    value = os.environ.get(var_name)
    if not value or value.strip() == "":
        if default is not None:
            return var_type(default)
        return None
    
    try:
        return var_type(value.strip())
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid value for {var_name}: {value}. Using default: {default}")
        return var_type(default) if default is not None else None

# Настройки из окружения
BOT_TOKEN = get_env_var("BOT_TOKEN")
DB_HOST = get_env_var("DB_HOST")
DB_PORT = get_env_var("DB_PORT", 3306, int)
DB_NAME = get_env_var("DB_NAME")
DB_USER = get_env_var("DB_USER")
DB_PASSWORD = get_env_var("DB_PASSWORD")
DB_TABLE = get_env_var("DB_TABLE", "users")  # Имя таблицы с выпускниками
WEBHOOK_URL = get_env_var("WEBHOOK_URL")
GROUP_ID = get_env_var("GROUP_ID", 0, int)
PORT = get_env_var("PORT", 10000, int)

# Проверяем наличие обязательных переменных
required_values = {
    "BOT_TOKEN": BOT_TOKEN,
    "DB_HOST": DB_HOST,
    "DB_NAME": DB_NAME,
    "DB_USER": DB_USER,
    "DB_PASSWORD": DB_PASSWORD,
    "WEBHOOK_URL": WEBHOOK_URL
}
missing_vars = [var for var, value in required_values.items() if not value]
if missing_vars:
    logger.error(f"Missing or empty required environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing or empty required environment variables: {', '.join(missing_vars)}")

# Flask-приложение
app = Flask(__name__)

# Контекстный менеджер для подключения к БД
@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            conn.close()

# Нормализация ФИО с улучшенной логикой
def normalize_fio(raw_fio):
    """
    Нормализует ФИО: приводит к нижнему регистру, убирает лишние пробелы,
    создает множество из 2-3 частей для гибкого сравнения
    """
    if not raw_fio:
        return set()
    
    parts = [part.strip().lower() for part in raw_fio.strip().split() if part.strip()]
    
    # Удаляем отчество если оно есть (берем максимум 2 части)
    if len(parts) > 2:
        parts = parts[:2]
    
    return set(parts)

def format_year_for_db(year_str):
    """Форматирует год для поиска в БД (добавляет .00)"""
    try:
        year_int = int(year_str)
        return f"{year_int}.00"
    except ValueError:
        logger.warning(f"Invalid year format: {year_str}")
        return None

def format_class_for_db(class_str):
    """Форматирует класс для поиска в БД (добавляет .00)"""
    try:
        class_int = int(class_str)
        return f"{class_int}.00"
    except ValueError:
        logger.warning(f"Invalid class format: {class_str}")
        return None

# Проверка выпускника с улучшенной логикой
def check_user(fio, year, klass):
    """
    Проверяет наличие пользователя в БД с гибким сравнением ФИО
    """
    if not (fio and year and klass):
        return False
        
    fio_set = normalize_fio(fio)
    if not fio_set:
        return False
    
    # Форматируем год и класс для БД
    formatted_year = format_year_for_db(year)
    formatted_class = format_class_for_db(klass)
    
    if not (formatted_year and formatted_class):
        return False
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Используем f-строку для имени таблицы (безопасно, так как имя контролируется нами)
                query = f"SELECT fio FROM {DB_TABLE} WHERE year = %s AND klass = %s"
                cursor.execute(query, (formatted_year, formatted_class))
                rows = cursor.fetchall()
                
                for row in rows:
                    db_fio_set = normalize_fio(row['fio'])
                    
                    # Проверяем совпадение: все части введенного ФИО должны присутствовать в БД
                    # Это позволяет работать с перестановками и отсутствием отчества
                    if fio_set.issubset(db_fio_set) or db_fio_set.issubset(fio_set):
                        logger.info(f"User found: {fio} -> {row['fio']}")
                        return True
                        
    except Exception as e:
        logger.error(f"Error checking user: {e}")
        return False
    
    logger.info(f"User not found: {fio}, {year}, {klass}")
    return False

# Парсинг входного текста
def parse_text(text):
    """
    Парсит текст заявки и извлекает ФИО, год и класс
    Поддерживает различные форматы ввода
    """
    if not text:
        return None, None, None
        
    lines = text.split('\n')
    data = {}
    
    for line in lines:
        line = line.strip()
        if ':' in line:
            key, val = line.split(':', 1)
            key_lower = key.strip().lower()
            val_clean = val.strip()
            
            # Нормализуем ключи
            if key_lower in ['фио', 'фамилия имя', 'имя фамилия', 'fio']:
                data['фио'] = val_clean
            elif key_lower in ['год', 'год выпуска', 'year']:
                data['год'] = val_clean
            elif key_lower in ['класс', 'class', 'группа']:
                data['класс'] = val_clean
    
    return data.get('фио'), data.get('год'), data.get('класс')

# Обработка заявки
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает заявку на вступление в группу"""
    try:
        user_id = update.chat_join_request.from_user.id
        text = update.chat_join_request.bio or ""
        
        logger.info(f"Processing join request from user {user_id} with bio: {text}")
        
        fio, year, klass = parse_text(text)

        if not (fio and year and klass):
            logger.info(f"Declining request from {user_id}: incomplete data")
            await context.bot.decline_chat_join_request(update.chat.id, user_id)
            return

        if check_user(fio, year, klass):
            logger.info(f"Approving request from {user_id}")
            await context.bot.approve_chat_join_request(update.chat.id, user_id)
        else:
            logger.info(f"Declining request from {user_id}: user not found in database")
            await context.bot.decline_chat_join_request(update.chat.id, user_id)
            
    except Exception as e:
        logger.error(f"Error handling join request: {e}")
        # В случае ошибки отклоняем заявку
        try:
            await context.bot.decline_chat_join_request(
                update.chat.id, 
                update.chat_join_request.from_user.id
            )
        except:
            pass

# Telegram application
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(ChatJoinRequestHandler(handle_join_request))

# Flask endpoint for Telegram webhook
@app.route("/", methods=["POST"])
def webhook():
    """Webhook endpoint для получения обновлений от Telegram"""
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        telegram_app.update_queue.put(update)
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# Установка webhook
async def setup_webhook():
    """Устанавливает webhook для бота"""
    try:
        await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/")
        logger.info(f"Webhook set to {WEBHOOK_URL}/")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

# Запуск
if __name__ == "__main__":
    import asyncio
    
    # Устанавливаем webhook при запуске
    asyncio.run(setup_webhook())
    
    logger.info("Starting Flask application")
    app.run(host="0.0.0.0", port=PORT, debug=False)
