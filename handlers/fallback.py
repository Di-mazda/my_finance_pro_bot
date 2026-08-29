from aiogram import Router
from aiogram.types import Message

from keyboards import get_main_reply_keyboard

router = Router()


# ВАЖНО: у этого хендлера нет фильтров - он ловит любой текст, который не
# подошёл ни одному из хендлеров выше. Поэтому его router обязательно
# должен быть подключён ПОСЛЕДНИМ в handlers/__init__.py, иначе он
# перехватит сообщения, предназначенные другим хендлерам (например, ввод
# смс-кода или пароля в процессе логина).
@router.message()
async def mess(message: Message):
    await message.answer(
        "🤔 Не смог распознать вашу команду.\n"
        "Пожалуйста, выберите действие из меню внизу экрана.",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )
