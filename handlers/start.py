from aiogram import Router, F
from aiogram.filters import Command, or_f
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from database import get_user_info
from keyboards import get_main_reply_keyboard
from handlers.account import check_user
# НОВОЕ (03.09.2026): гасим сторожа (services/browser_watchdog.py) при
# явной отмене - иначе он через 10 минут ещё раз попытается закрыть уже
# закрытый браузер и очистить уже новое состояние пользователя.
from services.browser_watchdog import cancel_watchdog

router = Router()


@router.message(or_f(Command("start"), F.text.lower() == "старт"))
async def start(message: Message, state: FSMContext):
    #Авторизовываем пользователя
    await check_user(message, state)


@router.message(F.text.lower() == "отмена")
async def cancel(message: Message, state: FSMContext):

    user_id = message.from_user.id
    user_info = await get_user_info(user_id)

    await message.answer("🚫 Ввод отменён", reply_markup=get_main_reply_keyboard(is_authorized = user_info!=None))
    data = await state.get_data()
    browser = data.get("browser")
    # Было: (сторож не гасился явно)
    cancel_watchdog(data.get("watchdog_task"))  # НОВОЕ (03.09.2026)
    if browser:
        await browser.close()
    await state.clear()
