import os
import json
from datetime import date

import asyncpg
from logger_config import logger

DATABASE_URL = os.getenv("DATABASE_URL")

# ИЗМЕНЕНО: вместо открытия нового соединения на каждый вызов (как было с
# aiosqlite.connect(DB_NAME) в каждой функции) держим один общий пул
# соединений на всё приложение. Это стандартная практика для Postgres -
# открывать соединение на каждый запрос слишком дорого, а пул переиспользует
# уже установленные TCP-соединения. get_pool() лениво создаёт пул при первом
# обращении и переиспользует его дальше.
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL)
    return _pool


async def close_pool():
    """Вызывать при остановке приложения, чтобы аккуратно закрыть соединения."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# НОВОЕ: Postgres, в отличие от SQLite, не приводит типы параметров сам -
# если в колонку INTEGER передать Python str '2', asyncpg упадёт с
# DataError, хотя SQLite молча преобразовал бы строку в число. А строкой
# id почти всегда и приходит - Telegram callback_data и aiogram FSM-состояния
# всегда строки, даже когда в них цифры. Чтобы не расставлять int(...) в
# каждом хендлере вручную (и не забыть где-то одно место), нормализуем типы
# централизованно прямо здесь, на входе в функции работы с БД.

def _as_int(value):
    """Приводит value к int. None остаётся None (например, необязательный
    category_id для лимита без категории)."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool - подкласс int в Python (True == 1), но здесь это почти
        # наверняка ошибка вызывающего кода, а не осознанный int(True) -
        # пусть будет явная ошибка, а не тихо записанная единица.
        raise TypeError(f"_as_int: ожидалось число, получен bool: {value!r}")
    return int(value)


