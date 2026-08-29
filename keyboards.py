from os import getenv

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from database import get_user_info


def _normalize_webapp_url(raw: str) -> str:
    """
    Telegram принимает в WebAppInfo только https-ссылки - если в .env
    WEBAPP_URL указан без схемы (например, просто "xxx.up.railway.app",
    как отдаёт Railway в поле домена) или по ошибке с "http://", Telegram
    отвечает "Bad Request: ... Only HTTPS links are allowed" прямо при
    попытке отправить клавиатуру. Поэтому всегда приводим к https:// сами,
    вместо того чтобы полагаться на то, что переменная окружения задана
    правильно.
    """
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("http://"):
        raw = raw[len("http://"):]
    elif raw.startswith("https://"):
        raw = raw[len("https://"):]
    return f"https://{raw}"


def get_plan_inline_keyboard():
    """
    ВАЖНО: по документации Telegram initData (данные о личности
    пользователя, которые проверяет services/webapp_api.py) ВСЕГДА пустая,
    если Mini App открыт через web_app-кнопку на ОБЫЧНОЙ (Reply)
    клавиатуре - для такой кнопки Telegram выделяет только односторонний
    канал Telegram.WebApp.sendData(), initData не передаётся вообще (см.
    https://core.telegram.org/bots/webapps#keyboard-button-mini-apps).
    initData появляется, только если Mini App открыт через INLINE-кнопку
    сообщения или через кнопку меню (Menu Button) - Telegram описывает оба
    варианта как идентичные по поведению.

    Поэтому кнопка "📅 План на год" в основной (Reply) клавиатуре ниже -
    обычная текстовая кнопка, а по нажатию handlers/planning.py:open_plan_webapp
    присылает ОТДЕЛЬНОЕ сообщение с этой inline-кнопкой - только через неё
    сервер сможет проверить личность пользователя.
    """
    webapp_url = _normalize_webapp_url(getenv("WEBAPP_URL", ""))
    if not webapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📊 Открыть таблицу плана",
                web_app=WebAppInfo(url=f"{webapp_url}/webapp/plan.html"),
            )]
        ]
    )


def get_main_reply_keyboard(is_authorized=True):

    if is_authorized:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Отчёт за текущий месяц"), KeyboardButton(text="Отчёт за прошлый месяц")],
                # ИЗМЕНЕНО: обычная текстовая кнопка (не web_app) - см.
                # get_plan_inline_keyboard() выше и
                # handlers/planning.py:open_plan_webapp, почему Mini App
                # запускается через отдельную inline-кнопку, а не отсюда.
                [KeyboardButton(text="Текущие лимиты"), KeyboardButton(text="📅 План на год")],
                [KeyboardButton(text="Мой аккаунт")]
            ],
            resize_keyboard=True
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Старт")]
            ],
            resize_keyboard=True
        )
    return keyboard


def get_OK_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="👍 Да, все верно")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    return keyboard


def get_cancel_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_phone_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📲 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    return keyboard


def get_account_main_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить имя", callback_data="set_name")],
            [InlineKeyboardButton(text="Изменить номер", callback_data="set_phone")]
        ]
    )
    return keyboard

def get_permission_inline_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Запросить доступ у пользователя", callback_data="request_permission")],
            [InlineKeyboardButton(text="Изменить номер", callback_data="set_phone")]
        ]
    )
    return keyboard

def get_response_permission_inline_keyboard(user_id):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, выдать право на просмотр моих отчётов этому пользователю", callback_data=f"yes_response_permission:{user_id}")],
            [InlineKeyboardButton(text="Нет, закрыть доступ", callback_data=f"no_response_permission:{user_id}")]
        ]
    )
    return keyboard


# НОВОЕ: клавиатуры для реализации TODO из handlers/reports.py - запрос
# отчёта не-owner пользователем.
def get_report_request_owner_keyboard(requester_id, session_valid: bool):
    """
    Клавиатура, которая уходит ВЛАДЕЛЬЦУ номера, когда не-owner пользователь
    запросил отчёт. Текст кнопки подтверждения зависит от того, жива ли
    ещё сохранённая сессия владельца в Т-Банке:
    - сессия жива -> просим только подтвердить;
    - сессия протухла -> предлагаем сразу авторизоваться заново.
    """
    if session_valid:
        confirm_text = "🔐 Подтвердить"
    else:
        confirm_text = "🔑 Авторизоваться заново"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=confirm_text, callback_data=f"owner_confirm_report:{requester_id}")],
            [InlineKeyboardButton(text="🚫 Отклонить запрос", callback_data=f"owner_decline_report:{requester_id}")]
        ]
    )
    return keyboard


def get_requester_waiting_keyboard():
    """
    Клавиатура для ЗАПРАШИВАЮЩЕГО пользователя, пока он ждёт реакции
    владельца номера. Позволяет не ждать, а попробовать войти самостоятельно
    (на случай, если запрашивающий сам знает пароль/пин от Т-Банка владельца).
    """
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Войти самостоятельно")],
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )
    return keyboard

# НОВОЕ: клавиатура для подтверждения при вводе плана (переиспользует
# get_yes_no_keyboard по смыслу, но с более уместной для планирования
# формулировкой кнопок - используется в handlers/planning.py).
def get_planning_yes_no_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да, всё верно"), KeyboardButton(text="Нет, ввести заново")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    return keyboard