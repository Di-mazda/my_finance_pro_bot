import re

from aiogram import Router, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from forms.user import Form, Registration
from keyboards import get_main_reply_keyboard, get_cancel_keyboard, get_OK_keyboard, get_account_main_keyboard, get_permission_inline_keyboard, get_response_permission_inline_keyboard
from database import add_pre_user, add_user, delete_user, get_pre_user_name, get_user_info, set_user_name, get_phone_owner_info, set_user_phone_owner
from phone_utils import normalize_phone
from services import tbank_client

from logger_config import logger

router = Router()

# Проверка авторизации пользователя
async def check_user(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user_info(user_id)

    if user is None:
        #Региструем
        await state.update_data(end_point="registration")

        user_name = message.from_user.first_name or "Гость"
        await state.update_data(name=user_name)

        await message.answer(
            f"Добро пожаловать! 👋\n\n"
            f"Ваше имя *{user_name}*?\n\n"
            f"*Если все верно — нажмите кнопку ниже.\n"
            f"Если хотите другое имя — просто напишите его в ответ.*",
            parse_mode="Markdown",
            reply_markup=get_OK_keyboard(),
        )
        await state.set_state(Registration.name)
        return

    await message.answer(f"Добро пожаловать, {user[1]}! 👋\n\nС чего начнём?", reply_markup=get_main_reply_keyboard())


# Хэндлер для получения имени
@router.message(Registration.name, F.text)
async def process_name(message: Message, state: FSMContext):
    if message.text != "👍 Да, все верно":
        await state.update_data(name=message.text)

    data = await state.get_data()
    user_name = data["name"]
    user_id = message.from_user.id

    await add_pre_user(user_id, user_name)

    await message.answer(
        f"Отлично, {user_name}! 👋\n\n"
        "Для получения данных необходимо указать номер телефона, привязанный к Т-Банку.\n\n"
        "👉 *Введите ваш номер телефона*.\n\n"
        "🔒 *Безопасность:* Бот никогда не хранит пароли или коды от вашего аккаунта Т-Банка. "
        "Номер телефона нужен только для того, чтобы вам не пришлось вводить его заново при каждом запросе данных.",
        reply_markup=get_cancel_keyboard(),
        parse_mode="Markdown",
    )

    # Переводим в состояние ожидания телефона
    await state.set_state(Registration.phone)


# Хэндлер для получения номера телефона
@router.message(Registration.phone, F.text | F.contact)
async def process_phone(message: Message, state: FSMContext, playwright_instance):
    phone = None
    user_id = message.from_user.id

    # Вариант 1: Пользователь нажал на кнопку "Поделиться контактом"
    if message.contact:
         phone = normalize_phone(message.contact.phone_number)


    # Вариант 2: Пользователь ввел номер вручную текстом
    elif message.text:
        phone = normalize_phone(message.text)
        if phone is None:
            await message.answer(
                "❌ Похоже, вы ввели неверный формат номера.\n"
                "Пожалуйста, введите корректный номер телефона (например, +79991234567) "
                "или нажмите на кнопку ниже:"
            )
            return

    if phone:
        # Если это редактирование номера, то проверяем, не ввел ли пользователь свой же номер.
        user_data = await state.get_data()
        end_point = user_data.get("end_point","")
        if end_point == "set_phone":
            user_info = await get_user_info(user_id)
            if user_info[0] == phone:
                await message.answer('<b>Вы ввели свой номер</b>\nВведите другой номер или нажмите <b>«Отмена»</b>, если хотите отменить изменение номера.', parse_mode="HTML")
                return


        await state.update_data(phone=phone)

        owner = await get_phone_owner_info(phone)

        if owner is not None:
            #Проверяем, жив ли овнер
            try:
                # Имитируем, что бот набирает текст в чате с пользователем
                await message.bot.send_chat_action(chat_id=owner[0], action="typing")
                is_owner_available = True
            except TelegramForbiddenError:
                # Пользователь заблокировал бота или остановил его, удаляем его
                await delete_user(owner[0])
                is_owner_available = False

            if is_owner_available:
                await message.answer(
                    "<b>Пользователь с таким номером уже зарегистрирован.</b>\n"
                    f"Вы можете просматривать лимиты и отчёты пользователя с номером {phone}, если <i>запросите у него разрешение</i>\n"
                    "или можете <i>изменить номер телефона.</i>",
                    parse_mode="HTML",
                    reply_markup=get_permission_inline_keyboard()
                )
                return


        await message.answer("Хорошо! Давайте убедимся, что этот номер действительно ваш, пройдите, пожалуйста, быструю авторизацию в <b>Т-Банке</b>.", parse_mode="HTML")
        await message.answer("⏳ Авторизуюсь в Т-Банке, пожалуйста ожидайте...")

        # ИЗМЕНЕНО: раньше здесь был жёстко зашит headless=True, из-за чего
        # переменная окружения BROWSER_HEADLESS (см. services/tbank_client.py)
        # тут бы не сработала, даже если её выставить. Теперь используем
        # значение по умолчанию, как и в остальных местах вызова
        # launch_browser, чтобы headless управлялся из одного места.
        # Было: browser = await tbank_client.launch_browser(playwright_instance, True)
        browser = await tbank_client.launch_browser(playwright_instance)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            await tbank_client.start_phone_login(page, phone)

            await state.update_data(browser=browser, context=context, page=page)
            await state.set_state(Form.sms)

            await message.answer(
                f"💬 Т-Банк отправил код для входа на номер {phone}. Пожалуйста, введите код сюда в чат.\n"
                "🛡️<b>Не переживайте о безопасности:</b> мы не храним ваши пароли и коды, а ради вашей безопасности <b><i>сообщение с кодом автоматически удалится из этого чата</i></b> у вас и у нас.",
                parse_mode="HTML"
            )

        except Exception as e:
            await browser.close()
            await state.clear()
            await message.answer(f"❌ Ошибка при авторизации. Ошибка: {e}")


@router.message(F.text.lower() == "мой аккаунт")
async def show_account_info(message: Message, state: FSMContext):
    user_data = await get_user_info(message.from_user.id)

    if user_data is None:
        logger.warning(f"show_account_info: нет данных пользователя user_id={message.from_user.id}")
        await message.answer(
            "⚠️ Не нашёл ваши данные. Похоже, нужно пройти регистрацию заново.",
            reply_markup=get_main_reply_keyboard(is_authorized=False)
        )
        return

    await message.answer(
        "<b>Ваши данные:</b>\n"
        f"<b>Имя:</b> {user_data[1]}\n"
        f"<b>Номер телефона:</b> {user_data[0]}\n"
        f"<b>Вы владелец номера:</b> {'Да' if user_data[2] else 'Нет'}",
        parse_mode="HTML",
        reply_markup=get_account_main_keyboard()
    )


@router.callback_query(lambda c: c.data == "set_name")
async def set_name_handler(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
            "*Смена имени*\n"
            "Введите имя.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard(),
        )
    await state.set_state(Form.edit_name)
    await callback.answer()  # Обязательно закрываем callback


@router.message(Form.edit_name, F.text)
async def set_name(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_name = message.text
    await set_user_name(user_id, user_name)
    await message.answer(
            f"✅ Отлично, {user_name}!\nИмя изменено.",
            reply_markup=get_main_reply_keyboard(),
        )
    await state.clear()


@router.callback_query(lambda c: c.data == "set_phone")
async def set_phone_handler(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
            "*Смена номера*\n"
            "Введите номер.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard(),
        )
    await state.set_state(Registration.phone)

    user_data = await state.get_data()
    end_point = user_data.get("end_point")
    if not end_point:
        await state.update_data(end_point="set_phone")

    await callback.answer()  # Обязательно закрываем callback


@router.callback_query(lambda c: c.data == "request_permission")
async def request_permission_handler(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_data = await state.get_data()
    name = user_data.get("name")

    phone = user_data.get("phone")

    owner_info = await get_phone_owner_info(phone)
    owner_id = owner_info[0]

    try:
        await callback.bot.send_message(
            chat_id=owner_id,
            text=f"Пользователь <a href='tg://user?id={user_id}'>{name}</a> просит у вас разрешение на просмотр ваших категорий, лимитов и отчётов.\n\n"
                "<b>Хотите предоставить право на просмотр этому пользователю?</b>",
                parse_mode="HTML",
                reply_markup=get_response_permission_inline_keyboard(user_id)
        )
        message_sent = True
    except TelegramForbiddenError:
        # Пользователь заблокировал бота или остановил его, удаляем его
        await delete_user(owner_id)
        message_sent = False
    except Exception as e:
        logger.exception(f"Произошла ошибка при отправке сообщения другому пользователю: {e}")
        message_sent = False

    if message_sent:
        await callback.message.answer(
                "<b>Запрос отправлен</b>\n"
                "Пользователь получил сообщение с запросом на предоставление вам прав на просмотр его категорий, лимитов и отчётов.\n"
                "Когда пользователь даст свой ответ я пришлю вам сообщение с результатом.\n\n"
                "<b><i>Ожидайте ответ от пользователя</i></b> или отмените регистрацию и начните заного с другим номером.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
        await state.set_state(Registration.waiting_permission)

    else:
        await callback.message.answer(
                "<b>Ошибка!</b>\nВозникла непредвиденная ошибка, пожалуйста нажмите или введите <b>Отмена</b>.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
        await state.clear()

    await callback.answer()  # Обязательно закрываем callback


@router.message(Registration.waiting_permission, F.text)
async def waiting_permission_handler(message: Message, state: FSMContext):
    await message.answer(
            "<i>Пожалуйста, ожидайте, пока пользователь ответит на запрос.</i>\n"
            'Или нажмите <b>"Отмена"</b> и пройдите регистрацию заново',
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )


@router.callback_query(lambda c: c.data.startswith("yes_response_permission:"))
async def yes_response_permission_handler(callback: CallbackQuery, state: FSMContext):

    owner_id = callback.from_user.id
    owner_info = await get_user_info(owner_id)
    owner_name = owner_info[1]
    owner_phone = owner_info[0]

    
    requester_id = int(callback.data.removeprefix("yes_response_permission:"))
    requester_info = await get_user_info(requester_id)

    if requester_info is None:

        requester_name = await get_pre_user_name(requester_id)

        #Сообщение владельцу номера - овнера, который предоставил доступ
        await callback.message.answer(
            f"<b>Вы предоставили разрешение</b> на просмотр ваших категорий, лимитов и отчётов, пользователю <a href='tg://user?id={requester_id}'>{requester_name}</a>\n\n"
            "Когда пользователь захочет посмотреть ваш отчёт, в этот чат придёт сообщение с запросом ввода данных для входа в ваш банк.\n\n"
            "🔒 <b>Безопасность:</b> Бот никогда не отправит ваши пароли или коды другим пользователям, введенные вами пароли сразу же удаляются.",
            parse_mode="HTML"
        )

        try:
            #Сообщение пользователю который делал запрос
            #Сначала делаем попытку отправить сообщение, и если пользователь не заблокировал бота, добавляем юзера
            await callback.bot.send_message(
                chat_id=requester_id,
                text=f"Отлично! Вы прошли регистрацию под номером {owner_phone}.\n"
                f"Пользователь <a href='tg://user?id={owner_id}'>{owner_name}</a> <b>предоставил вам разрешение</b> на просмотр его категорий, лимитов и отчётов.\n\n"
                "<i>Если вы захотите изменить номер, вы всегда сможете сделать это в меню «Мой аккаунт».</i>\n",
                parse_mode="HTML"
            )

            await add_user(requester_id, owner_phone, requester_name, False)

            await callback.bot.send_message(
                chat_id=requester_id,
                text="Теперь вы можете посмотреть лимиты и сформировать отчёт.\n"
                "<i>Используйте меню кнопок для быстрой навигации.</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard()
            )

            requester_state = FSMContext(
                storage=state.storage,
                key=StorageKey(bot_id=callback.bot.id, chat_id=requester_id, user_id=requester_id)
            )
            await requester_state.clear()
        except Exception as e:
            logger.exception(f"Не удалось завершить регистрацию пользователя {requester_id}: {e}")

    else:
        #Сообщение владельцу номера - овнера, который предоставил доступ
        await callback.message.answer("<i>Ошибка! Извините, пользователь уже зарегистрирован.</i>", parse_mode="HTML")

    await callback.answer()  # Обязательно закрываем callback


@router.callback_query(lambda c: c.data.startswith("no_response_permission:"))
async def no_response_permission_handler(callback: CallbackQuery, state: FSMContext):

    requester_id = int(callback.data.removeprefix("no_response_permission:"))
    requester_name = await get_pre_user_name(requester_id)

    #Сообщение владельцу номера - овнера, который предоставил доступ
    await callback.message.answer(
        f"<b>Вы отклонили запрос</b> пользователя <a href='tg://user?id={requester_id}'>{requester_name}</a>.",
        parse_mode="HTML"
    )

    requester_info = await get_user_info(requester_id)
    if requester_info is None:
        #Сообщение пользователю который делал запрос
        try:
            await callback.bot.send_message(
                chat_id=requester_id,
                text="К сожалению, пользователь <b>отклонил запрос</b> на предоставление доступа к его категориям, лимитам и отчётам.\n"
                "<i>Пожалуйста введите другой номер телефона:</i>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
            requester_state = FSMContext(
                storage=state.storage,
                key=StorageKey(bot_id=callback.bot.id, chat_id=requester_id, user_id=requester_id)
            )
            await requester_state.set_state(Registration.phone)
        except Exception as e:
            logger.exception(f"Не удалось уведомить пользователя {requester_id}: {e}")

    await callback.answer()  # Обязательно закрываем callback