def _as_bool(value):
    """Приводит value к bool. Помимо реальных bool/int, понимает то, что
    может прийти строкой из callback_data/FSM: '1'/'0', 'true'/'false'
    (в любом регистре), 'да'/'нет'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "да")
    return bool(value)


def _as_date(value):
    """Приводит value к datetime.date. FSM-состояния aiogram нередко
    хранят дату строкой ('2025-01-01') после сериализации через JSON -
    Postgres-колонка DATE ожидает конкретно datetime.date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ИЗМЕНЕНО: PRAGMA foreign_keys = ON не нужен - в Postgres внешние
        # ключи включены всегда по умолчанию, отдельной настройки нет.

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                id BIGINT NOT NULL UNIQUE,
                phone TEXT NOT NULL,
                name TEXT,
                session_json TEXT,
                is_phone_owner BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )

        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_owner_per_phone
            ON users(phone)
            WHERE is_phone_owner = TRUE
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pre_users(
                id BIGINT NOT NULL UNIQUE,
                name TEXT
            )
            """
        )

        # ИЗМЕНЕНО: добавлена колонка is_protected - признак "защищённой"
        # (обязательной) категории, которая не участвует в автоматическом
        # перераспределении лимитов при пересчёте по общему бюджету (см.
        # services/budget_forecast.py, PROTECT_CATEGORIES). По умолчанию
        # FALSE (не защищена) - переключается через кнопку
        # "🛡 Переключить защиту" в handlers/limits.py.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories(
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                category TEXT NOT NULL,
                is_protected BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE(phone, category)
            )
            """
        )
        # НОВОЕ: миграция для БД, созданных ДО этого изменения (у них таблица
        # categories уже существует без колонки is_protected, и CREATE TABLE
        # IF NOT EXISTS выше в этом случае ничего не сделает). ALTER TABLE
        # ADD COLUMN бросает ошибку, если колонка уже есть - поэтому проверяем
        # через information_schema.columns (аналог PRAGMA table_info из
        # SQLite) и добавляем только при необходимости.
        await _add_column_if_missing(conn, "categories", "is_protected", "BOOLEAN NOT NULL DEFAULT FALSE")

        # ИЗМЕНЕНО: добавлена колонка is_manual - отличает лимит, который
        # пользователь выставил САМ (через "Лимиты и категории"), от лимита,
        # который посчитал автоматический пересчёт по общему бюджету (см.
        # services/budget_forecast.py). Это нужно, чтобы автопересчёт не
        # затирал молча ручную правку пользователя за тот же месяц - см.
        # save_recalculated_limits ниже. Значение по умолчанию - TRUE
        # (считаем существующие/добавляемые вручную лимиты ручными, если явно
        # не сказано иное - см. save_recalculated_limits, которая пишет FALSE).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS limits(
                phone TEXT NOT NULL,
                month DATE NOT NULL,
                category_id INTEGER,
                spending_limit INTEGER,
                is_manual BOOLEAN NOT NULL DEFAULT TRUE,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                UNIQUE(phone, month, category_id)
            )
            """
        )
        await _add_column_if_missing(conn, "limits", "is_manual", "BOOLEAN NOT NULL DEFAULT TRUE")

        # НОВОЕ: план ЗП по месяцам (на 12 месяцев вперёд, с возможностью
        # правки задним числом и на будущее - см. set_salary_plan). Один
        # телефон = одна ЗП на семью (как и с categories/limits, ключ -
        # именно phone, а не user id, т.к. номером могут пользоваться
        # несколько telegram-аккаунтов, см. users.is_phone_owner).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS salary_plan(
                phone TEXT NOT NULL,
                month DATE NOT NULL,
                planned_salary INTEGER NOT NULL,
                UNIQUE(phone, month)
            )
            """
        )

        # НОВОЕ: план трат по каждой категории на каждый месяц - основа для
        # пересчёта лимитов по общему бюджету (services/budget_forecast.py).
        # Фактические траты сюда НЕ пишем и никогда не будем - они всегда
        # скачиваются заново из Т-Банка при пересчёте (см. комментарий в
        # services/budget_forecast.py), чтобы учитывать пометки "не
        # учитывать" в банке без риска рассинхронизации.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS category_plan(
                phone TEXT NOT NULL,
                month DATE NOT NULL,
                category_id INTEGER NOT NULL,
                planned_amount INTEGER NOT NULL,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                UNIQUE(phone, month, category_id)
            )
            """
        )

        # НОВОЕ (Mini App "План на год"): признак "не пересчитывать лимит
        # этой категории в этом месяце" - точечная альтернатива
        # categories.is_protected (та защищает категорию во ВСЕХ месяцах
        # сразу). Выставляется прямо в ячейке таблицы плана - см.
        # set_category_plan_cell/get_no_recalc_categories_for_month ниже и
        # services/budget_forecast.recalc_month_limits.
        await _add_column_if_missing(conn, "category_plan", "no_recalc", "BOOLEAN NOT NULL DEFAULT FALSE")

        # НОВОЕ: настройки бюджета пользователя - пока только начальные
        # накопления (точка отсчёта для строки "План накоплений" в таблице
        # плана, см. services/webapp_api.py). Один phone - одна запись, как
        # и с остальными таблицами плана.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_settings(
                phone TEXT PRIMARY KEY,
                initial_savings INTEGER NOT NULL DEFAULT 0
            )
            """
        )


# ИЗМЕНЕНО: хелпер под Postgres - проверяет наличие колонки через
# information_schema.columns вместо PRAGMA table_info (SQLite-специфичной
# команды), логика та же самая: добавляем колонку, только если её ещё нет.
async def _add_column_if_missing(conn, table: str, column: str, column_def: str):
    row = await conn.fetchrow(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = $1 AND column_name = $2
        """,
        table, column,
    )
    if row is None:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
        logger.info(f"Миграция БД: в таблицу {table} добавлена колонка {column}.")


async def add_pre_user(user_id, name):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pre_users(id, name)
            VALUES ($1, $2)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name
            """,
            user_id, name,
        )


async def get_pre_user_name(user_id):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM pre_users WHERE id = $1", user_id
        )
        return row["name"] if row else None


