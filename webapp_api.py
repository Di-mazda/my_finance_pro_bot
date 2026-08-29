"""
services/webapp_api.py
=======================

НОВЫЙ МОДУЛЬ. HTTP API + статика для Telegram Mini App "План на год"
(webapp/plan.html). Раньше план (ЗП + траты по категориям на 12 месяцев)
заполнялся пошагово через чат (handlers/planning.py) - теперь по кнопке
"📅 План на год" сразу открывается веб-таблица (см. keyboards.py,
KeyboardButton(web_app=...)), а этот модуль отдаёт ей данные и сохраняет
изменения.

Работает на aiohttp (уже используется как транспорт в aiogram, поэтому
не тянет новых зависимостей) и запускается ВНУТРИ того же процесса и
event loop, что и сам бот - см. run_webapp_server() и main.py.

Авторизация Mini App устроена так: фронтенд (webapp/plan.js) на каждый
запрос кладёт в заголовок X-Telegram-Init-Data сырую строку
Telegram.WebApp.initData. Мы проверяем её подлинность через HMAC по
BOT_TOKEN (см. validate_init_data - официальный алгоритм Telegram) и по
initData.user.id находим phone пользователя через database.get_user_info.
Редактировать план может только владелец номера (is_phone_owner) - то же
правило, что действовало раньше в handlers/limits.py и planning.py.

Важное решение по валидации (см. п.6 требований): "План накоплений"
(нарастающий итог: initial_savings + сумма(ЗП - траты) по месяцам) не
должен уходить в минус ни в одном месяце окна. Эта проверка выполняется
на СЕРВЕРЕ при каждом сохранении ячейки (а не только в JS) - именно
сервер решает, сохранять изменение или отклонить его (HTTP 409), клиент
же (webapp/plan.js) отвечает только за то, чтобы показать это как ошибку
и подсветить месяцы красным без лишнего похода в сеть.
"""

import hashlib
import hmac
import json
import time
from datetime import date
from os import getenv
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import database
from logger_config import logger

BOT_TOKEN = getenv("BOT_TOKEN", "")

# Пользователь видит и заполняет план на PLAN_HORIZON_MONTHS месяцев вперёд,
# начиная с текущего месяца - то же окно, что раньше строилось в
# handlers/planning.py:_next_n_months. Окно "плавающее": при каждом
# открытии Mini App отсчитывается заново от сегодняшнего дня, поэтому
# план воспринимается как непрерывный "год вперёд", а не фиксированный
# календарный год.
PLAN_HORIZON_MONTHS = 12

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

# На сколько секунд доверяем initData с момента её выпуска Telegram-ом.
# Mini App выпускает новую initData при каждом открытии, поэтому большой
# запас (сутки) ничем не мешает, но подстраховывает от использования
# протухшей/слитой строки.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------
# Проверка подлинности Telegram.WebApp.initData
# ---------------------------------------------------------------------

