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
ADMIN_ID = get_env_var("ADMIN_ID", 0, int)  # ID админа для уведомлений

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

# Whitelist проверенных пользователей (временное хранение)
verified_users = set()

# Состояния пошагового ввода
user_states = {}  # {user_id: {'step': 'waiting_name'/'waiting_year'/'waiting_class', 'data': {...}}}

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
    
    # Формат с двоеточиями
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
    
    if data.get('фио') and data.get('год') and data.get('класс'):
        return data.get('фио'), data.get('год'), data.get('класс')
    
    # Умный парсинг строки "Федоров Сергей 2010 2"
    parts = text.strip().split()
    if len(parts) >= 3:
        # Ищем год (4 цифры) и класс (1-2 цифры)
        year_part = None
        class_part = None
        name_parts = []
        
        for part in parts:
            if part.isdigit():
                if len(part) == 4 and 1950 <= int(part) <= 2030:  # Год
                    year_part = part
                elif len(part) in [1, 2] and 1 <= int(part) <= 11:  # Класс
                    class_part = part
                else:
                    name_parts.append(part)
            else:
                name_parts.append(part)
        
        if year_part and class_part and len(name_parts) >= 2:
            fio = ' '.join(name_parts)
            return fio, year_part, class_part
    
    return None, None, None

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
        
        # Проверяем реальное значение bio
        actual_bio = update.chat_join_request.bio
        logger.info(f"Actual bio value: {repr(actual_bio)}")
        logger.info(f"Bio type: {type(actual_bio)}")
        
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
        
        # Проверяем whitelist проверенных пользователей
        if user_id in verified_users:
            logger.info(f"User {user_id} is in whitelist, approving automatically")
            try:
                await context.bot.approve_chat_join_request(chat_id, user_id)
                logger.info(f"Successfully approved request from verified user {user_id}")
                # Удаляем из whitelist после использования
                verified_users.discard(user_id)
                return
            except Exception as e:
                logger.error(f"Failed to approve request from {user_id}: {e}")
        
        # Если bio отсутствует, отклоняем и отправляем инструкции
        if not bio:
            logger.info(f"Declining request from {user_id}: no bio provided")
            
            # Отклоняем заявку
            try:
                await context.bot.decline_chat_join_request(chat_id, user_id)
                logger.info(f"Successfully declined request from {user_id}")
            except Exception as e:
                logger.error(f"Failed to decline request from {user_id}: {e}")
            
            # Отправляем инструкции пользователю в личные сообщения с кнопкой
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                
                instruction_message = (
                    "❌ Ваша заявка на вступление в чат выпускников 30ки отклонена.\n\n"
                    "Это админ чата Сергей Федоров, 1983-2.\n\n"
                    "Для вступления необходимо подтвердить что вы выпускник школы.\n\n"
                    "Пожалуйста, отправьте мне данные в одном из форматов:\n\n"
                    "📝 Простой: Федоров Сергей 2010 2\n"
                    "📋 Структурированный:\n"
                    "ФИО: Иван Петров\n"
                    "Год: 2015\n"
                    "Класс: 3\n"
                    "🤖 Пошагово: /start\n\n"
                    "После проверки данных повторно подайте заявку - она будет одобрена автоматически."
                )
                
                # Создаем кнопку для связи с админом
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🆘 Произошла ошибка, я точно выпускник ФМЛ 30",
                        callback_data=f"admin_help_{user_id}"
                    )]
                ])
                
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=instruction_message,
                    reply_markup=keyboard
                )
                logger.info(f"Sent instructions with admin button to user {user_id}")
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
            
            # Запускаем в существующем event loop
            import asyncio
            
            def run_sync():
                try:
                    # Создаем новый event loop в отдельном потоке
                    import threading
                    result = []
                    error = []
                    
                    def thread_worker():
                        try:
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            new_loop.run_until_complete(process_join_request())
                            new_loop.close()
                            result.append("success")
                        except Exception as e:
                            error.append(str(e))
                            logger.error(f"Error in thread_worker: {e}")
                    
                    thread = threading.Thread(target=thread_worker)
                    thread.start()
                    thread.join(timeout=30)  # Ждем максимум 30 секунд
                    
                    if error:
                        logger.error(f"Join request processing failed: {error[0]}")
                    elif result:
                        logger.info("Join request processed successfully")
                    else:
                        logger.warning("Join request processing timed out")
                        
                except Exception as e:
                    logger.error(f"Error in run_sync: {e}")
            
            run_sync()
        elif update.callback_query:
            # Обрабатываем нажатия на кнопки
            logger.info("Processing callback query")
            
            async def process_callback():
                try:
                    await handle_callback_query(update, telegram_app)
                except Exception as e:
                    logger.error(f"Error in process_callback: {e}")
            
            # Запускаем в отдельном потоке
            def run_callback():
                try:
                    import threading
                    result = []
                    error = []
                    
                    def thread_worker():
                        try:
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            new_loop.run_until_complete(process_callback())
                            new_loop.close()
                            result.append("success")
                        except Exception as e:
                            error.append(str(e))
                            logger.error(f"Error in callback thread_worker: {e}")
                    
                    thread = threading.Thread(target=thread_worker)
                    thread.start()
                    thread.join(timeout=10)  # Ждем максимум 10 секунд
                    
                    if error:
                        logger.error(f"Callback processing failed: {error[0]}")
                    elif result:
                        logger.info("Callback processed successfully")
                    else:
                        logger.warning("Callback processing timed out")
                        
                except Exception as e:
                    logger.error(f"Error in run_callback: {e}")
            
            run_callback()
        elif update.message and update.message.chat.type.name == 'PRIVATE':
            # Обрабатываем личные сообщения с данными пользователя
            logger.info("Processing private message with user data")
            user_id = update.message.from_user.id
            text = update.message.text or ""
            
            logger.info(f"Received private message from {user_id}: {text}")
            
            # Проверяем команды
            if text.strip().lower() == '/start':
                def run_start():
                    try:
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(start_step_input(user_id, telegram_app))
                        new_loop.close()
                    except Exception as e:
                        logger.error(f"Error in start command: {e}")
                
                import threading
                thread = threading.Thread(target=run_start)
                thread.start()
                return "ok"
            
            # Проверяем состояние пошагового ввода
            if user_id in user_states:
                def run_step():
                    try:
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(handle_step_input(user_id, text, telegram_app))
                        new_loop.close()
                    except Exception as e:
                        logger.error(f"Error in step input: {e}")
                
                import threading
                thread = threading.Thread(target=run_step)
                thread.start()
                return "ok"
            
            # Парсим данные (умный парсинг + формат с двоеточиями)
            fio, year, klass = parse_text(text)
            
            if fio and year and klass:
                # Проверяем пользователя в базе
                if check_user(fio, year, klass):
                    # Добавляем в whitelist
                    verified_users.add(user_id)
                    response = (
                        f"✅ Отлично! Вы найдены в базе выпускников:\n"
                        f"ФИО: {fio}\n"
                        f"Год: {year}\n"
                        f"Класс: {klass}\n\n"
                        f"Теперь подайте заявку на вступление в группу - она будет одобрена автоматически.\n\n"
                        f"Ссылка на группу: https://t.me/test_bots_nf"
                    )
                else:
                    # Отправляем сообщение с кнопкой админа
                    await send_not_found_message(user_id, fio, year, klass, telegram_app)
                    return "ok"
            else:
                # Предлагаем пошаговый ввод
                response = (
                    "Неполные данные!\n\n"
                    "Вы можете отправить данные в любом из форматов:\n\n"
                    "1️⃣ Одной строкой: Федоров Сергей 2010 2\n\n"
                    "2️⃣ С двоеточиями:\n"
                    "ФИО: Ваше Имя Фамилия\n"
                    "Год: 2015\n"
                    "Класс: 3\n\n"
                    "3️⃣ Или отправьте /start для пошагового ввода"
                )
            
            # Отправляем ответ
            try:
                import asyncio
                
                async def send_response():
                    from telegram.ext import CallbackContext
                    context = CallbackContext(application=telegram_app)
                    await context.bot.send_message(chat_id=user_id, text=response)
                    logger.info(f"Sent response to user {user_id}")
                
                def run_response():
                    try:
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(send_response())
                        new_loop.close()
                    except Exception as e:
                        logger.error(f"Error sending response: {e}")
                
                import threading
                thread = threading.Thread(target=run_response)
                thread.start()
                
            except Exception as e:
                logger.error(f"Error processing private message: {e}")
        else:
            logger.info("No chat_join_request found in update")
        
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# Пошаговый ввод данных
async def handle_step_input(user_id, text, telegram_app):
    """Обрабатывает пошаговый ввод данных пользователя"""
    try:
        state = user_states[user_id]
        step = state['step']
        
        if text.strip().lower() == '/cancel':
            del user_states[user_id]
            response = "Ввод данных отменен. Отправьте /start чтобы начать заново."
        elif step == 'waiting_name':
            # Проверяем что введено имя и фамилия
            name_parts = text.strip().split()
            if len(name_parts) >= 2:
                state['data']['fio'] = text.strip()
                state['step'] = 'waiting_year'
                response = "Отлично! Теперь введите год окончания школы (например: 2015):"
            else:
                response = "Пожалуйста, введите имя и фамилию (например: Иван Петров):"
        elif step == 'waiting_year':
            # Проверяем год
            if text.strip().isdigit() and 1950 <= int(text.strip()) <= 2030:
                state['data']['year'] = text.strip()
                state['step'] = 'waiting_class'
                response = "Хорошо! Теперь введите номер класса (1-11):"
            else:
                response = "Пожалуйста, введите корректный год (например: 2015):"
        elif step == 'waiting_class':
            # Проверяем класс
            if text.strip().isdigit() and 1 <= int(text.strip()) <= 11:
                state['data']['class'] = text.strip()
                
                # Проверяем пользователя в базе
                fio = state['data']['fio']
                year = state['data']['year']
                klass = state['data']['class']
                
                if check_user(fio, year, klass):
                    verified_users.add(user_id)
                    response = (
                        f"✅ Отлично! Вы найдены в базе выпускников:\n"
                        f"ФИО: {fio}\n"
                        f"Год: {year}\n"
                        f"Класс: {klass}\n\n"
                        f"Теперь подайте заявку на вступление в группу - она будет одобрена автоматически.\n\n"
                        f"Ссылка на группу: https://t.me/test_bots_nf"
                    )
                else:
                    # Отправляем сообщение с кнопкой админа
                    await send_not_found_message(user_id, fio, year, klass, telegram_app)
                    # Удаляем состояние
                    del user_states[user_id]
                    return
                
                # Удаляем состояние
                del user_states[user_id]
            else:
                response = "Пожалуйста, введите корректный номер класса (1-11):"
        
        # Отправляем ответ
        await send_message(user_id, response, telegram_app)
        
    except Exception as e:
        logger.error(f"Error in step input: {e}")
        if user_id in user_states:
            del user_states[user_id]

