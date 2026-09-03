"""
Работа с личным кабинетом Т-Банка через Playwright: логин (по сохранённой
сессии либо телефон+смс+пароль/пин) и выгрузка гистограммы трат.

Модуль ничего не знает про Telegram/aiogram - принимает объекты Playwright
(browser/context/page) и простые типы, возвращает данные или бросает
исключения. Ответы пользователю формирует вызывающий код в handlers/.
"""
import asyncio
import json
import os
import platform
import time
from calendar import monthrange
from datetime import date, datetime
from urllib.parse import urlencode
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from logger_config import logger

TARGET_URL = "https://www.tbank.ru/mybank/"


# НОВОЕ: управление headless через переменную окружения BROWSER_HEADLESS.
# По умолчанию (переменная не задана) остаёмся в headless=True, чтобы не
# менять поведение "из коробки". Чтобы включить headful-режим на сервере
# без монитора (это нужно, чтобы обойти детект бота Т-Банком) - выставьте
# BROWSER_HEADLESS=false и запускайте процесс под виртуальным дисплеем
# (Xvfb). Подробное объяснение - в комментарии в конце этого файла.
def _get_default_headless() -> bool:
    return os.getenv("BROWSER_HEADLESS", "true").strip().lower() != "false"


# ============================================================================
# НОВОЕ (03.09.2026): ограничение памяти на Railway (лимит 1 ГБ на процесс).
# ============================================================================
# 1) СЕМАФОР НА ОДНОВРЕМЕННЫЕ БРАУЗЕРЫ.
#    Каждый Chromium (особенно в headful-режиме, см. блок про Xvfb ниже) -
#    это 150-500 МБ памяти. Если два пользователя одновременно запросят
#    отчёт/логин, раньше могли подняться два процесса Chrome разом - на
#    1 ГБ памяти это реальный риск OOM. MAX_CONCURRENT_BROWSERS (по
#    умолчанию 1) гарантирует, что новый браузер не запустится, пока не
#    закроется предыдущий - следующий вызов launch_browser просто подождёт
#    своей очереди вместо того, чтобы запуститься параллельно.
#    Если понадобится больше параллелизма (например, после апгрейда тарифа
#    Railway) - поднимите переменную окружения MAX_CONCURRENT_BROWSERS.
_MAX_CONCURRENT_BROWSERS = max(1, int(os.getenv("MAX_CONCURRENT_BROWSERS", "1")))
_browser_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BROWSERS)

# 2) ЛЕНИВЫЙ Xvfb (виртуальный дисплей).
#    Раньше Xvfb поднимался один раз при старте бота (main.py:
#    _maybe_start_virtual_display) и жил ВСЮ жизнь процесса, даже если
#    браузер месяцами не открывался - это лишние ~20-50 МБ впустую.
#    Теперь дисплей поднимается лениво прямо здесь, непосредственно перед
#    первым headful-браузером, и гасится, когда закрывается ПОСЛЕДНИЙ
#    активный headful-браузер (считаем ссылки в _virtual_display_refcount).
#    Реальную экономию памяти это даёт небольшую (Xvfb сам по себе лёгкий,
#    ~20-50 МБ), но она не требует усилий и не мешает - см. также вопрос
#    про Xvfb в разборе утечки памяти в чате от 03.09.2026.
_virtual_display = None
_virtual_display_refcount = 0
_virtual_display_lock = asyncio.Lock()


async def _acquire_virtual_display() -> None:
    global _virtual_display, _virtual_display_refcount
    async with _virtual_display_lock:
        _virtual_display_refcount += 1
        if _virtual_display is not None:
            return  # уже поднят другим (параллельным) браузером

        if os.getenv("USE_VIRTUAL_DISPLAY", "false").strip().lower() != "true":
            return
        if platform.system() != "Linux":
            return
        try:
            from pyvirtualdisplay import Display
        except ImportError:
            logger.warning(
                "USE_VIRTUAL_DISPLAY=true, но пакет pyvirtualdisplay не установлен."
            )
            return
        try:
            _virtual_display = Display(visible=False, size=(1920, 1080))
            _virtual_display.start()
            logger.info("tbank_client: виртуальный дисплей Xvfb поднят лениво под headful-браузер.")
        except Exception as e:
            logger.exception(f"tbank_client: не удалось запустить Xvfb: {e}")
            _virtual_display = None


