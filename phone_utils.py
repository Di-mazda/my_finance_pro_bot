import re


def normalize_phone(raw: str) -> str | None:
    """
    Приводит номер телефона к единому формату +7XXXXXXXXXX.
    Понимает варианты: +79991234567, 89991234567, 79991234567, 9991234567,
    а также любые пробелы/скобки/тире внутри.
    Возвращает None, если номер не похож на российский мобильный.
    """
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        # Ввели без кода страны, например "9991234567"
        digits = "7" + digits
    else:
        return None

    return f"+{digits}"