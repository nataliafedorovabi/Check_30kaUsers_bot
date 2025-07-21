-- Создание таблицы выпускников в PostgreSQL
-- Используйте этот скрипт для создания таблицы в вашей PostgreSQL базе на Render

CREATE TABLE IF NOT EXISTS cms_users (
    id SERIAL PRIMARY KEY,
    fio VARCHAR(255) NOT NULL,
    year INTEGER NOT NULL,
    klass INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Создаем индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_cms_users_year_klass ON cms_users (year, klass);
CREATE INDEX IF NOT EXISTS idx_cms_users_fio ON cms_users (fio);

-- Примеры вставки данных (замените на ваши данные)
-- INSERT INTO cms_users (fio, year, klass) VALUES 
-- ('Иванов Иван Иванович', 2015, 5),
-- ('Петров Петр Петрович', 2010, 3),
-- ('Федоров Сергей Александрович', 2010, 2);

-- Проверка данных
-- SELECT * FROM cms_users LIMIT 10;