async def start_step_input(user_id, telegram_app):
    """Начинает пошаговый ввод данных"""
    user_states[user_id] = {
        'step': 'waiting_name',
        'data': {}
    }
    response = (
        "👋 Привет! Давайте введем ваши данные пошагово.\n\n"
        "Введите ваше имя и фамилию (например: Иван Петров):\n\n"
        "Отправьте /cancel чтобы отменить."
    )
    await send_message(user_id, response, telegram_app)

async def send_message(user_id, text, telegram_app):
    """Отправляет сообщение пользователю"""
    try:
        from telegram.ext import CallbackContext
        context = CallbackContext(application=telegram_app)
        await context.bot.send_message(chat_id=user_id, text=text)
        logger.info(f"Sent message to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending message to {user_id}: {e}")

async def send_not_found_message(user_id, fio, year, klass, telegram_app):
    """Отправляет сообщение о том что пользователь не найден с кнопкой админа"""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        message = (
            f"❌ К сожалению, в базе выпускников не найден:\n"
            f"ФИО: {fio}\n"
            f"Год: {year}\n"
            f"Класс: {klass}\n\n"
            f"Возможные причины:\n"
            f"• Опечатка в написании ФИО\n"
            f"• Указан неверный год выпуска или класс\n"
            f"• Данные отсутствуют в базе школы\n\n"
            f"Проверьте правильность данных и попробуйте еще раз или обратитесь к администратору."
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🆘 Произошла ошибка, я точно выпускник ФМЛ 30",
                callback_data=f"admin_help_{user_id}"
            )]
        ])
        
        from telegram.ext import CallbackContext
        context = CallbackContext(application=telegram_app)
        await context.bot.send_message(
            chat_id=user_id, 
            text=message,
            reply_markup=keyboard
        )
        logger.info(f"Sent 'not found' message with admin button to user {user_id}")
        
    except Exception as e:
        logger.error(f"Error sending not found message to {user_id}: {e}")

