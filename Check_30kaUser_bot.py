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

# === СПИСОК ЗАПРЕЩЕННЫХ СЛОВ ===
FORBIDDEN_WORDS = {
    'penis', 'dick', 'cock', 'pussy', 'vagina', 'fuck', 'shit', 'bitch', 'whore', 'slut',
    'хуй', 'пизда', 'блять', 'ебать', 'сука', 'блядь', 'хуя', 'пиздец', 'еблан', 'ебало',
    'faggot', 'nigger', 'nigga', 'kike', 'spic', 'chink', 'gook', 'wop', 'kraut',
    'пидор', 'пидорас', 'гомик', 'лесбиянка', 'педик', 'гей', 'лесби', 'транс',
    'asshole', 'cunt', 'twat', 'bastard', 'motherfucker', 'fucker', 'dumbass',
    'мудак', 'мудила', 'говнюк', 'говно', 'дерьмо', 'говнюк', 'мудак', 'идиот',
    'retard', 'idiot', 'moron', 'stupid', 'dumb', 'retarded',
    'дебил', 'идиот', 'тупой', 'дурак', 'придурок', 'кретин', 'дегенерат'
}

def contains_forbidden_words(text):
    """
    Проверяет текст на наличие запрещенных слов
    Возвращает список найденных запрещенных слов или None если ничего не найдено
    """
    if not text:
        return None
    
    text_lower = text.lower().strip()
    found_words = []
    
    for word in FORBIDDEN_WORDS:
        if word in text_lower:
            found_words.append(word)
    
    return found_words if found_words else None

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

def check_user_names(first_name=None, last_name=None, username=None):
    """
    Проверяет имя, фамилию и никнейм пользователя на запрещенные слова
    Возвращает кортеж (is_valid, forbidden_words_found, message)
    """
    all_names = []
    if first_name:
        all_names.append(('имя', first_name))
    if last_name:
        all_names.append(('фамилия', last_name))
    if username:
        all_names.append(('никнейм', username))
    
    found_forbidden = []
    
    for name_type, name_value in all_names:
        forbidden = contains_forbidden_words(name_value)
        if forbidden:
            found_forbidden.extend([f"{name_type}: {word}" for word in forbidden])
    
    if found_forbidden:
        message = (
            "❌ Заявка отклонена!\n\n"
            "В вашем имени, фамилии или никнейме обнаружены некорректные слова:\n"
            f"{', '.join(found_forbidden)}\n\n"
            "Пожалуйста, измените свои данные в настройках Telegram и попробуйте снова."
        )
        return False, found_forbidden, message
    
    return True, [], None

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
    """Нормализует ФИО для гибкого сравнения, заменяя ё на е"""
    if not raw_fio:
        return set()
    def norm(s):
        return s.strip().lower().replace('ё', 'е')
    parts = [norm(part) for part in raw_fio.strip().split() if part.strip()]
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
async def send_message(user_id, text, context_or_app, reply_markup=None, parse_mode=None):
    """Универсальная отправка сообщений"""
    try:
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
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        logger.info(f"Sent message to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending message to {user_id}: {e}")

# === ТЕКСТОВЫЕ СООБЩЕНИЯ ===
INSTRUCTION_MESSAGE = (
    "К сожалению, я тебя не понял, давай попробуем еще раз. Напиши мне ФИ год класс, или /start.\n\n"
)

async def send_admin_user_status(approved, fio, year, klass, username=None, group_link=None, teacher=None, telegram_app=None, first_name=None, last_name=None, user_id=None, chat_id=None):
    """Отправляет только главному админу (ADMIN_ID) сообщение о принятии или отклонении пользователя"""
    if not telegram_app or not Config.ADMIN_ID:
        return
    
    username_display = f"@{username}" if username else '(нет username)'
    extra_info = f"first_name, last_name: {first_name or ''}, {last_name or ''}\nuser_id: {user_id or ''}\n"
    
    if approved:
        admin_msg = (
            f"✅ В чат {group_link or ''} принят новый пользователь {username_display}:\n"
            f"ФИО: {fio}\n"
            f"Год выпуска: {year}\n"
            f"Класс: {klass}\n"
            f"Кл.рук.: {teacher}\n"
            f"{extra_info}"
        )
    else:
        admin_msg = (
            f"❌ В чат {group_link or ''} постучался пользователь {username_display}, но не был найден в базе и был отклонен:\n"
            f"ФИО: {fio}\n"
            f"Год выпуска: {year}\n"
            f"Класс: {klass}\n"
            f"Кл.рук.: {teacher}\n"
            f"{extra_info}"
        )
    
    try:
        await send_message(Config.ADMIN_ID, admin_msg, telegram_app)
        logger.info(f"Sent user status notification to admin {Config.ADMIN_ID}")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу {Config.ADMIN_ID}: {e}")

