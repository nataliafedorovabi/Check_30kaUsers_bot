import os
import asyncio
import threading
import psycopg2
import psycopg2.extras
import logging
from contextlib import contextmanager
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, ChatJoinRequestHandler, CallbackContext

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
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Вспомогательная функция для безопасного получения переменных окружения
def get_env_var(var_name, default=None, var_type=str):
    """Безопасно получает переменную окружения с обработкой пустых строк"""
    value = os.environ.get(var_name)
    if not value or value.strip() == "":
        return var_type(default) if default is not None else None
    
    try:
        return var_type(value.strip())
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid value for {var_name}: {value}. Using default: {default}")
        return var_type(default) if default is not None else None

# Конфигурация из окружения
class Config:
    BOT_TOKEN = get_env_var("BOT_TOKEN")
    # PostgreSQL конфигурация - поддерживаем как отдельные параметры, так и DATABASE_URL
    DATABASE_URL = get_env_var("DATABASE_URL")  # Render предоставляет полный URL
    DB_HOST = get_env_var("DB_HOST")
    DB_PORT = get_env_var("DB_PORT", 5432, int)  # PostgreSQL порт по умолчанию
    DB_NAME = get_env_var("DB_NAME")
    DB_USER = get_env_var("DB_USER")
    DB_PASSWORD = get_env_var("DB_PASSWORD")
    DB_TABLE = get_env_var("DB_TABLE", "cms_users")
    WEBHOOK_URL = get_env_var("WEBHOOK_URL")
    GROUP_ID = get_env_var("GROUP_ID", 0, int)
    PORT = get_env_var("PORT", 10000, int)
    ADMIN_ID = get_env_var("ADMIN_ID", 0, int)

# Проверяем наличие обязательных переменных
required_vars = ["BOT_TOKEN", "WEBHOOK_URL"]
if Config.DATABASE_URL:
    logger.info("Using DATABASE_URL for database connection")
else:
    required_vars.extend(["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"])
    logger.info("Using individual database parameters")