async def handle_callback_query(update, telegram_app):
    """Обрабатывает нажатия на inline кнопки"""
    try:
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data
        
        logger.info(f"Callback query from {user_id}: {data}")
        
        if data.startswith("admin_help_"):
            # Пользователь нажал кнопку "Произошла ошибка"
            user_info = query.from_user
            username = f"@{user_info.username}" if user_info.username else "без username"
            
            # Отвечаем пользователю
            await query.answer("Ваш запрос отправлен администратору")
            
            user_message = (
                "✅ Ваш запрос отправлен администратору.\n\n"
                "Администратор свяжется с вами в ближайшее время для решения вопроса.\n\n"
                "Пожалуйста, ожидайте ответа."
            )
            
            await send_message(user_id, user_message, telegram_app)
            
            # Уведомляем админа
            if ADMIN_ID != 0:
                admin_message = (
                    f"🆘 ЗАПРОС НА ПОМОЩЬ ОТ ПОЛЬЗОВАТЕЛЯ\n\n"
                    f"👤 Пользователь: {user_info.first_name} {user_info.last_name or ''}\n"
                    f"📧 Username: {username}\n"
                    f"🆔 ID: {user_id}\n"
                    f"📱 Язык: {user_info.language_code or 'не указан'}\n\n"
                    f"💬 Сообщение: Пользователь утверждает что является выпускником ФМЛ 30, но не найден в базе данных.\n\n"
                    f"🔗 Для ответа перейдите в чат: tg://user?id={user_id}"
                )
                
                try:
                    await send_message(ADMIN_ID, admin_message, telegram_app)
                    logger.info(f"Sent admin notification about user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")
            else:
                logger.warning("ADMIN_ID not configured, cannot notify admin")
                
    except Exception as e:
        logger.error(f"Error handling callback query: {e}")
        try:
            await query.answer("Произошла ошибка, попробуйте еще раз")
        except:
            pass