async def get_admin_username(bot):
    try:
        admin_id = Config.ADMIN_ID
        if not admin_id:
            return "admin"
        admin_user = await bot.get_chat(admin_id)
        if getattr(admin_user, 'username', None):
            return f"@{admin_user.username}"
        else:
            return "admin"
    except Exception as e:
        logger.error(f"Не удалось получить username админа: {e}")
        return "admin"

async def send_not_found_message(user_id, fio, year, klass, context_or_app):
    """Отправляет сообщение о том что пользователь не найден (без кнопки, с ником админа)"""
    admin_username = await get_admin_username(context_or_app.bot if hasattr(context_or_app, 'bot') else context_or_app)
    message = (
        "К сожалению, мы не нашли тебя в базе данных <b>и не можем принять в чат</b>, этот чат только для выпускников лицея.\n\n"
        "Проверь правильность введенных данных:\n"
        f"ФИО: {fio}\n"
        f"Год: {year}\n"
        f"Класс: {klass}\n"
        "Для <b>исправления данных снова</b> нажми /start\n\n"
        f"Если <b>данные верные и</b> ты точно выпускник(ца) <b>30ки</b>, напиши <b>админу чата</b> Сергею в личку {admin_username} — мы обязательно разберёмся!\n"
    )
    await send_message(user_id, message, context_or_app, parse_mode="HTML")

def create_instruction_message():
    """Создает стандартное сообщение с инструкциями"""
    return INSTRUCTION_MESSAGE

# Глобальные переменные
verified_users = set()  # Whitelist проверенных пользователей
user_states = {}        # Состояния пошагового ввода

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

def make_success_message(fio, year, klass, teacher=None, admin_username=None, group_link=None):
    teacher_block = f"Классный руководитель: {teacher}\n\n" if teacher and teacher != '-' else ""
    if admin_username is None:
        admin_username = "admin"
    # --- Изменено: если group_link не передан, берем из окружения GROUP_LINK, иначе дефолт ---
    if group_link is None:
        group_link = os.environ.get("GROUP_LINK") or ""
    return (
        "✅ Рады знакомству! Ты найден(а) в базе выпускников.\n"
        f"Теперь подай заявку на вступление в чат - она будет одобрена автоматически, ссылка: {group_link}\n\n"
        f"Рекомендуем опубликовать в чате инфо о себе (год выпуска, чем занимаешься и т.п.) с тегом #ктоя\n\n"
        f"Админ чата Сергей Федоров, 1983-2, {admin_username}. Если будут вопросы по Клубу, Фонду30, сайту 30ka.ru, чату, школе - не стесняйся мне их задавать!"
    )