missing_vars = [var for var in required_vars if not getattr(Config, var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Flask приложение
app = Flask(__name__)

# Глобальные переменные
verified_users = set()  # Whitelist проверенных пользователей
user_states = {}  # Состояния пошагового ввода

# Утилиты для работы с асинхронностью
def run_async_in_thread(async_func, timeout=30):
    def thread_worker():
        try:
            asyncio.run(async_func())  # безопасный способ запуска корутины
            logger.info("Async processing completed successfully")
        except Exception as e:
            logger.error(f"Error in async thread: {e}")

    thread = threading.Thread(target=thread_worker)
    thread.start()
    thread.join(timeout=timeout)

# База данных
@contextmanager
def get_db_connection():
    """Контекстный менеджер для подключения к PostgreSQL"""
    conn = None
    try:
        if Config.DATABASE_URL:
            logger.info(f"Connecting to PostgreSQL using DATABASE_URL")
            conn = psycopg2.connect(
                Config.DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
                sslmode='require'  # Render требует SSL
            )
        else:
            logger.info(f"Connecting to PostgreSQL: {Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}")
            conn = psycopg2.connect(
                host=Config.DB_HOST,
                port=Config.DB_PORT,
                user=Config.DB_USER,
                password=Config.DB_PASSWORD,
                database=Config.DB_NAME,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
                sslmode='prefer'
            )
        
        conn.autocommit = True  # Включаем автокоммит
        logger.info("✅ PostgreSQL connection successful")
        yield conn
        
    except Exception as e:
        logger.error(f"❌ PostgreSQL connection error: {e}")
        if Config.DATABASE_URL:
            logger.error("Connection via DATABASE_URL failed")
        else:
            logger.error(f"Connection details - Host: {Config.DB_HOST}, Port: {Config.DB_PORT}, DB: {Config.DB_NAME}, User: {Config.DB_USER}")
        raise
    finally:
        if conn:
            conn.close()
            logger.info("PostgreSQL connection closed")

# Утилиты для работы с данными
def normalize_fio(raw_fio):
    """Нормализует ФИО для гибкого сравнения"""
    if not raw_fio:
        return set()
    
    parts = [part.strip().lower() for part in raw_fio.strip().split() if part.strip()]
    # Берем максимум 2 части (убираем отчество)
    return set(parts[:2])

def format_for_db(value, field_type="string"):
    """Форматирует значения для поиска в БД"""
    if field_type in ["year", "class"]:
        try:
            # Возвращаем integer для PostgreSQL (без .00)
            return int(value)
        except ValueError:
            logger.warning(f"Invalid {field_type} format: {value}")
            return None
    return value

def parse_text(text):
    """Универсальный парсер текста - поддерживает разные форматы"""
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
            
            if key_lower in ['фио', 'фамилия имя', 'имя фамилия', 'fio']:
                data['фио'] = val_clean
            elif key_lower in ['год', 'год выпуска', 'year']:
                data['год'] = val_clean
            elif key_lower in ['класс', 'class', 'группа']:
                data['класс'] = val_clean
    
    if data.get('фио') and data.get('год') and data.get('класс'):
        return data.get('фио'), data.get('год'), data.get('класс')
    
    # Умный парсинг "Федоров Сергей 2010 2"
    parts = text.strip().split()
    if len(parts) >= 3:
        year_part = None
        class_part = None
        name_parts = []
        
        for part in parts:
            if part.isdigit():
                if len(part) == 4 and 1950 <= int(part) <= 2030:
                    year_part = part
                elif len(part) in [1, 2] and 1 <= int(part) <= 11:
                    class_part = part
                else:
                    name_parts.append(part)
            else:
                name_parts.append(part)
        
        if year_part and class_part and len(name_parts) >= 2:
            return ' '.join(name_parts), year_part, class_part
    
    return None, None, None

def check_user(fio, year, klass):
    """Проверяет наличие пользователя в БД"""
    logger.info(f"🔍 Starting user verification - FIO: '{fio}', Year: '{year}', Class: '{klass}'")
    
    if not (fio and year and klass):
        logger.warning("❌ Invalid input data - missing required fields")
        return False
        
    fio_set = normalize_fio(fio)
    if not fio_set:
        logger.warning("❌ Invalid FIO format after normalization")
        return False
    
    formatted_year = format_for_db(year, "year")
    formatted_class = format_for_db(klass, "class")
    
    logger.info(f"📝 Normalized data - FIO parts: {fio_set}, Year: {formatted_year}, Class: {formatted_class}")
    logger.info(f"📊 Data types - Year: {type(formatted_year)}, Class: {type(formatted_class)}")
    
    if formatted_year is None or formatted_class is None:
        logger.warning("❌ Invalid year or class format for database")
        return False
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # PostgreSQL использует %s для всех типов параметров
                query = f"SELECT fio FROM {Config.DB_TABLE} WHERE year = %s AND klass = %s"
                logger.info(f"🗃️ Executing PostgreSQL query: {query}")
                logger.info(f"📊 Query parameters: year={formatted_year}, klass={formatted_class}")
                logger.info(f"📋 Using table: {Config.DB_TABLE}")
                
                cursor.execute(query, (formatted_year, formatted_class))
                rows = cursor.fetchall()
                
                logger.info(f"📈 Found {len(rows)} records in PostgreSQL for year {formatted_year}, class {formatted_class}")
                
                if rows:
                    logger.info("👥 Database records found:")
                    for i, row in enumerate(rows, 1):
                        logger.info(f"  {i}. {row['fio']}")
                
                for row in rows:
                    db_fio_set = normalize_fio(row['fio'])
                    logger.info(f"🔄 Comparing: input={fio_set} vs db={db_fio_set}")
                    
                    if fio_set.issubset(db_fio_set) or db_fio_set.issubset(fio_set):
                        logger.info(f"✅ MATCH FOUND! User verified: '{fio}' matches '{row['fio']}'")
                        return True
                        
    except Exception as e:
        logger.error(f"❌ Database query error: {e}")
        logger.error(f"Query details - Table: {Config.DB_TABLE}, Year: {formatted_year}, Class: {formatted_class}")
        return False
    
    logger.info(f"❌ NO MATCH: User '{fio}' not found in {Config.DB_TABLE} for year {formatted_year}, class {formatted_class}")
    return False

# Telegram утилиты
async def send_message(user_id, text, context_or_app, reply_markup=None):
    """Универсальная отправка сообщений"""
    try:
        # Определяем тип объекта и получаем bot
        if hasattr(context_or_app, 'bot'):  # Context
            bot = context_or_app.bot
        elif hasattr(context_or_app, '_bot'):  # Application
            bot = context_or_app._bot
        else:  # Предполагаем что это bot
            bot = context_or_app
        
        await bot.send_message(
            chat_id=user_id, 
            text=text,
            reply_markup=reply_markup
        )
        logger.info(f"Sent message to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending message to {user_id}: {e}")

async def send_not_found_message(user_id, fio, year, klass, context_or_app):
    """Отправляет сообщение о том что пользователь не найден с кнопкой админа"""
    message = (
        f"❌ К сожалению, в базе выпускников не найден:\n"
        f"ФИО: {fio}\n"
        f"Год: {year}\n"
        f"Класс: {klass}\n\n"
        f"Возможные причины:\n"
        f"• Опечатка в написании ФИО\n"
        f"• Указан неверный год выпуска или класс\n"
        f"• Данные отсутствуют в базе школы\n\n"
        f"Проверьте правильность данных и попробуйте еще раз."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🆘 Произошла ошибка, я точно выпускник ФМЛ 30",
            callback_data=f"admin_help_{user_id}"
        )]
    ])
    
    await send_message(user_id, message, context_or_app, keyboard)

