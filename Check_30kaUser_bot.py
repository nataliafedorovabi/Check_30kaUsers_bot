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

# Отключаем warning'и Werkzeug в production
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

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
DB_TABLE = get_env_var("DB_TABLE", "cms_users")  # Имя таблицы с выпускниками
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
        chat_id = update.chat_join_request.chat.id
        
        # Получаем все возможные поля из заявки
        bio = getattr(update.chat_join_request, 'bio', None)
        
        # Проверяем все атрибуты chat_join_request для отладки
        logger.info(f"ChatJoinRequest attributes: {dir(update.chat_join_request)}")
        logger.info(f"Full ChatJoinRequest: {update.chat_join_request}")
        
        # Может быть это поле называется по-другому?
        for attr in ['bio', 'message', 'text', 'comment', 'description']:
            value = getattr(update.chat_join_request, attr, None)
            if value:
                logger.info(f"Found {attr}: {value}")
        
        text = bio or ""
        
        logger.info(f"Processing join request from user {user_id} in chat {chat_id}")
        logger.info(f"Bio present: {bio is not None}, Bio content: '{text}'")
        logger.info(f"Expected GROUP_ID: {GROUP_ID}, Actual chat_id: {chat_id}")
        
        # Временно отключаем проверку GROUP_ID для отладки
        if GROUP_ID != 0 and GROUP_ID != chat_id:
            logger.warning(f"GROUP_ID mismatch! Expected: {GROUP_ID}, Got: {chat_id}")
            logger.info("Continuing processing for debugging purposes...")
        
        # Если bio отсутствует, отклоняем и отправляем инструкции
        if not bio:
            logger.info(f"Declining request from {user_id}: no bio provided")
            await context.bot.decline_chat_join_request(chat_id, user_id)
            
            # Отправляем инструкции пользователю в личные сообщения
            try:
                instruction_message = (
                    "Привет, спасибо за заявку в чате выпускников 30ки.\n\n"
                    "Это админ чата Сергей Федоров, 1983-2.\n\n"
                    "Для доступа в чат просьба повторно подать заявку и указать в описании:\n"
                    "• ФИО: [Ваши Фамилия Имя]\n"
                    "• Год: [год выпуска]\n"
                    "• Класс: [номер класса]\n\n"
                    "Пример заполнения:\n"
                    "ФИО: Иван Петров\n"
                    "Год: 2015\n"
                    "Класс: 3"
                )
                await context.bot.send_message(chat_id=user_id, text=instruction_message)
                logger.info(f"Sent instructions to user {user_id}")
            except Exception as e:
                logger.warning(f"Could not send instructions to user {user_id}: {e}")
            
            return
        
        fio, year, klass = parse_text(text)
        logger.info(f"Parsed data - FIO: '{fio}', Year: '{year}', Class: '{klass}'")

        if not (fio and year and klass):
            logger.info(f"Declining request from {user_id}: incomplete data (FIO: {bool(fio)}, Year: {bool(year)}, Class: {bool(klass)})")
            await context.bot.decline_chat_join_request(chat_id, user_id)
            
            # Отправляем инструкции о правильном формате
            try:
                missing_fields = []
                if not fio: missing_fields.append("ФИО")
                if not year: missing_fields.append("Год выпуска")
                if not klass: missing_fields.append("Класс")
                
                instruction_message = (
                    "Привет, спасибо за заявку в чате выпускников 30ки.\n\n"
                    "Это админ чата Сергей Федоров, 1983-2.\n\n"
                    f"В вашей заявке не хватает: {', '.join(missing_fields)}\n\n"
                    "Для доступа в чат просьба повторно подать заявку и указать в описании:\n"
                    "• ФИО: [Ваши Фамилия Имя]\n"
                    "• Год: [год выпуска]\n"
                    "• Класс: [номер класса]\n\n"
                    "Пример заполнения:\n"
                    "ФИО: Иван Петров\n"
                    "Год: 2015\n"
                    "Класс: 3"
                )
                await context.bot.send_message(chat_id=user_id, text=instruction_message)
                logger.info(f"Sent format instructions to user {user_id}")
            except Exception as e:
                logger.warning(f"Could not send format instructions to user {user_id}: {e}")
            
            return

        if check_user(fio, year, klass):
            logger.info(f"Approving request from {user_id}")
            await context.bot.approve_chat_join_request(chat_id, user_id)
        else:
            logger.info(f"Declining request from {user_id}: user not found in database")
            await context.bot.decline_chat_join_request(chat_id, user_id)
            
            # Отправляем сообщение о том, что пользователь не найден
            try:
                not_found_message = (
                    "Привет, спасибо за заявку в чате выпускников 30ки.\n\n"
                    "Это админ чата Сергей Федоров, 1983-2.\n\n"
                    f"К сожалению, в базе выпускников не найден пользователь:\n"
                    f"ФИО: {fio}\n"
                    f"Год выпуска: {year}\n"
                    f"Класс: {klass}\n\n"
                    "Возможные причины:\n"
                    "• Опечатка в написании ФИО\n"
                    "• Указан неверный год выпуска или класс\n"
                    "• Данные отсутствуют в базе школы\n\n"
                    "Пожалуйста, проверьте данные и подайте заявку повторно или обратитесь к администратору."
                )
                await context.bot.send_message(chat_id=user_id, text=not_found_message)
                logger.info(f"Sent 'not found' message to user {user_id}")
            except Exception as e:
                logger.warning(f"Could not send 'not found' message to user {user_id}: {e}")
            
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
try:
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(ChatJoinRequestHandler(handle_join_request))
    logger.info("Telegram application initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Telegram application: {e}")
    raise

