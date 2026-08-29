from os import getenv

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from database import get_user_info


def _plan_webapp_button():
    """
    НОВОЕ: кнопка "📅 План на год" теперь открывает Telegram Mini App
    (webapp/plan.html) вместо чатового сценария handlers/planning.py -
    там же теперь заводятся и категории, и лимиты (план по месяцам), так
    что отдельная кнопка "Лимиты и категории" больше не нужна и убрана из
    клавиатуры ниже.

    WEBAPP_URL - публичный https-адрес, на котором крутится сервер бота
    (services/webapp_api.py отдаёт статику Mini App по пути /webapp/...).
    Если переменная не задана (например, при локальном запуске без
    домена), оставляем обычную текстовую кнопку - нажатие на неё попадёт
    в handlers/fallback.py с понятной подсказкой, бот не упадёт.
    """
    webapp_url = getenv("WEBAPP_URL", "").strip().rstrip("/")
    if not webapp_url:
        return KeyboardButton(text="📅 План на год")
    return KeyboardButton(
        text="📅 План на год",
        web_app=WebAppInfo(url=f"{webapp_url}/webapp/plan.html"),
    )


def get_main_reply_keyboard(is_authorized=True):

    if is_authorized:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Отчёт за текущий месяц"), KeyboardButton(text="Отчёт за прошлый месяц")],
                # ИЗМЕНЕНО: кнопка "Лимиты и категории" убрана - управление
                # категориями и лимитами (планом по месяцам) теперь целиком
                # происходит в Mini App по кнопке "📅 План на год" (см.
                # _plan_webapp_button() выше и webapp/plan.html).
                [KeyboardButton(text="Текущие лимиты"), _plan_webapp_button()],
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