def create_instruction_message():
    """Создает стандартное сообщение с инструкциями"""
    return (
        "👋 Привет! Я бот для проверки выпускников ФМЛ 30.\n\n"
        "Это админ чата Сергей Федоров, 1983-2.\n\n"
        "Для вступления в группу выпускников необходимо подтвердить что вы учились в школе.\n\n"
        "📝 Отправьте мне ваши данные в любом из форматов:\n\n"
        "▫️ Простой: Федоров Сергей 2010 2\n\n"
        "▫️ Структурированный:\n"
        "ФИО: Иван Петров\n"
        "Год: 2015\n"
        "Класс: 3\n\n"
        "▫️ Пошагово: отправьте /start\n\n"
        "После проверки данных повторно подайте заявку в группу - она будет одобрена автоматически! ✅"
    )

# Обработчики
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает заявки на вступление в группу"""
    try:
        user_id = update.chat_join_request.from_user.id
        chat_id = update.chat_join_request.chat.id
        bio = getattr(update.chat_join_request, 'bio', None)
        
        logger.info(f"Processing join request from user {user_id} in chat {chat_id}")
        logger.info(f"Bio present: {bio is not None}")
        
        # Проверяем whitelist
        if user_id in verified_users:
            logger.info(f"User {user_id} is verified, approving")
            try:
                await context.bot.approve_chat_join_request(chat_id, user_id)
                verified_users.discard(user_id)
                logger.info(f"Approved request from verified user {user_id}")
            except Exception as e:
                logger.error(f"Error approving request: {e}")
            return
        
        # Если bio отсутствует
        if not bio:
            logger.info(f"Declining request from {user_id}: no bio")
            logger.info(f"Request should be declined for user {user_id}. User should write to bot directly.")
            
            # Отправляем простое сообщение в группу синхронно
            user_info = update.chat_join_request.from_user
            username = f"@{user_info.username}" if user_info.username else user_info.first_name
            
            try:
                # Получаем username бота и отправляем простое сообщение
                bot_info = await context.bot.get_me()
                group_message = f"👋 {username}, для вступления в группу выпускников ФМЛ 30, перейди в личку @{bot_info.username} и нажми /start. Бот сверится с БД."
                await context.bot.send_message(chat_id=chat_id, text=group_message)
                logger.info(f"✅ Sent instruction message to group for {username}")
            except Exception as e:
                logger.error(f"❌ Could not send group message for {username}: {e}")
                logger.info(f"⏳ Pending request from {username} (user_id: {user_id})")
            return
        
        # Парсим данные из bio
        fio, year, klass = parse_text(bio)
        
        if not (fio and year and klass):
            logger.info(f"Declining request from {user_id}: incomplete data")
            await context.bot.decline_chat_join_request(chat_id, user_id)
            return
        
        # Проверяем в базе
        if check_user(fio, year, klass):
            logger.info(f"Approving request from {user_id}")
            try:
                await context.bot.approve_chat_join_request(chat_id, user_id)
                logger.info(f"Approved request from {user_id} - user found in database")
            except Exception as e:
                logger.error(f"Error approving request: {e}")
        else:
            logger.info(f"Declining request from {user_id}: user not found")
            logger.info(f"Request should be declined for {user_id} - user not found in database")
            # Не вызываем decline_chat_join_request из-за проблем с event loop
            # Пусть пользователь сам напишет боту
            
    except Exception as e:
        logger.error(f"Error handling join request: {e}")

async def handle_private_message(user_id, text, telegram_app):
    """Обрабатывает личные сообщения"""
    # Команда /start или первое сообщение
    if text.strip().lower() == '/start':
        await start_step_input(user_id, telegram_app)
        return
    
    # Если пользователь написал что-то кроме данных, показываем приветствие
    if not parse_text(text)[0]:  # Если не смогли распарсить данные
        welcome_message = create_instruction_message()
        await send_message(user_id, welcome_message, telegram_app)
        return
    
    # Пошаговый ввод
    if user_id in user_states:
        await handle_step_input(user_id, text, telegram_app)
        return
    
    # Парсинг данных
    fio, year, klass = parse_text(text)
    
    if fio and year and klass:
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
            await send_message(user_id, response, telegram_app)
        else:
            await send_not_found_message(user_id, fio, year, klass, telegram_app)
    else:
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
        await send_message(user_id, response, telegram_app)

async def handle_step_input(user_id, text, telegram_app):
    """Обрабатывает пошаговый ввод данных"""
    try:
        state = user_states[user_id]
        step = state['step']
        
        if text.strip().lower() == '/cancel':
            del user_states[user_id]
            await send_message(user_id, "Ввод данных отменен. Отправьте /start чтобы начать заново.", telegram_app)
            return
        
        if step == 'waiting_name':
            name_parts = text.strip().split()
            if len(name_parts) >= 2:
                state['data']['fio'] = text.strip()
                state['step'] = 'waiting_year'
                response = "Отлично! Теперь введите год окончания школы (например: 2015):"
            else:
                response = "Пожалуйста, введите имя и фамилию (например: Иван Петров):"
                
        elif step == 'waiting_year':
            if text.strip().isdigit() and 1950 <= int(text.strip()) <= 2030:
                state['data']['year'] = text.strip()
                state['step'] = 'waiting_class'
                response = "Хорошо! Теперь введите номер класса (1-11):"
            else:
                response = "Пожалуйста, введите корректный год (например: 2015):"
                
        elif step == 'waiting_class':
            if text.strip().isdigit() and 1 <= int(text.strip()) <= 11:
                state['data']['class'] = text.strip()
                
                fio = state['data']['fio']
                year = state['data']['year']
                klass = state['data']['class']
                
                del user_states[user_id]
                
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
                    await send_message(user_id, response, telegram_app)
                else:
                    await send_not_found_message(user_id, fio, year, klass, telegram_app)
                return
            else:
                response = "Пожалуйста, введите корректный номер класса (1-11):"
        
        await send_message(user_id, response, telegram_app)
        
    except Exception as e:
        logger.error(f"Error in step input: {e}")
        if user_id in user_states:
            del user_states[user_id]

async def start_step_input(user_id, telegram_app):
    """Начинает пошаговый ввод данных"""
    user_states[user_id] = {'step': 'waiting_name', 'data': {}}
    response = (
        "👋 Привет! Давайте введем ваши данные пошагово.\n\n"
        "Введите ваше имя и фамилию (например: Иван Петров):\n\n"
        "Отправьте /cancel чтобы отменить."
    )
    await send_message(user_id, response, telegram_app)

async def handle_callback_query(update, telegram_app):
    """Обрабатывает нажатия на inline кнопки"""
    try:
        query = update.callback_query
        user_id = query.from_user.id
        
        if query.data.startswith("admin_help_"):
            await query.answer("Ваш запрос отправлен администратору")
            
            user_info = query.from_user
            username = f"@{user_info.username}" if user_info.username else "без username"
            
            user_message = (
                "✅ Ваш запрос отправлен администратору.\n\n"
                "Администратор свяжется с вами в ближайшее время для решения вопроса.\n\n"
                "Пожалуйста, ожидайте ответа."
            )
            await send_message(user_id, user_message, telegram_app)
            
            if Config.ADMIN_ID:
                admin_message = (
                    f"🆘 ЗАПРОС НА ПОМОЩЬ ОТ ПОЛЬЗОВАТЕЛЯ\n\n"
                    f"👤 Пользователь: {user_info.first_name} {user_info.last_name or ''}\n"
                    f"📧 Username: {username}\n"
                    f"🆔 ID: {user_id}\n"
                    f"📱 Язык: {user_info.language_code or 'не указан'}\n\n"
                    f"💬 Сообщение: Пользователь утверждает что является выпускником ФМЛ 30, но не найден в базе данных.\n\n"
                    f"🔗 Для ответа перейдите в чат: tg://user?id={user_id}"
                )
                await send_message(Config.ADMIN_ID, admin_message, telegram_app)
                
    except Exception as e:
        logger.error(f"Error handling callback query: {e}")

# Инициализация Telegram
try:
    telegram_app = ApplicationBuilder().token(Config.BOT_TOKEN).build()
    telegram_app.add_handler(ChatJoinRequestHandler(handle_join_request))
    logger.info("Telegram application initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Telegram application: {e}")
    raise

# Flask routes
@app.route("/", methods=["POST"])
def webhook():
    """Webhook endpoint для получения обновлений от Telegram"""
    try:
        json_data = request.get_json(force=True)
        logger.info(f"Received webhook data: {json_data}")
        
        update = Update.de_json(json_data, telegram_app.bot)
        
        if update.chat_join_request:
            logger.info("Processing chat_join_request")
            async def process_join():
                context = CallbackContext(application=telegram_app)
                await handle_join_request(update, context)
            run_async_in_thread(process_join)
            
        elif update.callback_query:
            logger.info("Processing callback_query")
            async def process_callback():
                await handle_callback_query(update, telegram_app)
            run_async_in_thread(process_callback, timeout=10)
            
        elif update.message and update.message.chat.type.name == 'PRIVATE':
            logger.info("Processing private message")
            user_id = update.message.from_user.id
            text = update.message.text or ""
            
            async def process_message():
                await handle_private_message(user_id, text, telegram_app)
            run_async_in_thread(process_message)
            
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# Database connection testing
def test_database_connection():
    """Тестирует разные варианты подключения к PostgreSQL"""
    logger.info("🧪 Testing PostgreSQL connection with different parameters...")
    
    connection_params = []
    
    # Если есть DATABASE_URL, тестируем его
    if Config.DATABASE_URL:
        connection_params.append({
            "name": "DATABASE_URL connection",
            "params": {
                "dsn": Config.DATABASE_URL,
                "cursor_factory": psycopg2.extras.RealDictCursor,
                "connect_timeout": 10,
                "sslmode": 'require'
            }
        })
    
    # Если есть отдельные параметры, тестируем их
    if Config.DB_HOST:
        connection_params.extend([
            {
                "name": "Individual parameters with SSL required",
                "params": {
                    "host": Config.DB_HOST,
                    "port": Config.DB_PORT,
                    "user": Config.DB_USER,
                    "password": Config.DB_PASSWORD,
                    "database": Config.DB_NAME,
                    "cursor_factory": psycopg2.extras.RealDictCursor,
                    "connect_timeout": 10,
                    "sslmode": 'require'
                }
            },
            {
                "name": "Individual parameters with SSL preferred",
                "params": {
                    "host": Config.DB_HOST,
                    "port": Config.DB_PORT,
                    "user": Config.DB_USER,
                    "password": Config.DB_PASSWORD,
                    "database": Config.DB_NAME,
                    "cursor_factory": psycopg2.extras.RealDictCursor,
                    "connect_timeout": 10,
                    "sslmode": 'prefer'
                }
            }
        ])
    
    for test in connection_params:
        try:
            logger.info(f"🔌 Testing: {test['name']}")
            if 'dsn' in test['params']:
                # Подключение через DATABASE_URL
                dsn = test['params'].pop('dsn')
                conn = psycopg2.connect(dsn, **test['params'])
            else:
                # Подключение через отдельные параметры
                conn = psycopg2.connect(**test['params'])
            
            conn.autocommit = True  # Включаем автокоммит
            logger.info(f"✅ {test['name']} - SUCCESS!")
            
            # Проверяем версию PostgreSQL и доступные схемы
            with conn.cursor() as cursor:
                cursor.execute("SELECT version()")
                version_row = cursor.fetchone()
                version = version_row['version'] if version_row else "Unknown"
                logger.info(f"📊 PostgreSQL version: {version}")
                
                cursor.execute("SELECT schema_name FROM information_schema.schemata")
                schemas = cursor.fetchall()
                schema_names = [schema['schema_name'] for schema in schemas]
                logger.info(f"📋 Available schemas: {schema_names}")
            
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"❌ {test['name']} - FAILED: {e}")
    
    return False

# Database verification
def verify_database():
    """Проверяет подключение к базе данных и структуру таблицы"""
    logger.info("🔍 Verifying database connection and table structure...")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Проверяем существование таблицы (PostgreSQL синтаксис)
                cursor.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public'
                """)
                tables = cursor.fetchall()
                table_names = [table['table_name'] for table in tables]
                
                logger.info(f"📋 Available tables: {table_names}")
                
                if Config.DB_TABLE not in table_names:
                    logger.error(f"❌ Table '{Config.DB_TABLE}' not found in database!")
                    return False
                
                logger.info(f"✅ Table '{Config.DB_TABLE}' exists")
                
                # Проверяем структуру таблицы (PostgreSQL синтаксис)
                cursor.execute(f"""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns 
                    WHERE table_name = %s AND table_schema = 'public'
                    ORDER BY ordinal_position
                """, (Config.DB_TABLE,))
                columns = cursor.fetchall()
                
                logger.info(f"📊 Table '{Config.DB_TABLE}' structure:")
                for col in columns:
                    logger.info(f"  - {col['column_name']}: {col['data_type']} (Nullable: {col['is_nullable']}, Default: {col['column_default']})")
                
                # Проверяем наличие необходимых полей
                required_fields = ['fio', 'year', 'klass']
                column_names = [col['column_name'] for col in columns]
                
                missing_fields = [field for field in required_fields if field not in column_names]
                if missing_fields:
                    logger.error(f"❌ Missing required fields: {missing_fields}")
                    return False
                
                logger.info("✅ All required fields present")
                
                # Проверяем количество записей
                cursor.execute(f"SELECT COUNT(*) as count FROM {Config.DB_TABLE}")
                count_result = cursor.fetchone()
                total_records = count_result['count']
                
                logger.info(f"📈 Total records in table: {total_records}")
                
                # Показываем несколько примеров записей
                cursor.execute(f"SELECT fio, year, klass FROM {Config.DB_TABLE} LIMIT 5")
                sample_rows = cursor.fetchall()
                
                if sample_rows:
                    logger.info("📝 Sample records:")
                    for i, row in enumerate(sample_rows, 1):
                        logger.info(f"  {i}. FIO: {row['fio']}, Year: {row['year']}, Class: {row['klass']}")
                else:
                    logger.warning("⚠️ No records found in table")
                
                logger.info("✅ Database verification completed successfully")
                return True
                
    except Exception as e:
        logger.error(f"❌ Database verification failed: {e}")
        return False