# Flask endpoint for Telegram webhook
@app.route("/", methods=["POST"])
def webhook():
    """Webhook endpoint для получения обновлений от Telegram"""
    try:
        json_data = request.get_json(force=True)
        logger.info(f"Received webhook data: {json_data}")
        
        update = Update.de_json(json_data, telegram_app.bot)
        logger.info(f"Parsed update: {update}")
        
        # Обрабатываем update напрямую
        if update.chat_join_request:
            logger.info("Found chat_join_request, processing...")
            import asyncio
            from telegram.ext import ContextTypes
            
            async def process_join_request():
                try:
                    # Создаем контекст с application
                    from telegram.ext import CallbackContext
                    context = CallbackContext(application=telegram_app)
                    await handle_join_request(update, context)
                except Exception as e:
                    logger.error(f"Error in process_join_request: {e}")
            
            # Запускаем асинхронно
            import threading
            
            def run_async():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(process_join_request())
                    loop.close()
                except Exception as e:
                    logger.error(f"Error in async execution: {e}")
            
            thread = threading.Thread(target=run_async)
            thread.start()
        else:
            logger.info("No chat_join_request found in update")
        
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
        
        # Попробуем настроить описание группы
        if GROUP_ID != 0:
            try:
                description = (
                    "🎓 Чат выпускников школы №30\n\n"
                    "Для вступления подайте заявку с указанием в комментарии:\n"
                    "• ФИО: [Фамилия Имя]\n"  
                    "• Год: [год выпуска]\n"
                    "• Класс: [номер]\n\n"
                    "Пример:\n"
                    "ФИО: Иван Петров\n"
                    "Год: 2015\n"
                    "Класс: 3\n\n"
                    "Админ: Сергей Федоров, 1983-2"
                )
                await telegram_app.bot.set_chat_description(chat_id=GROUP_ID, description=description)
                logger.info(f"Group description updated for {GROUP_ID}")
            except Exception as e:
                logger.warning(f"Could not update group description: {e}")
                
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

# Функция инициализации для production
def init_app():
    """Инициализация приложения для production"""
    import asyncio
    try:
        asyncio.run(setup_webhook())
        logger.info("Application initialized for production")
    except Exception as e:
        logger.error(f"Failed to initialize application: {e}")

# Запуск
if __name__ == "__main__":
    import asyncio
    
    # Устанавливаем webhook при запуске
    asyncio.run(setup_webhook())
    
    logger.info("Starting Flask application in development mode")
    app.run(host="0.0.0.0", port=PORT, debug=False)
else:
    # Production mode - инициализируем при импорте
    init_app()
