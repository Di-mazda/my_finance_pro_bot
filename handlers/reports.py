from datetime import date, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramForbiddenError

from forms.user import Form
from keyboards import (
    get_cancel_keyboard,
    get_main_reply_keyboard,
    get_report_request_owner_keyboard,
    get_requester_waiting_keyboard,
)
from database import get_limits, get_user_session_json, get_user_info, get_phone_owner_info, delete_user, has_plan_for_month
from services import tbank_client
from services.report_service import build_budget_report_text
# НОВОЕ: автоматический пересчёт лимитов по общему бюджету перед показом
# отчёта (если у пользователя заведён план - см. handlers/planning.py и
# services/budget_forecast.py). Если плана нет, recalc_month_limits вернёт
# None и всё будет работать ровно как раньше.
from services import budget_forecast

from logger_config import logger

router = Router()


# Вычисляет первое число месяца, предшествующего переданной дате.
def _first_day_of_previous_month(today: date) -> date:
    first_day_of_this_month = today.replace(day=1)
    last_day_of_prev_month = first_day_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.replace(day=1)


@router.message(F.text.lower() == "отчёт за текущий месяц")
async def report_current_month(message: Message, state: FSMContext, playwright_instance):
    month = date.today().replace(day=1)
    await _send_month_report(message, state, playwright_instance, month)


@router.message(F.text.lower() == "отчёт за прошлый месяц")
async def report_previous_month(message: Message, state: FSMContext, playwright_instance):
    month = _first_day_of_previous_month(date.today())
    await _send_month_report(message, state, playwright_instance, month)


async def _send_month_report(message: Message, state: FSMContext, playwright_instance, month: date):
    # Проверяем лимиты
    user_id = message.from_user.id # type: ignore
    user_info = await get_user_info(user_id)
    if not user_info:
        error_message = "Не удалось получить данные пользователя."
        await message.answer(
            f"❌ {error_message} Пожалуйста, попробуйте снова.",
            reply_markup=get_main_reply_keyboard()
        )
        logger.error(error_message)
        return
    user_phone, user_name, is_phone_owner = user_info

    # ИЗМЕНЕНО: раньше здесь проверялось наличие уже посчитанных лимитов
    # (limits) и, если их не было, предлагалось скопировать лимиты с
    # соседнего месяца. Теперь лимиты - производная от плана (см.
    # handlers/planning.py и services/budget_forecast.py): download_and_send_report
    # ниже сам пересчитает их из плана прямо перед отчётом (recalc_month_limits).
    # Поэтому единственное, что нужно проверить заранее - заведён ли вообще
    # план (ЗП и/или траты по категориям) на этот месяц.
    if not await has_plan_for_month(user_phone, month):
        month_string = month.strftime("%m.%Y")
        await message.answer(
            f"⚠️ План на {month_string} ещё не заполнен.\n\n"
            "Откройте «📅 План на год» и укажите зарплату и траты по "
            "категориям хотя бы на этот месяц - тогда бот сможет "
            "автоматически посчитать лимиты и построить отчёт.",
            reply_markup=get_main_reply_keyboard()
        )
        return

    limits = await get_limits(user_phone, month)

    if is_phone_owner:
        await _send_report_for_owner(message, state, playwright_instance, month, limits, user_id, user_phone)
    else:
        await _request_report_for_non_owner(
            message, state, playwright_instance, month, limits, user_id, user_phone, user_name
        )