# Установка webhook
async def setup_webhook():
    """Устанавливает webhook для бота"""
    try:
        await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/")
        logger.info(f"Webhook set to {WEBHOOK_URL}/")
        
        # Автоматическое обновление описания группы отключено
        # Чтобы включить - раскомментируйте код ниже и установите правильный GROUP_ID
        # if GROUP_ID != 0:
        #     try:
        #         description = "🎓 Чат выпускников школы №30..."
        #         await telegram_app.bot.set_chat_description(chat_id=GROUP_ID, description=description)
        #         logger.info(f"Group description updated for {GROUP_ID}")
        #     except Exception as e:
        #         logger.warning(f"Could not update group description: {e}")
        
        logger.info("Automatic group description update is disabled")
        logger.info(f"Current GROUP_ID setting: {GROUP_ID}")
        logger.info(f"Bot will process requests from any group for debugging")
        
        # Проверяем настройки группы
        if GROUP_ID != 0:
            try:
                chat_info = await telegram_app.bot.get_chat(chat_id=GROUP_ID)
                logger.info(f"Group info: {chat_info}")
                logger.info(f"Group type: {chat_info.type}")
                logger.info(f"Join by request: {getattr(chat_info, 'join_by_request', 'Not available')}")
                logger.info(f"Has protected content: {getattr(chat_info, 'has_protected_content', 'Not available')}")
            except Exception as e:
                logger.warning(f"Could not get group info: {e}")
                
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