def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS):
    """
    Официальный алгоритм проверки initData Telegram Mini Apps:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Возвращает (data, reason):
    - data = {"user": {...}, "auth_date": "...", ...}, reason = None - если всё ок;
    - data = None, reason = короткая строка-причина - если проверка не прошла.
    reason предназначен ТОЛЬКО для логов на сервере (см. auth_middleware) -
    наружу в HTTP-ответ он не идёт, чтобы не подсказывать посторонним, на
    каком шаге проверки подписи можно было бы попробовать обмануть сервер.
    """
    if not bot_token:
        return None, "BOT_TOKEN не задан в окружении процесса, где запущен services/webapp_api.py"
    if not init_data:
        return None, "заголовок X-Telegram-Init-Data пуст (запрос сделан не из Telegram Mini App)"

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None, "initData не парсится как query-строка"

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None, "в initData отсутствует поле hash"

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None, "подпись hash не совпала - BOT_TOKEN у бота и у webapp_api.py, похоже, разные"

    auth_date = pairs.get("auth_date")
    if auth_date is not None and max_age_seconds is not None:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return None, "initData устарела (auth_date старше допустимого срока)"
        except ValueError:
            return None, "auth_date не число"

    user_raw = pairs.get("user")
    try:
        user = json.loads(user_raw) if user_raw else None
    except json.JSONDecodeError:
        user = None

    if not user:
        return None, "в initData нет поля user"

    return {"user": user, "auth_date": auth_date, "raw": pairs}, None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Требует валидную initData и владельца номера для всех /api/* маршрутов."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    validated, reason = validate_init_data(init_data, BOT_TOKEN)

    if not validated:
        # НОВОЕ: логируем ПРИЧИНУ отказа на сервере (видно в логах Railway),
        # и отдаём человекочитаемое message в ответе - раньше здесь была
        # только {"error": "unauthorized"} без message, из-за чего Mini App
        # показывал общую надпись "Не удалось загрузить план" без всякой
        # зацепки, что пошло не так.
        logger.warning(f"Mini App: отказано в доступе к {request.path} - {reason}")
        return web.json_response(
            {"error": "unauthorized", "message": f"Не авторизован: {reason}."},
            status=401,
        )

    tg_user_id = validated["user"].get("id")
    user_info = await database.get_user_info(tg_user_id)
    if not user_info:
        logger.warning(f"Mini App: tg_user_id={tg_user_id} не найден в базе (не проходил /start)")
        return web.json_response(
            {"error": "user_not_found", "message": "Пользователь не найден. Откройте бота, нажмите /start и попробуйте снова."},
            status=404,
        )

    phone, _name, is_phone_owner = user_info
    if not is_phone_owner:
        logger.info(f"Mini App: tg_user_id={tg_user_id} (phone={phone}) не владелец номера, доступ к плану запрещён")
        return web.json_response(
            {"error": "forbidden", "message": "Редактировать план может только владелец номера."},
            status=403,
        )

    request["phone"] = phone
    request["tg_user_id"] = tg_user_id
    return await handler(request)


# ---------------------------------------------------------------------
# Вспомогательное: окно месяцев, проекция накоплений
# ---------------------------------------------------------------------

def _next_n_months(start: date, n: int) -> list:
    months = []
    year, month = start.year, start.month
    for _ in range(n):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _current_horizon() -> list:
    return _next_n_months(date.today().replace(day=1), PLAN_HORIZON_MONTHS)


async def _project_cumulative_savings(
    phone: str,
    months: list,
    salary_override: dict | None = None,
    category_override: dict | None = None,
    initial_savings_override: int | None = None,
) -> dict:
    """
    Считает "План накоплений" (нарастающий итог) по месяцам окна с учётом
    одной гипотетической правки (salary_override и/или category_override),
    ещё не сохранённой в БД - чтобы понять, ДО записи в базу, не уйдёт ли
    в минус какой-то месяц. Возвращает {month_iso: накопления_на_конец_месяца}.
    """
    month_keys = [m.isoformat() for m in months]

    salary_values = await database.get_salary_plan(phone, months)
    salary = dict(zip(month_keys, salary_values))
    if salary_override:
        salary.update(salary_override)

    plan_rows = await database.get_category_plan_full(phone)
    totals = {mk: 0 for mk in month_keys}
    overridden_keys = set()

    for category_id, month_value, amount, _no_recalc in plan_rows:
        mk = month_value.isoformat() if hasattr(month_value, "isoformat") else str(month_value)
        if mk not in totals:
            continue
        key = (category_id, mk)
        if category_override and key in category_override:
            amount = category_override[key]
            overridden_keys.add(key)
        totals[mk] += amount

    if category_override:
        for key, amount in category_override.items():
            if key not in overridden_keys and key[1] in totals:
                totals[key[1]] += amount

    initial_savings = (
        initial_savings_override
        if initial_savings_override is not None
        else await database.get_initial_savings(phone)
    )

    cumulative = {}
    running = initial_savings
    for mk in month_keys:
        running += salary.get(mk, 0) - totals.get(mk, 0)
        cumulative[mk] = running

    return cumulative


def _negative_months(cumulative: dict) -> list:
    return [mk for mk, value in cumulative.items() if value < 0]


def _rejected_response(cumulative: dict) -> web.Response:
    negative = _negative_months(cumulative)
    return web.json_response(
        {
            "error": "savings_would_go_negative",
            "message": "При таком значении план накоплений уходит в минус.",
            "months": negative,
            "cumulative": cumulative,
        },
        status=409,
    )


# ---------------------------------------------------------------------
# GET /api/plan - вся таблица разом
# ---------------------------------------------------------------------

async def get_plan(request: web.Request) -> web.Response:
    phone = request["phone"]
    months = _current_horizon()
    month_keys = [m.isoformat() for m in months]

    categories = await database.get_categories_full(phone)  # [{id, name, is_protected}]

    salary_values = await database.get_salary_plan(phone, months)
    salary = dict(zip(month_keys, salary_values))

    plan = {
        str(cat["id"]): {mk: {"amount": 0, "no_recalc": False} for mk in month_keys}
        for cat in categories
    }
    for category_id, month_value, amount, no_recalc in await database.get_category_plan_full(phone):
        mk = month_value.isoformat() if hasattr(month_value, "isoformat") else str(month_value)
        cat_key = str(category_id)
        if cat_key in plan and mk in plan[cat_key]:
            plan[cat_key][mk] = {"amount": amount, "no_recalc": bool(no_recalc)}

    initial_savings = await database.get_initial_savings(phone)
    cumulative = await _project_cumulative_savings(phone, months)

    return web.json_response({
        "months": month_keys,
        "categories": categories,
        "salary": salary,
        "plan": plan,
        "initial_savings": initial_savings,
        "cumulative": cumulative,
    })


# ---------------------------------------------------------------------
# Категории - создание/переименование/удаление/защита прямо из таблицы
# ---------------------------------------------------------------------

async def create_category(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad_request"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "empty_name"}, status=400)

    category_id = await database.add_category(phone, name)
    return web.json_response({"id": category_id, "name": name, "is_protected": False})


async def update_category(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        category_id = int(request.match_info["category_id"])
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    owner_phone = await database.get_category_owner_phone(category_id)
    if owner_phone != phone:
        return web.json_response({"error": "not_found"}, status=404)

    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            return web.json_response({"error": "empty_name"}, status=400)
        await database.set_category_name(category_id, name)

    if "is_protected" in body:
        await database.set_category_protected(category_id, bool(body["is_protected"]))

    return web.json_response({"ok": True})


async def delete_category(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        category_id = int(request.match_info["category_id"])
    except ValueError:
        return web.json_response({"error": "bad_request"}, status=400)

    owner_phone = await database.get_category_owner_phone(category_id)
    if owner_phone != phone:
        return web.json_response({"error": "not_found"}, status=404)

    await database.delete_category(category_id)
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------
# Ячейки таблицы - зарплата и план трат по категории/месяцу
# ---------------------------------------------------------------------

async def save_salary_cell(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        body = await request.json()
        month = date.fromisoformat(body["month"])
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    if amount < 0:
        return web.json_response({"error": "negative_amount"}, status=400)

    months = _current_horizon()
    if month not in months:
        return web.json_response({"error": "month_out_of_range"}, status=400)

    cumulative = await _project_cumulative_savings(
        phone, months, salary_override={month.isoformat(): amount},
    )
    if _negative_months(cumulative):
        return _rejected_response(cumulative)

    await database.set_salary_plan(phone, month, amount)
    return web.json_response({"ok": True, "cumulative": cumulative})


async def save_category_plan_cell(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        body = await request.json()
        category_id = int(body["category_id"])
        month = date.fromisoformat(body["month"])
        amount = int(body["amount"])
        no_recalc = bool(body.get("no_recalc", False))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    if amount < 0:
        return web.json_response({"error": "negative_amount"}, status=400)

    owner_phone = await database.get_category_owner_phone(category_id)
    if owner_phone != phone:
        return web.json_response({"error": "not_found"}, status=404)

    months = _current_horizon()
    if month not in months:
        return web.json_response({"error": "month_out_of_range"}, status=400)

    cumulative = await _project_cumulative_savings(
        phone, months, category_override={(category_id, month.isoformat()): amount},
    )
    if _negative_months(cumulative):
        return _rejected_response(cumulative)

    await database.set_category_plan_cell(phone, category_id, month, amount, no_recalc)
    return web.json_response({"ok": True, "cumulative": cumulative})


async def save_initial_savings(request: web.Request) -> web.Response:
    phone = request["phone"]
    try:
        body = await request.json()
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    months = _current_horizon()
    cumulative = await _project_cumulative_savings(
        phone, months, initial_savings_override=amount,
    )
    if _negative_months(cumulative):
        return _rejected_response(cumulative)

    await database.set_initial_savings(phone, amount)
    return web.json_response({"ok": True, "cumulative": cumulative})


# ---------------------------------------------------------------------
# aiohttp app factory + запуск сервера внутри процесса бота
# ---------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])

    app.router.add_get("/api/plan", get_plan)
    app.router.add_post("/api/categories", create_category)
    app.router.add_patch("/api/categories/{category_id}", update_category)
    app.router.add_delete("/api/categories/{category_id}", delete_category)
    app.router.add_put("/api/salary", save_salary_cell)
    app.router.add_put("/api/category-plan", save_category_plan_cell)
    app.router.add_put("/api/initial-savings", save_initial_savings)

    # Статика самого Mini App (webapp/plan.html, plan.css, plan.js) - Telegram
    # открывает WebAppInfo(url=...), указывающий сюда же, в тот же процесс.
    app.router.add_static("/webapp/", path=str(WEBAPP_DIR), show_index=False)

    return app


async def run_webapp_server() -> web.AppRunner:
    """
    Поднимает aiohttp-сервер в текущем event loop, НЕ блокируя его (в
    отличие от dp.start_polling) - вызывается один раз из main.py перед
    запуском polling бота. Возвращает AppRunner, который нужно
    остановить через runner.cleanup() при остановке бота.
    """
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"Web-сервер Mini App запущен на 0.0.0.0:{port} (статика: {WEBAPP_DIR}).")
    return runner
