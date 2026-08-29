from os import getenv
from dotenv import load_dotenv

# ВАЖНО: load_dotenv() должен вызываться ДО импорта локальных модулей
load_dotenv()

import asyncio
import platform
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from playwright.async_api import async_playwright
from handlers import router
from database import init_db, close_pool
from logger_config import logger
# НОВОЕ: HTTP API + статика для Telegram Mini App "План на год" (см.
# keyboards.py - кнопка теперь открывает веб-таблицу вместо чатового
# сценария handlers/planning.py). Сервер поднимается в этом же процессе и
# event loop, что и сам бот - см. run_webapp_server() ниже.
from services.webapp_api import run_webapp_server

TOKEN = getenv("BOT_TOKEN")

dp = Dispatcher()
dp.include_router(router)


@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logger.exception(f"Необработанная ошибка в хендлере: {event.exception}")
    # True - сообщаем aiogram, что ошибка обработана, и polling продолжает работать
    return True


def _maybe_start_virtual_display():
    """
    НОВОЕ: опциональный запуск виртуального X-дисплея (Xvfb) через пакет
    pyvirtualdisplay, чтобы Playwright можно было запускать в headful-режиме
    (headless=False) на сервере без монитора. Это нужно, чтобы обойти детект
    headless-браузера Т-Банком - подробное объяснение см. в комментарии в
    конце services/tbank_client.py ("ПРО HEADLESS И ФОРМУ Доступ заблокирован").

    Включается переменной окружения USE_VIRTUAL_DISPLAY=true. Если пакет не
    установлен, мы не на Linux, либо Xvfb не найден в системе - тихо
    логируем предупреждение и продолжаем работу в обычном режиме, ничего
    не ломая.

    Альтернатива без изменений в Python-коде: запускать процесс через
    `xvfb-run -a python main.py` (после `apt-get install xvfb`) и просто
    выставить BROWSER_HEADLESS=false в окружении - тогда эта функция не
    нужна вовсе.
    """
    if getenv("USE_VIRTUAL_DISPLAY", "false").strip().lower() != "true":
        return None

    if platform.system() != "Linux":
        logger.warning(
            f"USE_VIRTUAL_DISPLAY=true, но Xvfb - линуксовая технология "
            f"(X11), на {platform.system()} её нет и быть не может. "
            "Пропускаем запуск виртуального дисплея, бот продолжит работу "
            "в обычном режиме. Для локального теста на Windows отключите "
            "USE_VIRTUAL_DISPLAY (или уберите его из .env) и выставьте "
            "BROWSER_HEADLESS=false - Playwright откроет обычное видимое "
            "окно браузера на вашем рабочем столе."
        )
        return None

    try:
        from pyvirtualdisplay import Display
    except ImportError:
        logger.warning(
            "USE_VIRTUAL_DISPLAY=true, но пакет pyvirtualdisplay не установлен "
            "(pip install pyvirtualdisplay) и/или в системе нет xvfb "
            "(apt-get install xvfb). Работаем в обычном headless-режиме."
        )
        return None

    try:
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        logger.info("Виртуальный дисплей Xvfb запущен - можно использовать BROWSER_HEADLESS=false.")
        return display
    except Exception as e:
        logger.exception(f"Не удалось запустить виртуальный дисплей Xvfb: {e}")
        return None


async def main():
    bot = Bot(token=TOKEN)

    virtual_display = _maybe_start_virtual_display()
    web_runner = None

    try:
        async with async_playwright() as p:
            print("Бот запущен...")
            # ПРИМЕЧАНИЕ: новые таблицы/колонки (salary_plan, category_plan,
            # category_plan.no_recalc, categories.is_protected,
            # limits.is_manual, budget_settings) создаются/мигрируются
            # внутри самой database.init_db() (см. database.py), поэтому
            # здесь достаточно уже существующего вызова.
            await init_db()
            # НОВОЕ: поднимаем HTTP-сервер Mini App ДО start_polling - он не
            # блокирует event loop (site.start() возвращается сразу), а
            # start_polling ниже уже занимает его на всё время жизни бота.
            web_runner = await run_webapp_server()
            await dp.start_polling(bot, playwright_instance=p)
    except Exception as e:
        logger.exception(f"Критическая ошибка, бот остановлен: {e}")
        raise
    finally:
        await bot.session.close()
        # НОВОЕ: закрываем пул соединений с Postgres при остановке бота -
        # раньше с aiosqlite это не требовалось (соединение открывалось и
        # закрывалось на каждый вызов), но пул asyncpg держит соединения
        # открытыми постоянно, поэтому его нужно закрыть явно.
        await close_pool()
        if web_runner is not None:
            await web_runner.cleanup()
        if virtual_display is not None:
            virtual_display.stop()


if __name__ == "__main__":
    asyncio.run(main())