async def _release_virtual_display() -> None:
    global _virtual_display, _virtual_display_refcount
    async with _virtual_display_lock:
        _virtual_display_refcount = max(0, _virtual_display_refcount - 1)
        if _virtual_display_refcount == 0 and _virtual_display is not None:
            try:
                _virtual_display.stop()
                logger.info("tbank_client: виртуальный дисплей Xvfb остановлен - активных браузеров больше нет.")
            except Exception as e:
                logger.warning(f"tbank_client: ошибка при остановке Xvfb: {e}")
            _virtual_display = None


async def launch_browser(playwright_instance, headless: bool | None = None):
    # ИЗМЕНЕНО: headless теперь необязателен - если явно не передан, берём
    # значение из переменной окружения BROWSER_HEADLESS (см. DEFAULT_HEADLESS
    # выше). Так headless можно настроить один раз через .env, не трогая
    # каждое место вызова launch_browser (их несколько: handlers/reports.py,
    # handlers/account.py).
    # Было: async def launch_browser(playwright_instance, headless: bool = True):
    #
    # ИЗМЕНЕНО (03.09.2026): добавлены семафор на число одновременных
    # браузеров и ленивый подъём Xvfb - см. комментарий выше. Снаружи
    # ничего менять не нужно: вызывающий код по-прежнему просто получает
    # объект browser и вызывает browser.close() как раньше - семафор и
    # Xvfb освобождаются автоматически внутри обёрнутого close().
    # Было:
    #     if headless is None:
    #         headless = _get_default_headless()
    #     return await playwright_instance.chromium.launch(headless=headless)
    if headless is None:
        headless = _get_default_headless()

    await _browser_semaphore.acquire()
    if not headless:
        await _acquire_virtual_display()

    try:
        # ИСПРАВЛЕНО (03.09.2026, регрессия от правки того же дня): async_playwright()
        # в main.py поднимает Node.js-драйвер Playwright ОДИН РАЗ при старте бота, и
        # этот драйвер наследует переменные окружения (в т.ч. DISPLAY) от Python-
        # процесса именно в момент своего запуска - позже установленные переменные
        # в уже работающий дочерний процесс не попадают. Ленивый _acquire_virtual_display()
        # выше выставляет DISPLAY через pyvirtualdisplay уже ПОСЛЕ старта драйвера,
        # поэтому Chrome в headful-режиме падал с "Missing X server or $DISPLAY" -
        # именно эта ошибка и была в проде. Чтобы новый DISPLAY всё-таки дошёл до
        # процесса браузера, передаём актуальное окружение явно через параметр env=
        # (Playwright поддерживает его специально для таких случаев).
        # Было: browser = await playwright_instance.chromium.launch(headless=headless)
        browser = await playwright_instance.chromium.launch(headless=headless, env=dict(os.environ))
    except Exception:
        # Запуск не удался - сразу отдаём обратно то, что успели занять.
        if not headless:
            await _release_virtual_display()
        _browser_semaphore.release()
        raise

    released = False
    original_close = browser.close

    async def _close_and_release(*args, **kwargs):
        nonlocal released
        try:
            return await original_close(*args, **kwargs)
        finally:
            # Защита от повторного освобождения при повторном close() -
            # в коде хендлеров browser.close() иногда вызывается больше
            # одного раза на разных путях выхода (например, сторожем из
            # services/browser_watchdog.py и обычным кодом одновременно).
            if not released:
                released = True
                if not headless:
                    await _release_virtual_display()
                _browser_semaphore.release()

    browser.close = _close_and_release
    return browser


