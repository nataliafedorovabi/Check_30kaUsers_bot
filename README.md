# 🤖 Check 30ka Users Bot

Telegram бот для проверки выпускников ФМЛ 30 при вступлении в группу.

## 🚀 Деплой на Render

### 1. Создайте PostgreSQL базу
```sql
CREATE TABLE cms_users (
    id SERIAL PRIMARY KEY,
    fio VARCHAR(255) NOT NULL,
    year INTEGER NOT NULL,
    klass INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Добавьте ваши данные
INSERT INTO cms_users (fio, year, klass) VALUES 
('Федоров Сергей Александрович', 2010, 2),
('Иванов Иван Иванович', 2015, 5);
```

### 2. Environment Variables в Render
```
BOT_TOKEN=ваш_telegram_bot_token
WEBHOOK_URL=https://ваш-app.onrender.com
GROUP_ID=-1002672587905
DB_HOST=ваш_postgres_host.render.com
DB_PORT=5432
DB_NAME=имя_базы
DB_USER=username
DB_PASSWORD=password
DB_TABLE=cms_users
ADMIN_ID=ваш_telegram_id (опционально)
```

### 3. Настройка Telegram группы
- Добавьте бота как администратора
- Включите "Approve new members" 
- Бот будет автоматически проверять заявки

## 🔧 Локальная разработка

1. Клонируйте репозиторий
2. Установите зависимости: `pip install -r requirements.txt`
3. Создайте `.env` файл с переменными
4. Запустите: `python Check_30kaUser_bot.py`

## 📊 Функции

- ✅ Проверка выпускников по ФИО, году и классу
- 🔍 Умный парсинг входных данных
- 👥 Пошаговый ввод данных
- 🆘 Кнопка обращения к админу
- 📝 Детальное логирование

## 🛠 Технологии

- Python 3.x
- python-telegram-bot 20.8
- PostgreSQL (psycopg2)
- Flask для webhook
- Render для хостинга