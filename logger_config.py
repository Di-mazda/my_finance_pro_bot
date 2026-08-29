"""

Централизованная настройка логирования ошибок.

Теперь все ошибки пишутся в файл bot_errors.log (с ротацией, чтобы файл не
рос бесконечно). Использование в любом другом модуле проекта:

    from logger_config import logger

    logger.info("что-то произошло")
    logger.warning("что-то подозрительное")
    logger.error("явная ошибка")
    logger.exception("ошибка с traceback")   # вызывать только внутри except

Если в будущем понадобится хранить ошибки в БД вместо/вместе с файлом -
можно добавить свой logging.Handler, который в методе emit() будет писать
запись в таблицу (например error_logs(id, created_at, level, logger_name,
message, traceback)) через aiosqlite, и добавить его в logger.addHandler(...)
ниже. Сигнатура использования (logger.error(...) / logger.exception(...))
в остальном коде при этом не изменится.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

LOG_FILE = "bot_errors.log"


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("finance_bot")
    logger.setLevel(logging.INFO)

    # Защита от повторной настройки при повторном импорте модуля разными
    # частями приложения (`from logger_config import logger` в нескольких
    # файлах) - без этой проверки хендлеры задублировались бы и каждая
    # ошибка писалась бы в файл несколько раз.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Ротация: максимум 5 файлов по 5 МБ каждый - логи не накапливаются
    # бесконечно и не забивают диск на сервере.
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Критичные ошибки (logger.critical) дополнительно дублируем в stderr,
    # чтобы их было видно сразу, если бот запущен интерактивно или через
    # systemd/docker (journalctl/docker logs всё равно читают stderr).
    # Обычные info/warning/error, как и просили, в консоль НЕ идут - только в файл.
    # stderr_handler = logging.StreamHandler(sys.stderr)
    # stderr_handler.setLevel(logging.CRITICAL)
    # stderr_handler.setFormatter(formatter)
    # logger.addHandler(stderr_handler)

    # Новое: дублируем INFO и выше в stdout — чтобы Railway их показывал
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    return logger


logger = _setup_logger()