async def try_restore_session(browser, session_json_str: str):
    """
    Пытается восстановить сохранённую сессию.

    Возвращает (context, page, reused):
    reused=True, если сессия рабочая и мы уже в личном кабинете.
    Если сессия протухла - context/page уже "чистые", можно логиниться заново.
    """
    try:
        storage_state = json.loads(session_json_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            f"try_restore_session: не удалось разобрать сохранённую сессию "
            f"(будет выполнен вход заново). Ошибка: {e}"
        )
        context = await browser.new_context()
        page = await context.new_page()
        return context, page, False

    context = await browser.new_context(storage_state=storage_state)
    page = await context.new_page()

    await page.goto(TARGET_URL)
    await page.wait_for_load_state("load")
    try:
        await page.locator('[data-qa-type="navigation/username"]').wait_for(state= 'attached', timeout=5000)
    except:
        # Сессия устарела - создаём чистый контекст
        await context.close()
        context = await browser.new_context()
        page = await context.new_page()
        return context, page, False

    if page.url.startswith(TARGET_URL):
        return context, page, True

    

async def start_phone_login(page, phone):
    """Открывает страницу входа и вводит номер телефона."""
    await page.goto(TARGET_URL)
    
    phone_input = page.locator('input[type="tel"]')
    submit_button = page.locator('[automation-id="button-submit"]')
    
    await phone_input.wait_for(state="visible")
    await phone_input.press_sequentially(phone, delay=100)
    
    await submit_button.wait_for(state="visible")
    await submit_button.click()


async def submit_sms_code(page, sms_code: str) -> str:
    """
    Вводит код из смс.

    Возвращает, какая форма появилась дальше: "password" или "pin".
    """
    await page.locator('[automation-id="otp-input"]').fill(sms_code)

    password_selector = '[automation-id="password-input"]'
    pin_selector = '[automation-id="pin-code-input-0"]'

    await page.wait_for_selector(f'{password_selector}, {pin_selector}')

    if await page.locator(password_selector).count() > 0:
        return "password"
    elif await page.locator(pin_selector).count() > 0:
        return "pin"

    raise ValueError("Открыта неизвестная форма. Ожидались формы ввода пароля или пин-кода.")


async def submit_password(page, password: str):
    input_selector = '[automation-id="password-input"]'
    button_submit_selector = '[automation-id="button-submit"]'

    await page.locator(input_selector).fill(password)
    await page.locator(button_submit_selector).click()

async def wait_for_pin_form(page):
    """Ожидает появления формы ввода пин-кода на странице."""
    await page.wait_for_selector('[automation-id="pin-code-input-0"]')


async def submit_pin(page, pin_code: str):
    """
    Вводит пин-код и проверяет успешность входа.
    """
    await page.wait_for_selector('[automation-id="pin-code-input-0"]')

    for i, digit in enumerate(pin_code):
        await page.fill(f'[id="pinCode{i}"]', digit)

    button_submit_selector = '[automation-id="button-submit"]'
    await page.locator(button_submit_selector).click()

    # Биометрию пропускаем
    try:
        button_skip_selector = '[automation-id="button-skip"]'
        await page.wait_for_selector(button_skip_selector)
        await page.locator(button_skip_selector).click()
    except:
        # Рано или поздно они уберут этот шаг, поэтому просто пропускаю 
        pass

    ERROR_SELECTOR = '[automation-id="server-error"]'
    try:
        await page.wait_for_url(f"{TARGET_URL}**", timeout=15000)
    except PlaywrightTimeoutError:
        if await page.locator(ERROR_SELECTOR).count() > 0:
            error_text = (await page.locator(ERROR_SELECTOR).text_content() or "").strip()
            raise ValueError(f"Неверный пин-код{f': {error_text}' if error_text else ''}.")

        raise ValueError(
            "Не удалось подтвердить успешный вход по пин-коду: страница не "
            "перешла в личный кабинет за отведённое время, и явного "
            "сообщения об ошибке найти не удалось."
        )


async def save_session(context) -> str:
    storage_state = await context.storage_state()
    return json.dumps(storage_state, ensure_ascii=False)