async def add_user(user_id, phone, name, is_phone_owner):
    user_id = _as_int(user_id)
    is_phone_owner = _as_bool(is_phone_owner)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users(id, phone, name, is_phone_owner)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO NOTHING
            """,
            user_id, phone, name, is_phone_owner,
        )


async def delete_user(user_id):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def get_user_info(user_id):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT phone, name, is_phone_owner
            FROM users
            WHERE id = $1
            """,
            user_id,
        )
        if row is None:
            return None
        return tuple(row)


async def get_phone_owner_info(phone):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name
            FROM users
            WHERE phone = $1 AND is_phone_owner = TRUE
            """,
            phone,
        )
        if row is None:
            return None
        return tuple(row)


async def get_user_session_json(user_id):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_json FROM users WHERE id = $1", user_id
        )
        return row[0] if row else None


async def set_user_name(user_id, user_name):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET name = $1 WHERE id = $2", user_name, user_id
        )


async def set_user_phone(user_id, user_phone):
    user_id = _as_int(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET phone = $1, is_phone_owner = FALSE WHERE id = $2",
            user_phone, user_id,
        )


async def set_user_phone_owner(user_id, is_phone_owner):
    user_id = _as_int(user_id)
    is_phone_owner = _as_bool(is_phone_owner)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_phone_owner = $1 WHERE id = $2",
            is_phone_owner, user_id,
        )


async def set_user_session_json(user_id, session_json):
    if isinstance(session_json, dict):
        session_json = json.dumps(session_json)

    user_id = _as_int(user_id)

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # ИЗМЕНЕНО: у asyncpg нет cursor.rowcount как у aiosqlite -
            # conn.execute() возвращает строку статуса вида "UPDATE 1",
            # число обновлённых строк вытаскиваем из неё.
            status = await conn.execute(
                "UPDATE users SET session_json = $1 WHERE id = $2",
                session_json, user_id,
            )
            rowcount = int(status.split()[-1])
            if rowcount == 0:
                logger.warning(
                    f"set_user_session_json: пользователь с id {user_id} не найден в БД."
                )
                return False
            return True

    except asyncpg.PostgresError as e:
        logger.exception(f"Ошибка базы данных Postgres в set_user_session_json (user_id={user_id}): {e}")
        return False


async def add_limit(phone, month, category_id, limit):
    # ИЗМЕНЕНО: явно проставляем is_manual = TRUE - этот путь вызывается
    # только из ручного ввода лимита пользователем (handlers/limits.py),
    # поэтому такой лимит всегда считается "ручным" и не будет молча
    # перезаписан автопересчётом (см. save_recalculated_limits ниже).
    # INSERT ... ON CONFLICT DO UPDATE заодно и "переключает" лимит обратно
    # в ручной режим, если раньше он был выставлен автопересчётом, а теперь
    # пользователь его сам поправил - это ожидаемое поведение (аналог
    # INSERT OR REPLACE из SQLite-версии).
    month = _as_date(month)
    category_id = _as_int(category_id)
    limit = _as_int(limit)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO limits(phone, month, category_id, spending_limit, is_manual)
            VALUES ($1, $2, $3, $4, TRUE)
            ON CONFLICT (phone, month, category_id) DO UPDATE
                SET spending_limit = excluded.spending_limit,
                    is_manual = excluded.is_manual
            """,
            phone, month, category_id, limit,
        )


# НОВОЕ: используется автопересчётом лимитов (services/budget_forecast.py).
# В отличие от add_limit, пишет is_manual = FALSE и НЕ трогает строки,
# которые пользователь уже поправил вручную за этот месяц (WHERE
# limits.is_manual = FALSE в ON CONFLICT) - так ручная правка текущего
# месяца переживает повторный автопересчёт (например, если отчёт за месяц
# строится несколько раз).
async def save_recalculated_limits(phone, month, limits_by_category_id: dict):
    """
    limits_by_category_id: {category_id: рекомендованный_лимит}
    """
    month = _as_date(month)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ИЗМЕНЕНО: оборачиваем цикл в транзакцию, чтобы пересчёт применился
        # атомарно - либо все лимиты за месяц обновились, либо ни один
        # (при ошибке на середине).
        async with conn.transaction():
            for category_id, limit in limits_by_category_id.items():
                category_id = _as_int(category_id)
                limit = _as_int(limit)
                await conn.execute(
                    """
                    INSERT INTO limits(phone, month, category_id, spending_limit, is_manual)
                    VALUES ($1, $2, $3, $4, FALSE)
                    ON CONFLICT(phone, month, category_id) DO UPDATE
                        SET spending_limit = excluded.spending_limit
                        WHERE limits.is_manual = FALSE
                    """,
                    phone, month, category_id, limit,
                )