def make_admin_error_message(admin_username):
    return (
        f"Произошла ошибка при одобрении заявки. Пожалуйста, попробуй позже или напиши администратору {admin_username}."
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
        user_info = update.chat_join_request.from_user
        username = f"@{user_info.username}" if user_info.username else user_info.first_name
        
        # === ПРОВЕРКА НА ЗАПРЕЩЕННЫЕ СЛОВА ===
        is_valid_names, forbidden_words, forbidden_message = check_user_names(
            first_name=user_info.first_name,
            last_name=user_info.last_name,
            username=user_info.username
        )
        
        if not is_valid_names:
            logger.info(f"Declining request from {user_id}: forbidden words found - {forbidden_words}")
            try:
                await context.bot.decline_chat_join_request(chat_id, user_id)
                await send_message(user_id, forbidden_message, context)
            except Exception as e:
                logger.error(f"Error declining join request due to forbidden words: {e}")
            return
        
        # === УВЕДОМЛЕНИЕ АДМИНУ О НОВОЙ ЗАЯВКЕ ===
        if Config.ADMIN_ID:
            try:
                admin_notification = (
                    f"🆕 НОВАЯ ЗАЯВКА НА ВСТУПЛЕНИЕ В ЧАТ\n\n"
                    f"👤 Пользователь: {user_info.first_name} {user_info.last_name or ''}\n"
                    f"📧 Никнейм: @{user_info.username if user_info.username else '(нет username)'}\n"
                    f"🆔 ID: {user_id}\n"
                    f"📝 Bio: {bio if bio else '(нет bio)'}\n\n"
                    f"🔗 Для ответа перейдите в чат: tg://user?id={user_id}"
                )
                await send_message(Config.ADMIN_ID, admin_notification, context)
                logger.info(f"Sent join request notification to admin {Config.ADMIN_ID}")
            except Exception as e:
                logger.error(f"Error sending join request notification to admin: {e}")
        
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
                    admin_username = await get_admin_username(context.bot)
                    await send_message(user_id, make_admin_error_message(admin_username), context)
                except Exception as e2:
                    logger.error(f"Error sending error message to user: {e2}")
            return
        # Если bio отсутствует
        if not bio:
            logger.info(f"Declining request from {user_id}: no bio")
            logger.info(f"Request should be declined for user {user_id}. User should write to bot directly.")
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
                # --- Обновляем поле in_chat в базе ---
                try:
                    from datetime import datetime
                    today = datetime.utcnow().date()
                    # Получаем username или user_id
                    tg_username_val = user_info.username if user_info.username else str(user_id)
                    with get_db_connection() as conn:
                        with conn.cursor() as cursor:
                            update_query = f"""
                                UPDATE {Config.DB_TABLE}
                                SET in_chat = %s, tg_username = %s
                                WHERE year = %s AND klass = %s AND (
                                    lower(replace(fio, 'ё', 'е')) = lower(replace(%s, 'ё', 'е'))
                                    OR lower(replace(fio, 'е', 'ё')) = lower(replace(%s, 'е', 'ё'))
                                )
                            """
                            cursor.execute(update_query, (str(today), tg_username_val, format_for_db(year, "year"), format_for_db(klass, "class"), fio, fio))
                            logger.info(f"Updated in_chat and tg_username for user: {fio}, {year}, {klass} -> {today}, {tg_username_val}")
                except Exception as e:
                    logger.error(f"Error updating in_chat/tg_username in DB: {e}")
                # Получаем ссылку на чат
                chat_info = await context.bot.get_chat(chat_id)
                group_link = None
                if getattr(chat_info, 'username', None):
                    group_link = f"https://t.me/{chat_info.username}"
                elif getattr(chat_info, 'invite_link', None):
                    group_link = chat_info.invite_link
                else:
                    group_link = "https://t.me/"
                admin_username = await get_admin_username(context.bot)
                # Отправляем личное сообщение пользователю с правильной ссылкой
                response = make_success_message(fio, year, klass, admin_username=admin_username, group_link=group_link)
                await send_message(user_id, response, context)
            except Exception as e:
                logger.error(f"Error approving request: {e}")
                try:
                    admin_username = await get_admin_username(context.bot)
                    await send_message(user_id, make_admin_error_message(admin_username), context)
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
            admin_username = await get_admin_username(context.bot)
            await send_message(user_id, make_admin_error_message(admin_username), context)
        except Exception as e2:
            logger.error(f"Error sending error message to user: {e2}")

async def handle_private_message(user_id, text, telegram_app):
    # Исключение для админа
    if int(user_id) == int(Config.ADMIN_ID) and text.strip().lower() == '/start':
        await send_message(user_id, "Привет, я проверяю заявки в чате выпускников 30ки. Сюда будут приходить одобренные и отклонённые заявки.", telegram_app)
        return
    
    # === ПРОВЕРКА НА ЗАПРЕЩЕННЫЕ СЛОВА ===
    try:
        user_info = await telegram_app.bot.get_chat(user_id)
        is_valid_names, forbidden_words, forbidden_message = check_user_names(
            first_name=user_info.first_name,
            last_name=user_info.last_name,
            username=user_info.username
        )
        
        if not is_valid_names:
            logger.info(f"Rejecting private message from {user_id}: forbidden words found - {forbidden_words}")
            await send_message(user_id, forbidden_message, telegram_app)
            return
    except Exception as e:
        logger.error(f"Error checking user names for {user_id}: {e}")
        # Если не удалось получить информацию о пользователе, продолжаем обработку
    # Если пользователь в процессе пошагового ввода — только handle_step_input!
    if user_id in user_states:
        await handle_step_input(user_id, text, telegram_app)
        return
    # Команда /start или первое сообщение
    if text.strip().lower() == '/start':
        await start_step_input(user_id, telegram_app)
        return
    # --- Новое: если пользователь не в user_states и написал только два слова (ФИО), запускаем пошаговый сценарий ---
    name_parts = text.strip().split()
    if len(name_parts) == 2 and all(part.isalpha() for part in name_parts):
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
            admin_username = await get_admin_username(telegram_app.bot)
            response = make_success_message(fio, year, klass, admin_username=admin_username)
            await send_message(user_id, response, telegram_app)
            # --- Уведомление админу через send_admin_user_status ---
            username = None
            first_name = None
            last_name = None
            try:
                user = await telegram_app.bot.get_chat(user_id)
                username = user.username
                first_name = user.first_name
                last_name = user.last_name
            except Exception:
                pass
            group_link = os.environ.get("GROUP_LINK")
            await send_admin_user_status(True, fio, year, klass, username=username, group_link=group_link, telegram_app=telegram_app, first_name=first_name, last_name=last_name, user_id=user_id, chat_id=None)
        else:
            await send_not_found_message(user_id, fio, year, klass, telegram_app)
            # --- Уведомление админу об отказе через send_admin_user_status ---
            username = None
            first_name = None
            last_name = None
            try:
                user = await telegram_app.bot.get_chat(user_id)
                username = user.username
                first_name = user.first_name
                last_name = user.last_name
            except Exception:
                pass
            group_link = os.environ.get("GROUP_LINK")
            await send_admin_user_status(False, fio, year, klass, username=username, group_link=group_link, telegram_app=telegram_app, first_name=first_name, last_name=last_name, user_id=user_id, chat_id=None)
    else:
        await send_message(user_id, INCOMPLETE_DATA_MESSAGE, telegram_app)

async def handle_step_input(user_id, text, telegram_app, chat_id=None):
    try:
        # === ПРОВЕРКА НА ЗАПРЕЩЕННЫЕ СЛОВА ===
        try:
            user_info = await telegram_app.bot.get_chat(user_id)
            is_valid_names, forbidden_words, forbidden_message = check_user_names(
                first_name=user_info.first_name,
                last_name=user_info.last_name,
                username=user_info.username
            )
            
            if not is_valid_names:
                logger.info(f"Rejecting step input from {user_id}: forbidden words found - {forbidden_words}")
                del user_states[user_id]  # Очищаем состояние
                await send_message(user_id, forbidden_message, telegram_app)
                return
        except Exception as e:
            logger.error(f"Error checking user names for {user_id}: {e}")
            # Если не удалось получить информацию о пользователе, продолжаем обработку
        
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
                response = "Напиши Фамилию и/или Имя Отчество классного руководителя:"
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
                admin_username = await get_admin_username(telegram_app.bot)
                response = make_success_message(fio, year, klass, teacher, admin_username)
                await send_message(user_id, response, telegram_app)
                # username, first_name, last_name, user_id для админа
                username = None
                first_name = None
                last_name = None
                # Попробуем получить user из telegram_app, если возможно
                try:
                    user = await telegram_app.bot.get_chat(user_id)
                    username = user.username
                    first_name = user.first_name
                    last_name = user.last_name
                except Exception:
                    pass
                group_link = os.environ.get("GROUP_LINK")
                await send_admin_user_status(True, fio, year, klass, username=username, group_link=group_link, teacher=teacher, telegram_app=telegram_app, first_name=first_name, last_name=last_name, user_id=user_id, chat_id=chat_id)
            else:
                await send_not_found_message(user_id, fio, year, klass, telegram_app)
                username = None
                first_name = None
                last_name = None
                try:
                    user = await telegram_app.bot.get_chat(user_id)
                    username = user.username
                    first_name = user.first_name
                    last_name = user.last_name
                except Exception:
                    pass
                group_link = os.environ.get("GROUP_LINK")
                await send_admin_user_status(False, fio, year, klass, username=username, group_link=group_link, teacher=teacher, telegram_app=telegram_app, first_name=first_name, last_name=last_name, user_id=user_id, chat_id=chat_id)
            return
        await send_message(user_id, response, telegram_app)
    except Exception as e:
        logger.error(f"Error in step input: {e}")
        if user_id in user_states:
            del user_states[user_id]

async def start_step_input(user_id, telegram_app):
    """Начинает пошаговый ввод данных"""
    # === ПРОВЕРКА НА ЗАПРЕЩЕННЫЕ СЛОВА ===
    try:
        user_info = await telegram_app.bot.get_chat(user_id)
        is_valid_names, forbidden_words, forbidden_message = check_user_names(
            first_name=user_info.first_name,
            last_name=user_info.last_name,
            username=user_info.username
        )
        
        if not is_valid_names:
            logger.info(f"Rejecting step input start from {user_id}: forbidden words found - {forbidden_words}")
            await send_message(user_id, forbidden_message, telegram_app)
            return
    except Exception as e:
        logger.error(f"Error checking user names for {user_id}: {e}")
        # Если не удалось получить информацию о пользователе, продолжаем обработку
    
    user_states[user_id] = {'step': 'waiting_name', 'data': {}}
    response = (
        "👋 Привет! Спасибо за заявку в чате выпускников 30ки.\n\n"
        "Для доступа в чат необходимо подтвердить что ты выпускник 30ки.\n\n"
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
