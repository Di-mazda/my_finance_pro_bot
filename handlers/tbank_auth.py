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
from services.browser_watchdog import cancel_watchdog

from logger_config import logger

router = Router()


# ============================================================================
# ЦИКЛ АВТОРИЗАЦИИ (диспетчер) - см. диаграмму "КАК ДОЛЖНО РАБОТАТЬ".
# ============================================================================
# Раньше поток входа был линейным и жёстко зашитым: process_sms_code сам
# знал, что дальше пароль или пин; process_password сам ждал пин. Любой
# неожиданный шаг банка ломал сценарий и вход начинался с телефона заново.
#
# Теперь всё крутится вокруг get_page_type. advance_auth - единая точка,
# которая:
#   1) спрашивает get_page_type "какая форма сейчас открыта?";
#   2) для форм, требующих данных от пользователя (sms/password/pin) -
#      ставит нужный set_state, просит ввод в чат и ВЫХОДИТ (ждёт ответ);
#   3) для шагов, которые можно пройти без пользователя (phone - ввести
#      номер из state; bio - скипнуть) - выполняет действие и снова зовёт
#      саму себя (это и есть "стрелка назад в get_page_type" на диаграмме);
#   4) для lk - вызывает _after_login (end_point);
#   5) для blocked/unknown/ошибок - аккуратно завершает вход с сообщением.
#
# Каждый process_* хендлер ниже теперь только вводит свои данные и в конце
# снова зовёт advance_auth - т.е. управление всегда возвращается к
# get_page_type, ровно как на схеме.

# Максимум переходов за один "прогон" advance_auth без ожидания ввода от
# пользователя - страховка от бесконечного цикла, если банк вдруг начнёт
# отдавать форму, которую мы не умеем закрывать сами.
_MAX_AUTOSTEPS = 10


async def _fail_auth(message: Message, state: FSMContext, text: str):
    """Единое аккуратное завершение входа при ошибке: сообщение + очистка."""
    data = await state.get_data()
    browser = data.get("browser")
    end_point = data.get("end_point", "")
    is_authorized = end_point != "registration"

    await message.answer(text, reply_markup=get_main_reply_keyboard(is_authorized))
    cancel_watchdog(data.get("watchdog_task"))
    if browser:
        await browser.close()
    await state.clear()


async def advance_auth(message: Message, state: FSMContext):
    """
    Диспетчер шага авторизации. Смотрит get_page_type и либо запрашивает у
    пользователя данные (и выходит), либо сам проходит шаг и повторяет,
    либо завершает вход через _after_login.
    """
    data = await state.get_data()
    page = data.get("page")
    phone = data.get("phone")

    if not page:
        await _fail_auth(message, state, "❌ Ошибка: сессия потеряна. Начните заново.")
        return

    for _ in range(_MAX_AUTOSTEPS):
        try:
            page_type = await tbank_client.get_page_type(page)
        except Exception as e:
            logger.exception(f"advance_auth: get_page_type упал (user_id={message.from_user.id}): {e}")  # type: ignore
            await _fail_auth(message, state, f"❌ Не удалось определить состояние входа. Ошибка: {e}")
            return

        # --- Формы, требующие данных от пользователя: просим ввод и выходим ---
        if page_type == "sms":
            await state.set_state(Form.sms)
            await message.answer(
                f"💬 Т-Банк отправил код для входа на номер {phone}. Пожалуйста, введите код сюда в чат.\n"
                "Мы не храним ваши пароли и коды! <b><i>Сообщение с кодом автоматически удалится из этого чата.</i></b>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(),
            )
            return

        elif page_type == "password":
            await state.set_state(Form.password)
            await message.answer(
                "Т-Банк запрашивает пароль. Пожалуйста, введите пароль сюда в чат.\n"
                "Мы не храним ваши пароли и коды! <b><i>Сообщение с паролем автоматически удалится из этого чата.</i></b>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(),
            )
            return

        elif page_type == "pin":
            await state.set_state(Form.pin)
            await message.answer(
                "Т-Банк запрашивает пин-код. Пожалуйста, введите пин-код сюда в чат.\n"
                "Мы не храним ваши пароли и коды! <b><i>Сообщение с пин-кодом автоматически удалится из этого чата.</i></b>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(),
            )
            return

        # --- Шаги, которые проходим сами и снова зовём get_page_type ---
        elif page_type == "phone":
            # Мы на форме номера, а номер уже известен из state - вводим его
            # сами и продолжаем цикл (не гоняем пользователя вводить телефон).
            if not phone:
                await _fail_auth(message, state, "❌ Ошибка входа: номер телефона не задан.")
                return
            try:
                await tbank_client.start_phone_login(page, phone)
            except Exception as e:
                logger.exception(f"advance_auth: ввод телефона упал (user_id={message.from_user.id}): {e}")  # type: ignore
                await _fail_auth(message, state, f"❌ Ошибка при вводе номера. Ошибка: {e}")
                return
            continue

        elif page_type == "bio":
            try:
                await tbank_client.skip_bio(page)
            except Exception as e:
                logger.exception(f"advance_auth: скип биометрии упал (user_id={message.from_user.id}): {e}")  # type: ignore
                await _fail_auth(message, state, f"❌ Ошибка на экране биометрии. Ошибка: {e}")
                return
            continue

        # --- Конечная точка: успех ---
        elif page_type == "lk":
            await _after_login(message, state)
            return

        # --- Проблемные состояния ---
        elif page_type == "blocked":
            logger.warning(f"advance_auth: банк показал 'Доступ заблокирован' (user_id={message.from_user.id})")  # type: ignore
            await _fail_auth(
                message, state,
                "❌ Т-Банк заблокировал автоматический вход (антифрод). "
                "Попробуйте позже.",
            )
            return

        else:  # "unknown"
            await _fail_auth(
                message, state,
                "❌ Не удалось распознать страницу входа. Попробуйте начать заново.",
            )
            return

    # Вышли из цикла, ни разу не попросив ввод и не дойдя до ЛК.
    await _fail_auth(message, state, "❌ Вход зациклился на неизвестном шаге. Попробуйте заново.")


# --- ХЕНДЛЕР ПОЛУЧЕНИЯ КОДА ДЛЯ ВХОДА (СМС) ---
@router.message(Form.sms, F.text)
async def process_sms_code(message: Message, state: FSMContext):
    sms_code = message.text.strip()  # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с кодом удалено</i>", parse_mode="HTML")
    await message.answer("💬 Код принят. Ожидайте...")

    data = await state.get_data()
    page = data.get("page")

    if not page:
        await _fail_auth(message, state, "❌ Ошибка: сессия потеряна. Начните заново.")
        return

    try:
        await tbank_client.submit_sms_code(page, sms_code)
    except Exception as e:
        logger.exception(f"Ошибка при вводе смс-кода (user_id={message.from_user.id}): {e}")  # type: ignore
        await _fail_auth(message, state, f"❌ Ошибка при вводе кода или неверный код. Ошибка: {e}")
        return

    # Управление снова уходит в get_page_type - он решит, что дальше.
    await advance_auth(message, state)


# --- ХЕНДЛЕР ВВОДА ПАРОЛЯ ---
@router.message(Form.password, F.text)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()  # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с паролем удалено</i>", parse_mode="HTML")
    await message.answer("Пароль принят. Ожидайте...")

    data = await state.get_data()
    page = data.get("page")

    if not page:
        await _fail_auth(message, state, "❌ Ошибка: сессия потеряна. Начните заново.")
        return

    try:
        await tbank_client.submit_password(page, password)
    except Exception as e:
        logger.exception(f"Ошибка при вводе пароля (user_id={message.from_user.id}): {e}")  # type: ignore
        await _fail_auth(message, state, f"❌ Ошибка при вводе пароля. Ошибка: {e}")
        return

    await advance_auth(message, state)


# --- ВВОД ПИН-КОДА ---
@router.message(Form.pin, F.text)
async def process_pin(message: Message, state: FSMContext):
    pin_code = message.text.strip()  # type: ignore
    await message.delete()
    await message.answer("<i>Сообщение с пин-кодом удалено</i>", parse_mode="HTML")

    if not pin_code.isdigit():
        await message.answer("⚠️ Пин-код должен состоять только из цифр. Попробуйте ещё раз:")
        return

    await message.answer("Пин-код принят. Ожидайте...")

    data = await state.get_data()
    page = data.get("page")

    if not page:
        await _fail_auth(message, state, "❌ Ошибка: сессия потеряна. Начните заново.")
        return

    try:
        await tbank_client.submit_pin(page, pin_code)
    except Exception as e:
        logger.exception(f"Ошибка при вводе пин-кода (user_id={message.from_user.id}): {e}")  # type: ignore
        await _fail_auth(message, state, f"❌ Ошибка при вводе пин-кода или неверный пин-код. Ошибка: {e}")
        return

    await advance_auth(message, state)


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
            # ЛЕНИВЫЙ импорт (см. комментарий у импортов вверху файла) -
            # разрывает цикл reports <-> tbank_auth.
            from handlers.reports import download_and_send_report
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
        cancel_watchdog(data.get("watchdog_task"))
        if browser:
            await browser.close()
        await state.clear()