async def get_limits(phone, month=None):
    if month is None:
        month = date.today().replace(day=1)
    else:
        month = _as_date(month)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                categories.id,
                categories.category,
                limits.spending_limit
            FROM limits
            JOIN categories
                ON limits.category_id = categories.id
            WHERE categories.phone = $1 AND limits.phone = $2 AND limits.month = $3
            """,
            phone, phone, month,
        )
        return [tuple(row) for row in rows]


async def get_limit(category_id, month=None):
    category_id = _as_int(category_id)
    if month is None:
        month = date.today().replace(day=1)
    else:
        month = _as_date(month)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT spending_limit
            FROM limits
            WHERE category_id = $1 AND month = $2
            """,
            category_id, month,
        )
        return row[0] if row else None


async def copy_limits(phone, source_month, target_month):
    """
    Копирует лимиты пользователя с одного месяца на другой.
    Если в целевом месяце категория уже существует, её лимит обновляется.
    """
    if phone is None or source_month is None or target_month is None:
        return

    source_month = _as_date(source_month)
    target_month = _as_date(target_month)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO limits (phone, month, category_id, spending_limit)
            SELECT phone, $1, category_id, spending_limit
            FROM limits
            WHERE phone = $2 AND month = $3
            ON CONFLICT(phone, month, category_id) DO NOTHING
            """,
            target_month, phone, source_month,
        )


async def add_category(phone, category, is_protected: bool = False):
    # ИЗМЕНЕНО: вместо cursor.lastrowid (специфика SQLite, плюс ненадёжно
    # работает вместе с ON CONFLICT DO NOTHING) используем RETURNING id -
    # штатный способ Postgres получить id вставленной строки. Если вставки
    # не произошло из-за конфликта (категория уже существует), RETURNING
    # ничего не вернёт, и мы отдельным запросом достаём id существующей
    # строки - логика полностью сохранена от исходной версии.
    is_protected = _as_bool(is_protected)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO categories(phone, category, is_protected)
            VALUES ($1, $2, $3)
            ON CONFLICT(phone, category) DO NOTHING
            RETURNING id
            """,
            phone, category, is_protected,
        )

        if row:
            return row["id"]

        row = await conn.fetchrow(
            "SELECT id FROM categories WHERE phone = $1 AND category = $2",
            phone, category,
        )
        return row["id"]


async def get_categories(phone):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, category FROM categories WHERE phone = $1", phone
        )
        return [tuple(row) for row in rows]


async def get_category(category_id):
    category_id = _as_int(category_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT category FROM categories WHERE id = $1", category_id
        )
        return row[0] if row else None


async def set_category_name(category_id, category_name):
    category_id = _as_int(category_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE categories SET category = $1 WHERE id = $2",
            category_name, category_id,
        )


async def delete_category(category_id):
    category_id = _as_int(category_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM categories WHERE id = $1", category_id)


# ---------------------------------------------------------------------
# НОВОЕ: признак "защищённая категория" (см. миграцию is_protected выше)
# ---------------------------------------------------------------------

async def get_category_protected(category_id) -> bool:
    category_id = _as_int(category_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_protected FROM categories WHERE id = $1", category_id
        )
        return bool(row[0]) if row else False


async def set_category_protected(category_id, is_protected: bool):
    category_id = _as_int(category_id)
    is_protected = _as_bool(is_protected)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE categories SET is_protected = $1 WHERE id = $2",
            is_protected, category_id,
        )