# Setup
async def setup_webhook():
    """Устанавливает webhook для бота"""
    try:
        # Логируем настройки PostgreSQL (без пароля)
        logger.info("🔧 PostgreSQL configuration:")
        if Config.DATABASE_URL:
            logger.info(f"  DATABASE_URL: {Config.DATABASE_URL[:50]}...{Config.DATABASE_URL[-20:] if len(Config.DATABASE_URL) > 70 else Config.DATABASE_URL}")
        logger.info(f"  Host: {Config.DB_HOST}")
        logger.info(f"  Port: {Config.DB_PORT}")
        logger.info(f"  Database: {Config.DB_NAME}")
        logger.info(f"  User: {Config.DB_USER}")
        logger.info(f"  Table: {Config.DB_TABLE}")
        logger.info(f"  Password length: {len(Config.DB_PASSWORD) if Config.DB_PASSWORD else 0} chars")
        
        # Тестируем подключение к базе данных
        logger.info("🔧 Testing database connectivity...")
        if not test_database_connection():
            logger.error("❌ All database connection tests failed")
        
        # Проверяем базу данных перед запуском
        if not verify_database():
            logger.error("❌ Database verification failed - bot may not work correctly")
        
        await telegram_app.bot.set_webhook(f"{Config.WEBHOOK_URL}/")
        logger.info(f"Webhook set to {Config.WEBHOOK_URL}/")
        logger.info(f"Bot configured for GROUP_ID: {Config.GROUP_ID}")
        
        if Config.GROUP_ID:
            try:
                chat_info = await telegram_app.bot.get_chat(chat_id=Config.GROUP_ID)
                logger.info(f"Group: {chat_info.title} ({chat_info.type})")
            except Exception as e:
                logger.warning(f"Could not get group info: {e}")
                
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

def init_app():
    """Инициализация для production"""
    try:
        asyncio.run(setup_webhook())
        logger.info("Application initialized for production")
    except Exception as e:
        logger.error(f"Failed to initialize application: {e}")

# Запуск
if __name__ == "__main__":
    asyncio.run(setup_webhook())
    logger.info("Starting Flask application")
    app.run(host="0.0.0.0", port=Config.PORT, debug=False)
else:
    init_app()
