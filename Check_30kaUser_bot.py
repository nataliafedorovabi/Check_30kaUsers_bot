import os
import asyncio
import psycopg2
import psycopg2.extras
import logging
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, ChatJoinRequestHandler, CallbackContext, MessageHandler, CommandHandler, filters

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
        bot = None
        if hasattr(context_or_app, 'bot'):
            bot = context_or_app.bot
        elif hasattr(context_or_app, '_bot'):
            bot = context_or_app._bot
        elif hasattr(context_or_app, 'send_message'):
            bot = context_or_app
        else:
            raise ValueError("Не удалось определить объект бота для отправки сообщения")
        await bot.send_message(
            chat_id=user_id, 
            text=text,
            reply_markup=reply_markup
        )
        logger.info(f"Sent message to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending message to {user_id}: {e}")

# === ТЕКСТОВЫЕ СООБЩЕНИЯ ===
NOT_FOUND_MESSAGE = (
    "К сожалению, мы не нашли тебя в базе данных, этот чат только для выпускников лицея.\n"
    "Админ чата Сергей Федоров @{admin_id} с удовольствием расскажет тебе все, секретов нет, но у нас правила. Надеюсь на понимание. С уважением.\n"
    "Если ты точно выпускник ФМЛ 30, напиши Сергею в личку @{admin_id} — мы обязательно разберёмся!\n"
    "Или попробуй еще раз /start"
)
INSTRUCTION_MESSAGE = (
    "К сожалению, я тебя не понял, давай попробуем еще раз. Напиши мне ФИ год класс, или /start.\n\n"
)

async def get_admin_mention(bot):
    try:
        admin_id = Config.ADMIN_ID
        if not admin_id:
            return "[админ](https://t.me/)"  # fallback
        admin_user = await bot.get_chat(admin_id)
        if getattr(admin_user, 'username', None):
            return f"[@{admin_user.username}](https://t.me/{admin_user.username})"
        else:
            # Если username нет, используем имя
            name = admin_user.first_name or "админ"
            return f"[{name}](tg://user?id={admin_id})"
    except Exception as e:
        logger.error(f"Не удалось получить ссылку на админа: {e}")
        return "[админ](https://t.me/)"

async def send_not_found_message(user_id, fio, year, klass, context_or_app):
    """Отправляет сообщение о том что пользователь не найден (без кнопки, с ником админа)"""
    admin_mention = await get_admin_mention(context_or_app.bot if hasattr(context_or_app, 'bot') else context_or_app)
    message = (
        "К сожалению, мы не нашли тебя в базе данных, этот чат только для выпускников лицея.\n"
        f"Админ чата Сергей Федоров {admin_mention} с удовольствием расскажет тебе все, секретов нет, но у нас правила. Надеюсь на понимание. С уважением.\n"
        f"Если ты точно выпускник ФМЛ 30, напиши Сергею в личку {admin_mention} — мы обязательно разберёмся!\n"
        "Или попробуй еще раз /start"
    )
    await send_message(user_id, message, context_or_app)

def create_instruction_message():
    """Создает стандартное сообщение с инструкциями"""
    return INSTRUCTION_MESSAGE

# Глобальные переменные
verified_users = set()  # Whitelist проверенных пользователей
user_states = {}        # Состояния пошагового ввода

SUCCESS_MESSAGE_ADMIN = (
    "✅ Рад знакомству! Ты найден в базе выпускников:\n"
    "ФИО: {fio}\n"
    "Год: {year}\n"
    "Класс: {klass}\n"
    "{teacher_block}"
    "Теперь подай заявку на вступление в чат - она будет одобрена автоматически, ссылка: https://t.me/test_bots_nf\n\n"
    "Админ чата Сергей Федоров, 1983-2, @{admin_id}. Если будут вопросы по Клубу, Фонду30, сайту 30ka.ru, чату, школе - не стесняйся мне их задавать!"
)
INCOMPLETE_DATA_MESSAGE = (
    "Неполные данные!\n\n"
    "Ты можешь отправить данные в любом из форматов:\n\n"
    "1️⃣ Одной строкой: Федоров Сергей 2010 2\n\n"
    "2️⃣ С двоеточиями:\n"
    "ФИО: Ваше Имя Фамилия\n"
    "Год: 2015\n"
    "Класс: 3\n\n"
    "3️⃣ Или отправь /start для пошагового ввода"
)