async def _send_report_for_owner(message: Message, state: FSMContext, playwright_instance, month, limits, user_id, user_phone):
    """
    Формирование отчёта для ВЛАДЕЛЬЦА номера 
    """
    saved_session_str = await get_user_session_json(user_id)

    await message.answer("⏳ Авторизуюсь в Т-Банке, пожалуйста ожидайте...")

    browser = await tbank_client.launch_browser(playwright_instance)

    try:
        context, page, reused = None, None, False

        if saved_session_str:
            context, page, reused = await tbank_client.try_restore_session(browser, saved_session_str)

        if reused:
            # ИЗМЕНЕНО: добавлен phone=user_phone, чтобы автопересчёт лимитов
            # (services/budget_forecast.py) мог найти план пользователя.
            # Было: await download_and_send_report(message.bot, message.chat.id, month, limits, context, page)
            await download_and_send_report(message.bot, message.chat.id, month, limits, context, page, phone=user_phone)
            await browser.close()
            await state.clear()
            return

        if context is None:
            context = await browser.new_context()
            page = await context.new_page()

        await tbank_client.start_phone_login(page, user_phone)

        await state.update_data(browser=browser, context=context, page=page, month=month, limits=limits)
        await state.set_state(Form.sms)
        await message.answer(
            f"💬 Т-Банк отправил код для входа на номер {user_phone}. Пожалуйста, введите код сюда в чат."
            "Мы не храним ваши пароли и коды! <b><i>Сообщение с кодом автоматически удалится из этого чата.</i></b>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )

    except Exception as e:
        logger.exception(f"Ошибка при авторизации в Т-Банке (user_id={user_id}, месяц={month}): {e}")
        await browser.close()
        await state.clear()
        await message.answer(f"❌ Ошибка при авторизации. Ошибка: {e}")


async def _request_report_for_non_owner(message: Message, state: FSMContext, playwright_instance, month, limits, requester_id, phone, requester_name):
    """
    Не-owner пользователь никогда не
    проходит собственный вход в Т-Банк - у него нет своего session_json,
    только доступ на чтение категорий/лимитов, которые хранятся по
    номеру телефона владельца. Поэтому отчёт для него можно построить
    только через сессию (или новый вход) владельца этого номера.

    Логика:
    1. Достаём владельца номера и пробуем восстановить ЕГО сохранённую
       сессию (не трогая его пароль/пин - просто проверяем куки).
    2. Самому запрашивающему предлагаем ввести пик-код самостоятельно 
        (вдруг он знает пароль/пин владельца) - см.
       requester_self_login_handler ниже.
    Итоговый отчёт в любом случае уходит ЗАПРАШИВАЮЩЕМУ (requester_id) -
    это отслеживается через report_recipient_id в FSM-состоянии (см.
    handlers/tbank_auth.py:_after_login).

    ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ (не реализовано, оставлено на будущее): если
    запрашивающий отменит ожидание ("Отмена") пока сессия владельца уже
    была открыта на шаге 1 (session_valid=True), открытый browser,
    сохранённый в состоянии ВЛАДЕЛЬЦА, не закроется автоматически, пока
    владелец сам не отреагирует на запрос (подтвердит или отклонит) -
    т.к. общий cancel-хендлер в handlers/start.py проверяет browser только
    в состоянии текущего (запрашивающего) пользователя.
    """
    owner_info = await get_phone_owner_info(phone)

    if owner_info is None:
        # В норме такого быть не должно - доступ к чужому номеру выдаётся
        # только через подтверждённый владельцем permission-flow (см.
        # handlers/account.py), но на всякий случай подстраховываемся от
        # рассинхрона данных в БД.
        logger.warning(
            f"_request_report_for_non_owner: не найден владелец номера {phone} (requester_id={requester_id})."
        )
        await message.answer(
            "⚠️ Не удалось найти владельца этого номера. Похоже, доступ был отозван.\n"
            "Попробуйте изменить номер в «Мой аккаунт». Или попробуйте войти самостоятельно.",
            reply_markup=get_main_reply_keyboard()
        )
        return

    owner_id, owner_name = owner_info

    saved_session_str = await get_user_session_json(owner_id)

    session_valid = False
    browser = context = page = None

    if saved_session_str:
        browser = await tbank_client.launch_browser(playwright_instance)
        context, page, session_valid = await tbank_client.try_restore_session(browser, saved_session_str)
        if not session_valid:
            # Сессия протухла - открытый browser сейчас не нужен: логиниться
            # заново будет либо владелец, либо сам запрашивающий, и в обоих
            # случаях они откроют СВОЙ browser в своём хендлере.
            await browser.close()
            browser = context = page = None

    # Сохраняем данные запроса в состоянии ВЛАДЕЛЬЦА - именно он будет
    # нажимать кнопки подтверждения/отклонения и вводить пин-код.
    owner_state = FSMContext(
        storage=state.storage,
        key=StorageKey(bot_id=message.bot.id, chat_id=owner_id, user_id=owner_id) # type: ignore
    )
    await owner_state.update_data(
        browser=browser,
        context=context,
        page=page,
        month=month,
        limits=limits,
        phone=phone,
        session_valid=session_valid,
        report_recipient_id=requester_id,
    )

    # А в состоянии ЗАПРАШИВАЮЩЕГО - то, что нужно, чтобы он мог сам
    # попробовать войти (см. requester_self_login_handler).
    await state.update_data(owner_id=owner_id, phone=phone, month=month, limits=limits)
    await state.set_state(Form.waiting_owner_action)

    month_string = month.strftime("%m.%Y")

    if session_valid:
        owner_text = (
            f"Пользователь <a href='tg://user?id={requester_id}'>{requester_name}</a> "
            f"хочет посмотреть отчёт за {month_string}.\n\n"
            "Ваша сессия в Т-Банке ещё активна. Подтвердите доступ, или отклоните запрос."
        )
        requester_text = (
            "⏳ Для формирования отчёта нужно подтверждение владельца номера - "
            "отправил ему запрос на подтверждение.\n\n"
            "Если вы знаете пин-код от Т-Банка владельца, можете ввести данные "
            "самостоятельно, не дожидаясь его ответа:"
        )
    else:
        owner_text = (
            f"Пользователь <a href='tg://user?id={requester_id}'>{requester_name}</a> "
            f"хочет посмотреть отчёт за {month_string}, но ваша сессия в Т-Банке истекла.\n\n"
            "Авторизуйтесь заново, чтобы отчёт был отправлен этому пользователю."
        )
        requester_text = (
            "⏳ Сессия владельца номера истекла - отправил ему запрос на повторный вход.\n\n"
            "Если вы знаете данные для входа в Т-Банк владельца (пароль/пин), можете "
            "попробовать войти самостоятельно, не дожидаясь его:"
        )

    try:
        await message.bot.send_message( # type: ignore
            chat_id=owner_id,
            text=owner_text,
            parse_mode="HTML",
            reply_markup=get_report_request_owner_keyboard(requester_id, session_valid=session_valid)
        )
    except TelegramForbiddenError:
        # если владелец заблокировал бота, его
        # запись больше не актуальна, удаляем её.
        logger.info(f"Пользователь {owner_id} заблокировал бота - не удалось отправить запрос на отчёт.")
        await delete_user(owner_id)
        if browser:
            await browser.close()
        await state.clear()
        await message.answer(
            "❌ Не удалось связаться с владельцем номера. "
            "Попробуйте изменить номер в «Мой аккаунт» и войдите самостоятельно.",
            reply_markup=get_main_reply_keyboard()
        )
        return

    await message.answer(requester_text, reply_markup=get_requester_waiting_keyboard())


@router.message(Form.waiting_owner_action, F.text.lower() == "войти самостоятельно")
async def requester_self_login_handler(message: Message, state: FSMContext, playwright_instance):
    """
    Запрашивающий не-owner пользователь решил не ждать владельца и
    попробовать войти в Т-Банк самостоятельно (например, физически имеет
    доступ к телефону владельца и знает пароль/пин от банка).

    После успешного входа полученная сессия сохраняется под ID ВЛАДЕЛЬЦА
    (session_owner_id), а не запрашивающего - см. handlers/tbank_auth.py:
    _after_login. Так свежая сессия станет доступна для будущих отчётов
    любому, у кого есть доступ к этому номеру, а не осядет мёртвым грузом
    в записи запрашивающего пользователя, которая для отчётов вообще не
    используется (см. _send_month_report выше).
    """
    data = await state.get_data()
    phone = data.get("phone")
    month = data.get("month")
    limits = data.get("limits")
    owner_id = data.get("owner_id")
    requester_id = message.from_user.id # type: ignore

    if not phone or not owner_id:
        await message.answer(
            "❌ Ошибка: запрос устарел. Пожалуйста, начните заново.",
            reply_markup=get_main_reply_keyboard()
        )
        await state.clear()
        return

    await message.answer("⏳ Авторизуюсь в Т-Банке, пожалуйста ожидайте...", reply_markup=get_main_reply_keyboard())

    browser = await tbank_client.launch_browser(playwright_instance)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await tbank_client.start_phone_login(page, phone)

        await state.update_data(
            browser=browser, context=context, page=page,
            month=month, limits=limits,
            report_recipient_id=requester_id,
            session_owner_id=owner_id,
        )
        await state.set_state(Form.sms)
        await message.answer(
            f"💬 Т-Банк отправил код для входа на номер {phone}. Пожалуйста, введите код сюда в чат.\n"
            "Мы не храним ваши пароли и коды! <b><i>Сообщение с кодом автоматически удалится из этого чата.</i></b>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception(f"Ошибка при самостоятельном входе не-owner пользователя (requester_id={requester_id}): {e}")
        await browser.close()
        await state.clear()
        await message.answer(f"❌ Ошибка при авторизации. Ошибка: {e}")


@router.callback_query(lambda c: c.data.startswith("owner_confirm_report:"))
async def owner_confirm_report_handler(callback: CallbackQuery, state: FSMContext, playwright_instance):
    """
    Владелец номера нажал кнопку подтверждения/повторного входа в
    ответ на запрос отчёта от не-owner пользователя (см.
    _request_report_for_non_owner выше).
    """
    owner_id = callback.from_user.id
    requester_id = int(callback.data.removeprefix("owner_confirm_report:")) # type: ignore

    data = await state.get_data()
    session_valid = data.get("session_valid", False)
    context = data.get("context")
    page = data.get("page")
    month = data.get("month")
    limits = data.get("limits")
    phone = data.get("phone")
    browser = data.get("browser")

    await callback.answer()

    if session_valid and context is not None and page is not None:
        await callback.message.answer("✅ Доступ подтверждён. Формирую отчёт для пользователя...", reply_markup=get_main_reply_keyboard())

        # ИЗМЕНЕНО: добавлен phone=phone (план и лимиты привязаны к номеру
        # владельца, а не к requester_id) - см. services/budget_forecast.py.
        # Было: await download_and_send_report(callback.bot, requester_id, month, limits, context, page)
        await download_and_send_report(callback.bot, requester_id, month, limits, context, page, phone=phone)

        if browser:
            await browser.close()

        try:
            await message.bot.send_message( # type: ignore
                chat_id=requester_id,
                text="✅ Владелец номера подтвердил доступ, отчёт готов."
            )
        except TelegramForbiddenError:
            logger.info(f"Пользователь {requester_id} заблокировал бота.")

        requester_state = FSMContext(
            storage=state.storage,
            key=StorageKey(bot_id=callback.bot.id, chat_id=requester_id, user_id=requester_id) # type: ignore
        )
        await requester_state.clear()
        await state.clear()
        return

    # Сессии не было / она протухла - запускаем полноценный вход владельца.
    # Итоговый отчёт после входа уйдёт запрашивающему (report_recipient_id),
    # а не самому владельцу - это учитывается в _after_login.
    await callback.message.answer("⏳ Авторизуюсь в Т-Банке, пожалуйста ожидайте...") # type: ignore

    browser = await tbank_client.launch_browser(playwright_instance)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await tbank_client.start_phone_login(page, phone)

        await state.update_data(
            browser=browser, context=context, page=page,
            month=month, limits=limits,
            report_recipient_id=requester_id,
        )
        await state.set_state(Form.sms)
        await callback.message.answer( # type: ignore
            f"💬 Т-Банк отправил код для входа на номер {phone}. Пожалуйста, введите код сюда в чат.\n"
            f"Мы не храним ваши пароли и коды! <b><i>Сообщение с кодом автоматически удалится из этого чата.</i></b>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        try:
            await callback.bot.send_message( # type: ignore
                chat_id=requester_id,
                text="⏳ Владелец номера начал повторный вход. Ожидайте отчёт."
            )
        except TelegramForbiddenError:
            logger.info(f"Пользователь {requester_id} заблокировал бота.")
    except Exception as e:
        logger.exception(f"Ошибка при авторизации владельца по запросу отчёта (owner_id={owner_id}): {e}")
        await browser.close()
        await state.clear()
        await callback.message.answer(f"❌ Ошибка при авторизации. Ошибка: {e}") # type: ignore


@router.callback_query(lambda c: c.data.startswith("owner_decline_report:"))
async def owner_decline_report_handler(callback: CallbackQuery, state: FSMContext):
    """владелец отклонил запрос на отчёт от не-owner пользователя."""
    requester_id = int(callback.data.removeprefix("owner_decline_report:")) # type: ignore

    data = await state.get_data()
    browser = data.get("browser")
    if browser:
        await browser.close()
    await state.clear()

    await callback.message.answer("🚫 Вы отклонили запрос на просмотр отчёта.") # type: ignore

    try:
        await callback.bot.send_message( # type: ignore
            chat_id=requester_id,
            text="❌ Владелец номера отклонил запрос на отчёт.",
            reply_markup=get_main_reply_keyboard()
        )
    except TelegramForbiddenError:
        logger.info(f"Пользователь {requester_id} заблокировал бота.")

    requester_state = FSMContext(
        storage=state.storage,
        key=StorageKey(bot_id=callback.bot.id, chat_id=requester_id, user_id=requester_id) # type: ignore
    )
    await requester_state.clear()

    await callback.answer()


@router.message(F.text.lower() == "текущие лимиты")
async def show_current_limits(message: Message, state: FSMContext):
    # Тут выводим список категорий и их лимиты на текущий месяц
    first_day_of_month = date.today().replace(day=1)
    user_id = message.from_user.id # type: ignore
    user_info = await get_user_info(user_id)

    if not user_info:
        error_message = "Не удалось получить данные пользователя."
        await message.answer(
            f"❌ {error_message}\n Пожалуйста, попробуйте снова.",
            reply_markup=get_main_reply_keyboard()
        )
        logger.error(f"show_current_limits: {error_message}")
        return

    user_phone = user_info[0]
    is_phone_owner = user_info[2]

    result = await get_limits(user_phone, first_day_of_month)

    if len(result) == 0:
        if not is_phone_owner:
            phone_owner_info = await get_phone_owner_info(user_phone)
            if phone_owner_info is None:
                error_message = "Не удалось получить данные владельца номера."
                await message.answer(
                    f"❌ {error_message}\n Пожалуйста, попробуйте снова.",
                    reply_markup=get_main_reply_keyboard()
                )
                logger.error(f"show_current_limits: {error_message}")
                return

            phone_owner_id = phone_owner_info[0]
            await message.answer(
                "🗂 <b>Лимиты на этот месяц ещё не заданы</b>\n"
                f"Подождите когда <a href='tg://user?id={phone_owner_id}'>владелец<a> установит лимиты на этот месяц.",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard()
            ) 
            return

        # ИЗМЕНЕНО: категории и план трат теперь заводятся в Mini App
        # ("📅 План на год", см. keyboards.py и webapp/plan.html), а не
        # через чат - отдельный сценарий создания категории здесь больше
        # не нужен.
        await message.answer(
            "🗂 <b>Лимиты на этот месяц ещё не заданы</b>\n"
            "Откройте «📅 План на год», добавьте категории и заполните "
            "план трат - лимиты появятся автоматически при следующем отчёте.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        return

    MAX_CATEGORY_WIDTH = 18
    category_width = min(
        MAX_CATEGORY_WIDTH,
        max((len(category) for _, category, _ in result), default=10)
    )

    table_lines = []
    total_limit = 0
    for category_id, category, spending_limit in result:
        total_limit += spending_limit
        display_category = category if len(category) <= category_width else category[:category_width - 1] + "…"
        table_lines.append(f"{display_category:<{category_width}} {spending_limit:>7} руб.")

    table_lines.append("-" * (category_width + 12))
    table_lines.append(f"{'Итого:':<{category_width}} {total_limit:>7} руб.")

    text = "📊 <b>Лимиты на текущий месяц:</b>\n\n<pre>" + "\n".join(table_lines) + "</pre>"

    await message.answer(text, parse_mode="HTML")


async def download_and_send_report(bot, chat_id: int, month, limits, context, page, phone: str | None = None):
    """
    Скачивает траты из Т-Банка и отправляет готовый отчёт пользователю.
    Используется как при переиспользовании сохранённой сессии, так и
    сразу после полного логина (см. handlers/tbank_auth.py).

    ИЗМЕНЕНО: добавлен необязательный параметр phone (по умолчанию None -
    ничего не ломает для мест, где он не передан). Если phone передан и
    у пользователя заведён план (handlers/planning.py), лимиты на month
    автоматически пересчитываются по общему бюджету ПЕРЕД тем, как строить
    текст отчёта - см. services/budget_forecast.recalc_month_limits. Если
    плана нет, либо пересчёт не удался (например, не получилось скачать
    факт за один из прошлых месяцев) - используются те лимиты, что были
    переданы изначально (старое поведение, без деградации для пользователей
    без плана).
    """
    recalc_note = None

    if phone is not None:
        try:
            recalced = await budget_forecast.recalc_month_limits(phone, month, context, page)
            if recalced is not None:
                # лимиты в БД обновились - перечитываем свежие значения,
                # чтобы отчёт строился уже по ним.
                limits = await get_limits(phone, month)
                recalc_note = budget_forecast.format_recalc_note(recalced)
        except Exception as e:
            # Пересчёт - это дополнительная опция поверх основного отчёта,
            # его сбой не должен мешать пользователю увидеть сам отчёт.
            logger.exception(
                f"download_and_send_report: автопересчёт лимитов не удался "
                f"(phone={phone}, month={month}): {e}"
            )

    try:
        spendings = await tbank_client.download_spendings(context, page, month)
    except Exception as e:
        logger.exception(f"Ошибка при скачивании трат из Т-Банка (месяц={month}): {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Не удалось получить данные о тратах из Т-Банка. Попробуйте ещё раз позже.",
            reply_markup=get_main_reply_keyboard()
        )
        return

    # НОВОЕ: если лимиты только что пересчитались - показываем короткую
    # заметку об этом отдельным сообщением перед основным отчётом.
    if recalc_note:
        await bot.send_message(chat_id=chat_id, text=recalc_note, parse_mode="HTML")

    text = build_budget_report_text(month, limits, spendings)

    if text is None:
        await bot.send_message(
            chat_id=chat_id,
            text="ℹ️ За выбранный период расходов не найдено.",
            reply_markup=get_main_reply_keyboard()
        )
        return

    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=get_main_reply_keyboard())