def generate_api_url(session_id: str, month: date | None = None) -> str:
    """
    Формирует ссылку на гистограмму трат.
    """
    base_url = "https://www.tbank.ru/mybank/api/operations/timeline/public/legacy/v1/operations_histogram"

    now = datetime.now()
    now_ms = int(time.time() * 1000)

    if month is None:
        month = date(now.year, now.month, 1)

    start_of_month = datetime(month.year, month.month, 1)
    start_of_month_ms = int(start_of_month.timestamp() * 1000)

    if month.year == now.year and month.month == now.month:
        end_ms = now_ms
    else:
        last_day = monthrange(month.year, month.month)[1]
        end_of_month = datetime(month.year, month.month, last_day, 23, 59, 59)
        end_ms = int(end_of_month.timestamp() * 1000)

    query_params = {
        "appName": "supreme",
        "appVersion": "0.0.1",
        "origin": "web,ib5,platform",
        "sessionid": session_id,
        "period": "day",
        "start": str(start_of_month_ms),
        "end": str(end_ms),
        "timeZone": "+03:00",
        "config": "allNotInner",
        "groupBy": "category"
    }

    return f"{base_url}?{urlencode(query_params)}"


def process_spending_data(data: dict) -> dict:
    spending = {}

    intervals = (
        data
        .get("payload", {})
        .get("spending", {})
        .get("intervals", [])
    )

    for interval in intervals:
        for item in interval.get("aggregated", []):
            category_name = item.get("category", {}).get("name")
            amount = item.get("amount", {}).get("value", 0)

            if not category_name:
                continue

            spending[category_name] = spending.get(category_name, 0) + float(amount)

    return spending


# НОВОЕ: тот же ответ гистограммы содержит блок "earning" (доходы) рядом
# со "spending" (тратами) - структура идентична, но нам не нужна разбивка
# по категориям дохода, только общая сумма поступлений за период (нужна
# services/budget_forecast.py для пересчёта лимитов по общему бюджету:
# факт ЗП/поступлений сравнивается с планом).
def process_earning_data(data: dict) -> float:
    total = 0.0

    intervals = (
        data
        .get("payload", {})
        .get("earning", {})
        .get("intervals", [])
    )

    for interval in intervals:
        for item in interval.get("aggregated", []):
            amount = item.get("amount", {}).get("value", 0)
            total += float(amount)

    return total


# ИЗМЕНЕНО: раньше download_spendings сам ходил в сеть (page.goto + разбор
# JSON) и парсил только блок "spending". Эта логика вынесена в отдельный
# приватный хелпер _fetch_histogram, чтобы новая download_earnings (и
# download_spendings_and_earnings) могла переиспользовать тот же самый
# HTTP-запрос вместо повторного похода за одними и теми же данными - и
# spending, и earning приходят в ОДНОМ ответе Т-Банка.
# Публичное поведение и сигнатура download_spendings при этом не изменились.
async def _fetch_histogram(context, page, month: date | None = None) -> dict:
    """
    Достаёт sessionid из кук браузера, запрашивает гистограмму операций
    (содержит и spending, и earning) и возвращает разобранный JSON.

    Бросает RuntimeError, если sessionid не найден или Т-Банк вернул не JSON.
    """
    cookies = await context.cookies("https://www.tbank.ru")
    session_id = next((c["value"] for c in cookies if c["name"] == "psid"), None)

    if not session_id:
        raise RuntimeError("Не удалось получить sessionid из браузера.")

    api_url = generate_api_url(session_id, month)
    await page.goto(api_url)

    # Браузер оборачивает чистый JSON в тег <pre>
    json_content = await page.locator("pre").text_content()

    try:
        return json.loads(json_content)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(
            f"_fetch_histogram: не удалось разобрать ответ Т-Банка как JSON. "
            f"Ошибка: {e}. Начало ответа: {(json_content or '')[:300]!r}"
        )
        raise RuntimeError("Т-Банк вернул неожиданный ответ вместо данных.") from e


async def download_spendings(context, page, month: date | None = None) -> dict:
    """
    Запрашивает гистограмму трат и возвращает словарь {категория: сумма}.

    Необязательный параметр month - чтобы можно было
    запросить траты не только за текущий, но и за прошлый (или любой
    другой) месяц. Пробрасывается в generate_api_url.

    (ИЗМЕНЕНО: сам поход в сеть теперь внутри _fetch_histogram - см. выше;
    поведение и сигнатура функции не изменились.)
    """
    json_data = await _fetch_histogram(context, page, month)
    return process_spending_data(json_data)