def make_success_message(fio, year, klass, teacher=None, admin_mention=None):
    teacher_block = f"Классный руководитель: {teacher}\n\n" if teacher and teacher != '-' else ""
    if admin_mention is None:
        admin_mention = "[админ](https://t.me/)"
    return (
        "✅ Рад знакомству! Ты найден в базе выпускников:\n"
        f"ФИО: {fio}\n"
        f"Год: {year}\n"
        f"Класс: {klass}\n"
        f"{teacher_block}"
        "Теперь подай заявку на вступление в чат - она будет одобрена автоматически, ссылка: https://t.me/test_bots_nf\n\n"
        f"Админ чата Сергей Федоров, 1983-2, {admin_mention}. Если будут вопросы по Клубу, Фонду30, сайту 30ka.ru, чату, школе - не стесняйся мне их задавать!"
    )

async def handle_private_message_entrypoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    await handle_private_message(user_id, text, context)

async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await handle_private_message(user_id, "/start", context)

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
                try:
                    await send_message(user_id, "Произошла ошибка при одобрении заявки. Пожалуйста, попробуй позже или напиши администратору @{admin_id}.", context)
                except Exception as e2:
                    logger.error(f"Error sending error message to user: {e2}")
            return
        # Если bio отсутствует
        if not bio:
            logger.info(f"Declining request from {user_id}: no bio")
            logger.info(f"Request should be declined for user {user_id}. User should write to bot directly.")
            user_info = update.chat_join_request.from_user
            username = f"@{user_info.username}" if user_info.username else user_info.first_name
            try:
                bot_info = await context.bot.get_me()
                group_message = f"Привет {username}, рады видеть! Для вступления в чат выпускников ФМЛ 30, перейди в личку @{bot_info.username} и нажми start. Бот сверится с БД лицея."
                await context.bot.send_message(chat_id=chat_id, text=group_message)
                logger.info(f"✅ Sent instruction message to group for {username}")
            except Exception as e:
                logger.error(f"❌ Could not send group message for {username}: {e}")
                logger.info(f"⏳ Pending request from {username} (user_id: {user_id})")
            # Удаляем/комментируем отправку сообщения в личку:
            # try:
            #     await send_message(user_id, "Заявка отклонена, так как не указано био. Пожалуйста, напиши боту в личные сообщения для подтверждения.", context)
            # except Exception as e2:
            #     logger.error(f"Error sending decline message to user: {e2}")
            return
        # Парсим данные из bio
        fio, year, klass = parse_text(bio)
        if not (fio and year and klass):
            logger.info(f"Declining request from {user_id}: incomplete data")
            try:
                await context.bot.decline_chat_join_request(chat_id, user_id)
            except Exception as e:
                logger.error(f"Error declining join request: {e}")
            try:
                await send_message(user_id, "Заявка отклонена, так как указаны неполные данные. Пожалуйста, напиши боту в личные сообщения для подтверждения.", context)
            except Exception as e2:
                logger.error(f"Error sending decline message to user: {e2}")
            return
        # Проверяем в базе
        if check_user(fio, year, klass):
            logger.info(f"Approving request from {user_id}")
            try:
                await context.bot.approve_chat_join_request(chat_id, user_id)
                logger.info(f"Approved request from {user_id} - user found in database")
            except Exception as e:
                logger.error(f"Error approving request: {e}")
                try:
                    await send_message(user_id, "Произошла ошибка при одобрении заявки. Пожалуйста, попробуй позже или напиши администратору @{admin_id}.", context)
                except Exception as e2:
                    logger.error(f"Error sending error message to user: {e2}")
        else:
            logger.info(f"Declining request from {user_id}: user not found")
            logger.info(f"Request should be declined for {user_id} - user not found in database")
            try:
                await send_not_found_message(user_id, fio, year, klass, context)
            except Exception as e2:
                logger.error(f"Error sending not found message to user: {e2}")
    except Exception as e:
        logger.error(f"Error handling join request: {e}")
        try:
            user_id = update.chat_join_request.from_user.id
            await send_message(user_id, "Произошла внутренняя ошибка при обработке заявки. Пожалуйста, попробуй позже или напиши администратору @{admin_id}.", context)
        except Exception as e2:
            logger.error(f"Error sending error message to user: {e2}")

async def handle_private_message(user_id, text, telegram_app):
    # Исключение для админа
    if int(user_id) == int(Config.ADMIN_ID) and text.strip().lower() == '/start':
        await send_message(user_id, "Привет, я проверяю заявки в чате выпускников 30ки. Сюда будут приходить одобренные и отклонённые заявки.", telegram_app)
        return
    # Если пользователь в процессе пошагового ввода — только handle_step_input!
    if user_id in user_states:
        await handle_step_input(user_id, text, telegram_app)
        return
    # Команда /start или первое сообщение
    if text.strip().lower() == '/start':
        await start_step_input(user_id, telegram_app)
        return
    # Если пользователь написал что-то кроме данных, показываем приветствие
    if not parse_text(text)[0]:  # Если не смогли распарсить данные
        welcome_message = create_instruction_message()
        await send_message(user_id, welcome_message, telegram_app)
        return
    # Парсинг данных
    fio, year, klass = parse_text(text)
    if fio and year and klass:
        if check_user(fio, year, klass):
            verified_users.add(user_id)
            admin_mention = await get_admin_mention(telegram_app.bot)
            response = make_success_message(fio, year, klass, admin_mention=admin_mention)
            await send_message(user_id, response, telegram_app)
        else:
            await send_not_found_message(user_id, fio, year, klass, telegram_app)
    else:
        await send_message(user_id, INCOMPLETE_DATA_MESSAGE, telegram_app)

