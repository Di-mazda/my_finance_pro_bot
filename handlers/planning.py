"""
handlers/planning.py
=====================

ИЗМЕНЕНО: раньше здесь был пошаговый чатовый сценарий ввода плана ЗП и
плана трат по категориям на 12 месяцев вперёд (кнопка "📅 План на год" ->
"1️⃣ Сначала укажите ЗП" -> "2️⃣ Затем траты по каждой категории" и т.д.).

Теперь кнопка "📅 План на год" в основной клавиатуре (см. keyboards.py) -
обычная текстовая кнопка. По нажатию хендлер open_plan_webapp ниже
присылает ОТДЕЛЬНОЕ сообщение с INLINE-кнопкой, которая уже открывает
Telegram Mini App - таблицу (категории x месяцы) с зарплатой, лимитами,
флагом "не пересчитывать" на ячейку, начальными накоплениями и планом
накоплений нарастающим итогом.

ВАЖНО, почему именно так, в два шага, а не одной web_app-кнопкой сразу на
основной клавиатуре (как было в первой версии): по официальной документации
Telegram (https://core.telegram.org/bots/webapps#keyboard-button-mini-apps),
initData (данные о личности пользователя, которые проверяет
services/webapp_api.py) ВСЕГДА пустая, если Mini App открыт через web_app-
кнопку на обычной Reply-клавиатуре - для такой кнопки Telegram выделяет
только односторонний канал Telegram.WebApp.sendData(), без initData вообще.
initData появляется только при открытии через INLINE-кнопку сообщения или
через Menu Button - оба варианта Telegram прямо называет идентичными по
поведению. Поэтому весь HTTP API с проверкой владельца номера
(services/webapp_api.py) работает только если открывать таблицу именно
через inline-кнопку из get_plan_inline_keyboard().

Вся логика самой таблицы и её хранение - в services/webapp_api.py (HTTP
API) и webapp/plan.html - никакого FSM-сценария в чате для неё не
требуется, поэтому planning_menu/process_salary_bulk/_start_category_plan_flow
и т.д. отсюда убраны целиком.

Оставлены только точечные команды /зп и /план - быстрая правка ОДНОГО
месяца без открытия Mini App, для тех, кто уже привык к ним. Они пишут в
те же таблицы (salary_plan/category_plan), что и Mini App, поэтому правки
видны в таблице сразу при следующем открытии.
"""

from datetime import date

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from keyboards import get_main_reply_keyboard, get_plan_inline_keyboard
from database import get_user_info, get_categories, set_salary_plan, set_category_plan

router = Router()


async def _require_owner(message: Message):
    """
    План (как раньше и лимиты/категории) заводит только владелец номера,
    т.к. от него зависят цифры, общие для всех, у кого есть доступ к этому
    номеру. Возвращает user_phone либо None (в этом случае сообщение об
    ошибке уже отправлено пользователю).
    """
    user = message.from_user
    if user is None:
        await message.answer("⚠️ Не удалось определить пользователя.")
        return None

    user_info = await get_user_info(user.id)
    if not user_info:
        await message.answer("⚠️ Не удалось определить номер телефона пользователя.")
        return None

    user_phone, _name, is_phone_owner = user_info
    if not is_phone_owner:
        await message.answer(
            "<i>Редактировать план на год может только владелец номера.</i>",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        return None

    return user_phone


@router.message(F.text.lower() == "📅 план на год")
async def open_plan_webapp(message: Message):
    """
    См. комментарий в шапке файла: сама кнопка в reply-клавиатуре - обычная
    текстовая, а Mini App открывается через ОТДЕЛЬНУЮ inline-кнопку в новом
    сообщении - иначе Telegram не передаст initData и
    services/webapp_api.py не сможет определить, кто открыл таблицу.
    """
    user_phone = await _require_owner(message)
    if user_phone is None:
        return

    keyboard = get_plan_inline_keyboard()
    if keyboard is None:
        await message.answer(
            "⚠️ Таблица плана пока не настроена на сервере (не задан "
            "WEBAPP_URL). Обратитесь к администратору бота."
        )
        return

    await message.answer(
        "📅 Нажмите кнопку ниже, чтобы открыть таблицу плана на год:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------
# Точечная правка одного месяца без открытия Mini App.
# ---------------------------------------------------------------------

@router.message(Command("зп"))
async def edit_salary_single_month(message: Message, state: FSMContext):
    """Формат: /зп ММ.ГГГГ сумма - например /зп 05.2026 95000"""
    user_phone = await _require_owner(message)
    if user_phone is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    args = parts[1].split() if len(parts) > 1 else []

    if len(args) != 2 or not args[1].isdigit():
        await message.answer(
            "⚠️ Формат команды: <code>/зп ММ.ГГГГ сумма</code>\n"
            "Например: <code>/зп 05.2026 95000</code>",
            parse_mode="HTML",
        )
        return

    try:
        month_str, year_str = args[0].split(".")
        month_date = date(int(year_str), int(month_str), 1)
    except (ValueError, IndexError):
        await message.answer("⚠️ Не удалось разобрать месяц. Формат: ММ.ГГГГ, например 05.2026.")
        return

    amount = int(args[1])
    await set_salary_plan(user_phone, month_date, amount)

    await message.answer(
        f"✅ ЗП на {month_date.strftime('%m.%Y')} установлена: <b>{amount} ₽</b>",
        parse_mode="HTML",
    )


@router.message(Command("план"))
async def edit_category_plan_single_month(message: Message, state: FSMContext):
    """Формат: /план Категория ММ.ГГГГ сумма - например /план Продукты 05.2026 30000"""
    user_phone = await _require_owner(message)
    if user_phone is None:
        return

    parts = (message.text or "").split()
    if len(parts) < 4 or not parts[-1].isdigit():
        await message.answer(
            "⚠️ Формат команды: <code>/план Категория ММ.ГГГГ сумма</code>\n"
            "Например: <code>/план Продукты 05.2026 30000</code>",
            parse_mode="HTML",
        )
        return

    amount = int(parts[-1])
    month_arg = parts[-2]
    category_name = " ".join(parts[1:-2])

    try:
        month_str, year_str = month_arg.split(".")
        month_date = date(int(year_str), int(month_str), 1)
    except (ValueError, IndexError):
        await message.answer("⚠️ Не удалось разобрать месяц. Формат: ММ.ГГГГ, например 05.2026.")
        return

    categories = await get_categories(user_phone)
    match = next((cid for cid, name in categories if name.lower() == category_name.lower()), None)

    if match is None:
        await message.answer(f"⚠️ Категория «{category_name}» не найдена.")
        return

    await set_category_plan(user_phone, match, month_date, amount)

    await message.answer(
        f"✅ План трат по категории <b>{category_name}</b> на "
        f"{month_date.strftime('%m.%Y')}: <b>{amount} ₽</b>",
        parse_mode="HTML",
    )
