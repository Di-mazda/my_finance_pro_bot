"""
services/browser_watchdog.py

НОВОЕ (03.09.2026): сторож для сессий Playwright-браузера, которые
хранятся в FSM-состоянии между сообщениями (ввод смс-кода/пароля/пин-кода,
ожидание подтверждения владельцем номера в handlers/reports.py).

ПОЧЕМУ ЭТО ПОНАДОБИЛОСЬ:
Раньше единственным способом закрыть такой "подвешенный" браузер была
явная команда "Отмена" (handlers/start.py:cancel). Если пользователь (или
владелец номера, от которого ждут подтверждения/повторного входа) просто
переставал отвечать - браузер оставался открытым НАВСЕГДА. На Railway с
лимитом 1 ГБ памяти несколько таких "зависших" процессов Chromium
постепенно съедали всю доступную память, и в какой-то момент бот падал с
BrowserType.launch: Timeout ... exceeded, потому что новому браузеру
просто не хватало памяти на запуск. Перезапуск бота убивал все зависшие
процессы и временно решал проблему - но не устранял причину.

В handlers/reports.py (_request_report_for_non_owner) это ограничение
даже было явно описано как известное и не реализованное:
"если запрашивающий отменит ожидание ... browser не закроется
автоматически, пока владелец сам не отреагирует". Этот модуль закрывает
именно эту дыру, и все остальные похожие места (см. правки в
handlers/account.py, handlers/reports.py, handlers/tbank_auth.py,
handlers/start.py).

КАК ПОЛЬЗОВАТЬСЯ:
    watchdog_task = start_browser_watchdog(
        browser, state, bot=message.bot, chat_id=message.chat.id,
    )
    await state.update_data(browser=browser, ..., watchdog_task=watchdog_task)

    # А в любом месте, где браузер закрывается штатно (успех, ошибка,
    # явная отмена) - ОБЯЗАТЕЛЬНО отменяем сторожа, чтобы он не сработал
    # вхолостую позже:
    cancel_watchdog(watchdog_task)
    # или, если под рукой только словарь из state.get_data():
    cancel_watchdog(data.get("watchdog_task"))
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError

from logger_config import logger

# По умолчанию - 10 минут. От браузера в этом боте требуется только
# авторизоваться (ввести смс/пароль/пин) - на это с большим запасом
# хватает 10 минут, даже если пользователь не торопится.
DEFAULT_MAX_LIFETIME_SECONDS = 10 * 60


def start_browser_watchdog(
    browser,
    state: FSMContext,
    bot=None,
    chat_id: int | None = None,
    max_lifetime: float = DEFAULT_MAX_LIFETIME_SECONDS,
) -> asyncio.Task:
    """
    Запускает фоновую задачу, которая через max_lifetime секунд:
      1. закрывает browser (если ещё не закрыт);
      2. очищает FSM-состояние state, чтобы пользователь не застрял
         навсегда в состоянии Form.sms/password/pin;
      3. если переданы bot и chat_id - уведомляет, что сессия истекла.

    Если сценарий входа успел завершиться раньше (успешно или с ошибкой),
    вызывающий код должен сам отменить возвращённую задачу через
    cancel_watchdog() - тогда таймер просто тихо остановится в блоке
    except asyncio.CancelledError ниже, ничего не закрывая повторно.
    """

    async def _watchdog():
        try:
            await asyncio.sleep(max_lifetime)
        except asyncio.CancelledError:
            # Штатная отмена - сценарий сам успел закрыть браузер вовремя.
            return

        logger.warning(
            "browser_watchdog: браузер не был закрыт за отведённое время "
            f"({max_lifetime:.0f} сек) - принудительно закрываю и сбрасываю "
            f"состояние (chat_id={chat_id})."
        )

        try:
            await browser.close()
        except Exception as e:
            # Браузер мог быть уже закрыт штатно чуть раньше, чем
            # отменилась задача (небольшое окно гонки) - это не проблема.
            logger.info(f"browser_watchdog: браузер уже был закрыт или ошибка закрытия: {e}")

        try:
            await state.clear()
        except Exception as e:
            logger.warning(f"browser_watchdog: не удалось очистить состояние: {e}")

        if bot is not None and chat_id is not None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="⌛ Время на вход в Т-Банк истекло. Пожалуйста, начните заново.",
                )
            except TelegramForbiddenError:
                pass
            except Exception as e:
                logger.warning(f"browser_watchdog: не удалось уведомить chat_id={chat_id}: {e}")

    return asyncio.create_task(_watchdog())


def cancel_watchdog(task: asyncio.Task | None) -> None:
    """Отменяет ранее запущенного сторожа (см. start_browser_watchdog), если он ещё жив."""
    if task is not None and not task.done():
        task.cancel()