async def get_categories_full(phone):
    """
    Как get_categories(), но также возвращает is_protected. Отдельная
    функция вместо изменения get_categories() - чтобы не сломать
    существующий код (keyboards.py, handlers/limits.py, handlers/reports.py),
    который распаковывает результат get_categories() как (id, category).
    Используется в services/budget_forecast.py и services/webapp_api.py.

    ИЗМЕНЕНО: добавлен ORDER BY id - без него Postgres не гарантирует
    порядок строк между запросами, из-за чего категории в таблице плана
    Mini App могли визуально "перемешиваться" при каждом открытии. Порядок
    по id = порядок создания категорий - предсказуемый и стабильный.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, category, is_protected FROM categories WHERE phone = $1 ORDER BY id",
            phone,
        )
        return [
            {"id": row["id"], "name": row["category"], "is_protected": bool(row["is_protected"])}
            for row in rows
        ]


# ---------------------------------------------------------------------
# НОВОЕ: план ЗП по месяцам
# ---------------------------------------------------------------------

async def set_salary_plan(phone, month, amount: int):
    """Задать/поправить план ЗП на месяц - можно вызывать и задним числом, и на будущее."""
    month = _as_date(month)
    amount = _as_int(amount)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO salary_plan(phone, month, planned_salary)
            VALUES ($1, $2, $3)
            ON CONFLICT(phone, month) DO UPDATE SET planned_salary = excluded.planned_salary
            """,
            phone, month, amount,
        )


async def get_salary_plan(phone, months: list) -> list:
    """Вернуть план ЗП для списка месяцев (date), в том же порядке. 0, если месяц не задан."""
    months = [_as_date(m) for m in months]
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT month, planned_salary FROM salary_plan WHERE phone = $1", phone
        )

    # ИЗМЕНЕНО: asyncpg сам возвращает колонку DATE как datetime.date -
    # в отличие от aiosqlite, тут не нужно вручную приводить строку/дату
    # к единому формату через isoformat().
    by_month = {row["month"]: row["planned_salary"] for row in rows}
    return [by_month.get(m, 0) for m in months]


# ---------------------------------------------------------------------
# НОВОЕ: план трат по категориям на месяц
# ---------------------------------------------------------------------

async def set_category_plan(phone, category_id, month, amount: int):
    category_id = _as_int(category_id)
    month = _as_date(month)
    amount = _as_int(amount)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO category_plan(phone, month, category_id, planned_amount)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(phone, month, category_id)
            DO UPDATE SET planned_amount = excluded.planned_amount
            """,
            phone, month, category_id, amount,
        )


async def get_category_plan_rows(phone) -> list:
    """Сырые строки (category_id, month, planned_amount) - используется get_plan_horizon
    и services/budget_forecast.py для сборки CategoryPlan."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT category_id, month, planned_amount FROM category_plan WHERE phone = $1",
            phone,
        )
        return [tuple(row) for row in rows]


async def set_category_plan_cell(phone, category_id, month, amount: int, no_recalc: bool):
    """
    Как set_category_plan(), но также явно выставляет no_recalc - флаг
    "не пересчитывать лимит по этой категории в этом месяце" (см.
    services/budget_forecast.recalc_month_limits). Используется таблицей
    плана в Mini App (services/webapp_api.py), где оба значения
    редактируются в одной ячейке одновременно.
    """
    category_id = _as_int(category_id)
    month = _as_date(month)
    amount = _as_int(amount)
    no_recalc = _as_bool(no_recalc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO category_plan(phone, month, category_id, planned_amount, no_recalc)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(phone, month, category_id)
            DO UPDATE SET planned_amount = excluded.planned_amount,
                          no_recalc = excluded.no_recalc
            """,
            phone, month, category_id, amount, no_recalc,
        )


async def get_category_plan_full(phone) -> list:
    """
    Полные строки плана трат, включая no_recalc - используется таблицей
    плана в Mini App (GET /api/plan, services/webapp_api.py). В отличие от
    get_category_plan_rows(), которая используется пересчётом лимитов и
    возвращает только (category_id, month, planned_amount).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT category_id, month, planned_amount, no_recalc
            FROM category_plan WHERE phone = $1
            """,
            phone,
        )
        return [tuple(row) for row in rows]


async def get_no_recalc_categories_for_month(phone, month) -> set:
    """
    Названия категорий, для которых на конкретный месяц выставлен флаг
    "не пересчитывать" (см. set_category_plan_cell). Используется
    services/budget_forecast.recalc_month_limits, чтобы не трогать лимит
    этих категорий при автопересчёте - он останется равен плану.
    """
    month = _as_date(month)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT categories.category
            FROM category_plan
            JOIN categories ON categories.id = category_plan.category_id
            WHERE category_plan.phone = $1 AND category_plan.month = $2
                  AND category_plan.no_recalc = TRUE
            """,
            phone, month,
        )
        return {row["category"] for row in rows}


async def get_category_owner_phone(category_id):
    """
    Возвращает phone, которому принадлежит категория, либо None - для
    проверки прав доступа перед изменением/удалением категории через API
    Mini App (services/webapp_api.py), чтобы один владелец не мог задеть
    категории другого номера.
    """
    category_id = _as_int(category_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT phone FROM categories WHERE id = $1", category_id
        )
        return row[0] if row else None


# ---------------------------------------------------------------------
# НОВОЕ: настройки бюджета - начальные накопления (см. budget_settings)
# ---------------------------------------------------------------------

async def get_initial_savings(phone) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT initial_savings FROM budget_settings WHERE phone = $1", phone
        )
        return row[0] if row else 0


async def set_initial_savings(phone, amount: int):
    amount = _as_int(amount)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_settings(phone, initial_savings)
            VALUES ($1, $2)
            ON CONFLICT(phone) DO UPDATE SET initial_savings = excluded.initial_savings
            """,
            phone, amount,
        )


# ---------------------------------------------------------------------
# НОВОЕ: план на конкретный месяц существует? (см. handlers/reports.py -
# источник истины для построения отчёта теперь план, а не таблица limits,
# которая - лишь производная от плана, см. services/budget_forecast.py)
# ---------------------------------------------------------------------

async def has_plan_for_month(phone, month) -> bool:
    month = _as_date(month)
    pool = await get_pool()
    async with pool.acquire() as conn:
        salary_row = await conn.fetchrow(
            "SELECT 1 FROM salary_plan WHERE phone = $1 AND month = $2", phone, month
        )
        if salary_row is not None:
            return True
        category_row = await conn.fetchrow(
            "SELECT 1 FROM category_plan WHERE phone = $1 AND month = $2", phone, month
        )
        return category_row is not None


async def get_plan_horizon(phone) -> list:
    """
    Возвращает отсортированный список месяцев (date), для которых заведён
    хоть какой-то план (ЗП или траты по категориям) - это и есть окно
    расчёта для services/budget_forecast.recalc_month_limits: часть месяцев
    в нём уже прошла (для них скачивается факт), часть - в будущем (на них
    размазывается отклонение).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ИЗМЕНЕНО: в Postgres у производного (derived) FROM-подзапроса
        # обязателен алиас (тут - AS combined), в SQLite он необязателен.
        rows = await conn.fetch(
            """
            SELECT DISTINCT month FROM (
                SELECT month FROM salary_plan WHERE phone = $1
                UNION
                SELECT month FROM category_plan WHERE phone = $1
            ) AS combined
            ORDER BY month ASC
            """,
            phone,
        )

    # asyncpg отдаёt month уже как datetime.date - доп. конвертация обычно
    # не нужна, но оставлена на случай нестандартного значения.
    result = []
    for row in rows:
        month_value = row["month"]
        if isinstance(month_value, date):
            result.append(month_value)
        else:
            result.append(date.fromisoformat(str(month_value)))
    return result