async def handle_step_input(user_id, text, telegram_app):
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
                response = "Отлично! Теперь введи год окончания школы (например: 2015):"
            else:
                response = "Пожалуйста, введи имя и фамилию (например: Иван Петров):"
        elif step == 'waiting_year':
            if text.strip().isdigit() and 1950 <= int(text.strip()) <= 2030:
                state['data']['year'] = text.strip()
                state['step'] = 'waiting_class'
                response = "Хорошо! Теперь введи номер класса (1-11):"
            else:
                response = "Пожалуйста, введи корректный год (например: 2015):"
        elif step == 'waiting_class':
            if text.strip().isdigit() and 1 <= int(text.strip()) <= 11:
                state['data']['class'] = text.strip()
                state['step'] = 'waiting_teacher'
                response = "Если помнишь, напиши ФИ классного руководителя (или просто '-' если не помнишь):"
            else:
                response = "Пожалуйста, введи корректный номер класса (1-11):"
        elif step == 'waiting_teacher':
            state['data']['teacher'] = text.strip()
            fio = state['data']['fio']
            year = state['data']['year']
            klass = state['data']['class']
            teacher = state['data']['teacher']
            del user_states[user_id]
            if check_user(fio, year, klass):
                verified_users.add(user_id)
                admin_mention = await get_admin_mention(telegram_app.bot)
                response = make_success_message(fio, year, klass, teacher, admin_mention)
                await send_message(user_id, response, telegram_app)
                # Отправить админу уведомление о принятии
                try:
                    chat_id = Config.GROUP_ID
                    chat_info = await telegram_app.bot.get_chat(chat_id)
                    group_title = chat_info.title if hasattr(chat_info, 'title') else str(chat_id)
                    admin_msg = (
                        f"В чат '{group_title}' принят новый пользователь:\n"
                        f"ФИ: {fio}\n"
                        f"Год выпуска: {year}\n"
                        f"Класс: {klass}\n"
                        f"Кл.руководитель: {teacher}"
                    )
                    if Config.ADMIN_ID:
                        await send_message(Config.ADMIN_ID, admin_msg, telegram_app)
                except Exception as e:
                    logger.error(f"Ошибка при отправке уведомления админу: {e}")
            else:
                await send_not_found_message(user_id, fio, year, klass, telegram_app)
                # Отправить админу уведомление об отказе
                try:
                    chat_id = Config.GROUP_ID
                    chat_info = await telegram_app.bot.get_chat(chat_id)
                    group_title = chat_info.title if hasattr(chat_info, 'title') else str(chat_id)
                    admin_msg = (
                        f"В чат '{group_title}' постучался пользователь, но не был найден в базе и был отклонен:\n"
                        f"ФИ: {fio}\n"
                        f"Год выпуска: {year}\n"
                        f"Класс: {klass}\n"
                        f"Кл.руководитель: {teacher}"
                    )
                    if Config.ADMIN_ID:
                        await send_message(Config.ADMIN_ID, admin_msg, telegram_app)
                except Exception as e:
                    logger.error(f"Ошибка при отправке уведомления админу: {e}")
            return
        await send_message(user_id, response, telegram_app)
    except Exception as e:
        logger.error(f"Error in step input: {e}")
        if user_id in user_states:
            del user_states[user_id]

async def start_step_input(user_id, telegram_app):
    """Начинает пошаговый ввод данных"""
    user_states[user_id] = {'step': 'waiting_name', 'data': {}}
    response = (
        "👋 Привет! Спасибо за заявку в чате выпускников 30ки.\n\n"
        "Для доступа в чат необходимо подтвердить что ты учился в лицее.\n\n"
        "Отправь мне твою фамилию и имя:"
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
    telegram_app.add_handler(CommandHandler("start", handle_start_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message_entrypoint))
    logger.info("Telegram application initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Telegram application: {e}")
    raise

# === ЗАЩИТА WEBHOOK ПО СЕКРЕТУ ===
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/"

if __name__ == "__main__":
    webhook_url = f"{Config.WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else f"{Config.WEBHOOK_URL}/"
    logger.info(f"Webhook set to {webhook_url}")
    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=Config.PORT,
        webhook_url=webhook_url
    )
