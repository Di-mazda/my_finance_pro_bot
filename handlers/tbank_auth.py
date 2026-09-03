from datetime import date

from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramForbiddenError

from forms.user import Form
from keyboards import get_main_reply_keyboard, get_cancel_keyboard
from database import get_limits, get_phone_owner_info, get_user_info, set_user_phone_owner, set_user_session_json, add_user, set_user_phone
from services import tbank_client
from handlers.reports import download_and_send_report
# НОВОЕ (03.09.2026): гасим сторожа (services/browser_watchdog.py) в
# каждом месте, где браузер закрывается штатно - см. разбор утечки памяти.
from services.browser_watchdog import cancel_watchdog

from logger_config import logger

router = Router()


# --- ХЕНДЛЕР ПОЛУЧЕНИЯ КОДА ДЛЯ ВХОДА (СМС) ---
@router.message(Form.sms, F.text)
async def process_sms_code(message: Message, state: FSMContext):
    sms_code = message.text.strip() # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с кодом удалено</i>", parse_mode="HTML")
    await message.answer("💬 Код принят. Ожидайте...")

    data = await state.get_data()
    browser = data.get("browser")
    context = data.get("context")
    page = data.get("page")
    watchdog_task = data.get("watchdog_task")

    if not page:
        end_point = data.get("end_point", "")
        is_authorized = end_point != "registration"
        await message.answer("❌ Ошибка: сессия потеряна. Начните заново", reply_markup=get_main_reply_keyboard(is_authorized))
        # НОВОЕ (03.09.2026): гасим сторожа вместе с закрытием браузера.
        cancel_watchdog(watchdog_task)
        if browser:
            await browser.close()
        await state.clear()
        return

    try:
        # Дальше может появиться либо форма ввода пароля, либо ввода пин-кода
        next_step = await tbank_client.submit_sms_code(page, sms_code)
        # Было: await state.update_data(browser=browser, context=context, page=page)
        await state.update_data(browser=browser, context=context, page=page, watchdog_task=watchdog_task)

        if next_step == "password":
            await state.set_state(Form.password)
            await message.answer(
                "Т-Банк запрашивает пароль. Пожалуйста, введите пароль сюда в чат.\n"
                "Мы не храним ваши пароли и коды! <b><i>Сообщение с паролем автоматически удалится из этого чата.</i></b>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
        else:  # "pin"
            await state.set_state(Form.pin)
            await message.answer(
                "Т-Банк запрашивает пин-код. Пожалуйста, введите пин-код сюда в чат.\n"
                "Мы не храним ваши пароли и коды! <b><i>Сообщение с пин-кодом автоматически удалится из этого чата.</i></b>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )

    except Exception as e:
        logger.exception(f"Ошибка при вводе смс-кода (user_id={message.from_user.id}): {e}") # type: ignore
        await message.answer(f"❌ Ошибка при вводе кода или неверный код. Ошибка: {e}")
        cancel_watchdog(watchdog_task)  # НОВОЕ (03.09.2026)
        if browser:
            await browser.close()
        await state.clear()


# --- ХЕНДЛЕР ВВОДА ПАРОЛЯ ---
@router.message(Form.password, F.text)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip() # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с паролем удалено</i>", parse_mode="HTML")
    await message.answer("Пароль принят. Ожидайте...")

    data = await state.get_data()
    browser = data.get("browser")
    context = data.get("context")
    page = data.get("page")
    watchdog_task = data.get("watchdog_task")

    if not page:
        end_point = data.get("end_point", "")
        is_authorized = end_point != "registration"
        await message.answer("❌ Ошибка: сессия потеряна. Начните заново", reply_markup=get_main_reply_keyboard(is_authorized))
        # ИСПРАВЛЕНО (03.09.2026): раньше здесь браузер НЕ закрывался (в
        # отличие от аналогичных веток в process_sms_code/process_pin) -
        # это была настоящая утечка: если состояние "потерялось" именно на
        # шаге пароля, browser оставался висеть в памяти навсегда. Заодно
        # гасим сторожа, раз закрываем браузер сами.
        # Было: await state.clear()
        cancel_watchdog(watchdog_task)
        if browser:
            await browser.close()
        await state.clear()
        return

    try:
        await tbank_client.submit_password(page, password)
    except Exception as e:
        logger.exception(f"Ошибка при вводе пароля (user_id={message.from_user.id}): {e}") # type: ignore
        await message.answer(f"❌ Ошибка при вводе пароля. Ошибка: {e}")
        cancel_watchdog(watchdog_task)  # НОВОЕ (03.09.2026)
        if browser:
            await browser.close()
        await state.clear()
        return

    # Было: await state.update_data(browser=browser, context=context, page=page)
    await state.update_data(browser=browser, context=context, page=page, watchdog_task=watchdog_task)

    try:
        await tbank_client.wait_for_pin_form(page)
    except Exception as e:
        logger.exception(f"Форма пин-кода не появилась после ввода пароля (user_id={message.from_user.id}): {e}") # type: ignore
        await message.answer(f"❌ Не дождались формы пин-кода после пароля. Ошибка: {e}")
        cancel_watchdog(watchdog_task)  # НОВОЕ (03.09.2026)
        if browser:
            await browser.close()
        await state.clear()
        return

    await state.set_state(Form.pin)
    await message.answer(
        "Т-Банк запрашивает пин-код. Пожалуйста, введите пин-код сюда в чат.\n"
        "Мы не храним ваши пароли и коды! <b><i>Сообщение с пин-кодом автоматически удалится из этого чата.</i></b>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )


# --- ВВОД ПИН-КОДА ---
@router.message(Form.pin, F.text)
async def process_pin(message: Message, state: FSMContext):
    pin_code = message.text.strip() # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с пин-кодом удалено</i>", parse_mode="HTML")

    if not pin_code.isdigit():
        await message.answer("⚠️ Пин-код должен состоять только из цифр. Попробуйте ещё раз:")
        return

    await message.answer("Пин-код принят. Ожидайте...")

    data = await state.get_data()
    browser = data.get("browser")
    context = data.get("context")
    page = data.get("page")
    watchdog_task = data.get("watchdog_task")

    if not page:
        end_point = data.get("end_point", "")
        is_authorized = end_point != "registration"
        await message.answer("❌ Ошибка: сессия потеряна. Начните заново", reply_markup=get_main_reply_keyboard(is_authorized))
        cancel_watchdog(watchdog_task)  # НОВОЕ (03.09.2026)
        if browser:
            await browser.close()
        await state.clear()
        return

    try:
        await tbank_client.submit_pin(page, pin_code)
    except Exception as e:
        logger.exception(f"Ошибка при вводе пин-кода (user_id={message.from_user.id}): {e}") # type: ignore
        await message.answer(f"❌ Ошибка при вводе пин-кода или неверный пин-код. Ошибка: {e}")
        cancel_watchdog(watchdog_task)  # НОВОЕ (03.09.2026)
        if browser:
            await browser.close()
        await state.clear()
        return

    await _after_login(message, state)


async def _after_login(message: Message, state: FSMContext):
    """Сохраняет сессию в БД и отправляет результат в зависимости от end_point."""
    user_id = message.from_user.id # type: ignore
    data = await state.get_data()
    name = data.get("name", "")
    phone = data.get("phone")
    browser = data.get("browser")
    context = data.get("context")
    page = data.get("page")
    end_point = data.get("end_point", "")
    month = data.get("month")
    limits = data.get("limits")

    # Поддержка сценария "не-owner запросил отчёт".
    # Если вход в Т-Банк выполнялся не ради
    # обычного собственного отчёта пользователя, а либо (а) владельцем
    # номера по запросу другого пользователя, либо (б) самим не-owner
    # пользователем от имени владельца - report_recipient_id указывает,
    # кому отправить готовый отчёт, а session_owner_id - под чьим id
    # сохранить полученную сессию Т-Банка. По умолчанию оба совпадают с
    # user_id (как и было раньше для обычного собственного входа).
    report_recipient_id = data.get("report_recipient_id", user_id)
    session_owner_id = data.get("session_owner_id", user_id)

    try:
        # Если end point не задан, просто выводим отчет.
        if not end_point:
            # Сохраняем новую сессию в БД.
            # сохраняем под session_owner_id (см. комментарий выше)
            session_json_str = await tbank_client.save_session(context)
            await set_user_session_json(session_owner_id, session_json_str)

            if month is None:
                month = date.today().replace(day=1)
            if phone is None:
                user_info = await get_user_info(user_id)
                phone = user_info[0] # type: ignore
            if limits is None:
                limits = await get_limits(phone, month)

            # ИЗМЕНЕНО: добавлен phone=phone, чтобы download_and_send_report
            # мог запустить автопересчёт лимитов по плану (см.
            # services/budget_forecast.py). phone здесь - это номер, на
            # который заведены категории/лимиты (владельца), уже вычислен
            # чуть выше в этой функции.
            # Было: await download_and_send_report(message.bot, report_recipient_id, month, limits, context, page)
            await download_and_send_report(message.bot, report_recipient_id, month, limits, context, page, phone=phone)

            if report_recipient_id != user_id:
                try:
                    await message.bot.send_message( # type: ignore
                        chat_id=report_recipient_id,
                        text="✅ Владелец номера подтвердил доступ, отчёт готов выше."
                    )
                except TelegramForbiddenError:
                    logger.info(f"Пользователь {report_recipient_id} заблокировал бота.")

                requester_state = FSMContext(
                    storage=state.storage,
                    key=StorageKey(bot_id=message.bot.id, chat_id=report_recipient_id, user_id=report_recipient_id) # type: ignore
                )
                await requester_state.clear()

        # Регистрация нового пользователя.
        elif end_point == "registration":

            await add_user(user_id, phone, name, True)

            session_json_str = await tbank_client.save_session(context)
            await set_user_session_json(user_id, session_json_str)

            await message.answer("🎉 Отлично! Вы прошли регистрацию.\n"
                                f"Номер {phone} сохранён для входа в Т-Банк. 🔓\n\n"
                                "<i>Если вы захотите изменить номер, вы всегда сможете сделать это в меню «Мой аккаунт».</i>\n",
                                parse_mode="HTML")

            await message.answer("Теперь вы можете задать лимиты и сформировать отчёт.\n"
                                "<i>Используйте меню кнопок для быстрой навигации.</i>",
                                parse_mode="HTML", reply_markup=get_main_reply_keyboard())
        
        # Смена номера.
        elif end_point == "set_phone":
            user_owner = await get_phone_owner_info(phone)
            if user_owner is not None:

                if not name:
                    user_info = await get_user_info(user_id)
                    name = user_info[1] # type: ignore

                user_owner_id = user_owner[0]
                await set_user_phone_owner(user_owner_id, False)

                try:
                    await message.bot.send_message( # type: ignore
                        chat_id=user_owner_id,
                        text=f"📵 Ваш номер {phone} подтвердил другой пользователь.\n\n"
                            f"Теперь редактировать лимиты и смотреть отчёты по этому номеру может "
                            f"<a href='tg://user?id={user_id}'>{name}</a>. У вас эта возможность больше недоступна.\n\n"
                            "<i>Чтобы вернуть доступ, снова авторизуйтесь в Т-Банке по этому номеру телефона через этот чат-бот.</i>",
                            parse_mode="HTML"
                    )
                except TelegramForbiddenError:
                    logger.info(f"Пользователь {user_owner_id} заблокировал бота или остановил его.")

                except Exception as e:
                    logger.exception(f"Произошла другая ошибка при уведомлении владельца номера {user_owner_id}: {e}")

            await set_user_phone_owner(user_id, False)
            await set_user_phone(user_id, phone)
            await set_user_phone_owner(user_id, True)
            session_json_str = await tbank_client.save_session(context)
            await set_user_session_json(user_id, session_json_str)
            await message.answer(f"Номер {phone} успешно сохранён для входа в Т-Банк. 🔓", reply_markup=get_main_reply_keyboard())

    except Exception as e:
        logger.exception(f"Ошибка в _after_login (user_id={user_id}, end_point={end_point!r}): {e}")
        await message.answer(f"❌ Ошибка при завершении входа. Ошибка: {e}")
    finally:
        # НОВОЕ (03.09.2026): вход (успешно или нет) завершился - сторож
        # больше не нужен, гасим его вместе с закрытием браузера.
        cancel_watchdog(data.get("watchdog_task"))
        if browser:
            await browser.close()
        await state.clear()