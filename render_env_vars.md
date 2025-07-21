# Переменные окружения для Render

## Обязательные переменные:

### 1. BOT_TOKEN
**Описание:** Токен вашего Telegram бота  
**Значение:** `7626457924:AAFeNv49XlsEzEzm1cnuIYQc6RsJN_PyO_I` (ваш токен)

### 2. WEBHOOK_URL  
**Описание:** URL вашего приложения на Render  
**Значение:** `https://check-30kausers-bot.onrender.com` (замените на ваш URL)

### 3. GROUP_ID
**Описание:** ID Telegram группы (с минусом!)  
**Значение:** `-1002672587905` (ваш ID группы)

### 4. DB_TABLE
**Описание:** Имя таблицы в PostgreSQL  
**Значение:** `cms_users`

## Опциональные переменные:

### 5. ADMIN_ID
**Описание:** Ваш Telegram ID для уведомлений  
**Значение:** `408206240` (ваш ID)

### 6. PORT
**Описание:** Порт для Flask (Render устанавливает автоматически)  
**Значение:** `10000` (можно не указывать)

## 📝 Важно:
- **DATABASE_URL** создается автоматически Render при создании PostgreSQL базы
- Отдельные DB_HOST, DB_PORT, DB_USER, DB_PASSWORD **НЕ НУЖНЫ** при использовании DATABASE_URL
- Все переменные добавляются в разделе "Environment" вашего Web Service на Render

## 🚀 Итого для добавления в Render:
```
BOT_TOKEN=7626457924:AAFeNv49XlsEzEzm1cnuIYQc6RsJN_PyO_I
WEBHOOK_URL=https://check-30kausers-bot.onrender.com
GROUP_ID=-1002672587905
DB_TABLE=cms_users
ADMIN_ID=408206240
```

DATABASE_URL будет создан автоматически!