# НОВОЕ: аналог download_spendings, но возвращает суммарный ДОХОД
# ('earning') за месяц вместо трат. Нужен для пересчёта лимитов по общему
# бюджету (services/budget_forecast.py), где план сравнивается не только
# по тратам, но и по фактической ЗП/поступлениям.
async def download_earnings(context, page, month: date | None = None) -> float:
    json_data = await _fetch_histogram(context, page, month)
    return process_earning_data(json_data)


# НОВОЕ: получить и траты, и доход за месяц ОДНИМ запросом к Т-Банку -
# используется в services/budget_forecast.py при пересчёте лимитов сразу
# за несколько прошедших месяцев, чтобы не ходить в сеть дважды за
# данными, которые и так приходят в одном ответе.
async def download_spendings_and_earnings(context, page, month: date | None = None):
    json_data = await _fetch_histogram(context, page, month)
    return process_spending_data(json_data), process_earning_data(json_data)


# ============================================================================
# ПРО HEADLESS И ФОРМУ "Доступ заблокирован"
# ============================================================================
# Т-Банк (как и большинство банков) детектит автоматизацию Playwright по
# совокупности признаков headless-браузера: navigator.webdriver == true,
# урезанный/отсутствующий window.chrome, пустые navigator.plugins/mimeTypes,
# нетипичное поведение Notification.permission, специфичный рендерер WebGL,
# иногда даже сетевой отпечаток TLS/JA3-хендшейка отличается у headless-
# сборки Chromium. Именно поэтому после ввода телефона вместо формы кода
# вылезает "Доступ заблокирован" - это защита антифрод-системы банка.
#
# Пытаться "подделать" headless Chromium под обычный браузер (спуфить
# navigator.webdriver, подсовывать фейковые plugins, накатывать сторонние
# stealth-патчи и т.п.) - плохая идея по трём причинам:
#   1) Это гонка вооружений: банк обновит детект - патч снова сломается,
#      результат непредсказуем, а диагностировать "внезапно перестало
#      работать" тяжело.
#   2) Речь идёт о входе в реальный банковский личный кабинет (пароль/пин) -
#      срабатывание антифрод-системы может означать не просто "страница не
#      открылась", а дополнительную проверку или блокировку самого
#      банковского аккаунта. Обход в лоб рискованнее, чем не открыть страницу.
#   3) Автоматизированный вход в личный кабинет банка почти наверняка
#      противоречит пользовательскому соглашению Т-Банка (такие соглашения
#      обычно прямо запрещают автоматизированный/роботизированный доступ) -
#      это юридический риск, который спуфингом фингерпринта не снять.
#
# Рабочий и НЕ основанный на обмане способ - запускать настоящий headful
# Chromium (headless=False), но без физического монитора - через виртуальный
# X-дисплей (Xvfb). Для сайта это будет обычный браузер, а не подделка под
# него, поэтому детект headless просто не сработает - мы ведь и правда не
# headless.
#
# Как включить на Linux-сервере (два варианта, можно выбрать любой):
#   А) Без изменений в коде Python - через обёртку при запуске:
#        1) sudo apt-get install -y xvfb
#        2) Запускать бота так: xvfb-run -a python main.py
#        3) В .env/окружении выставить BROWSER_HEADLESS=false
#           (см. DEFAULT_HEADLESS выше) - тогда launch_browser передаст в
#           Playwright headless=False, а xvfb-run подставит виртуальный экран.
#   Б) Через пакет pyvirtualdisplay прямо из Python - см. функцию
#      _maybe_start_virtual_display() в main.py (управляется переменной
#      окружения USE_VIRTUAL_DISPLAY=true), тогда отдельный xvfb-run не
#      нужен - Xvfb поднимается изнутри процесса бота.
#
# НАСКОЛЬКО КРИТИЧНО, ЧТО headless ВСЕГДА True:
# Именно для tbank.ru - критично: без headful-режима (через Xvfb) вход не
# пройдёт дальше ввода номера телефона, форма СМС даже не появится, поэтому
# бот в текущем виде на "чистом" headless=True для реальной авторизации
# нежизнеспособен. Headless - не единственный сигнал детекта (важны ещё
# User-Agent, локаль/таймзона, viewport, отсутствие обычной для пользователя
# истории/cookies и т.д.), но самый заметный, и Xvfb снимает его полностью,
# без обмана сайта.
# ============================================================================
