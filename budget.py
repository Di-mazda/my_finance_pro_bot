from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def calculate_category_budget(
    limit: float,
    fact: float,
    passed_days: float,
    days_in_month: int
):
    """
    Рассчитывает состояние бюджета одной категории.
    """

    # Остаток первоначального лимита
    balance = limit - fact

    if passed_days <= 0:
        return {
            "limit": limit,
            "fact": fact,
            "balance": balance,
            "speed": 0,
            "forecast": 0,
            "need": 0,
            "allowed_speed": limit / days_in_month
        }

    # Фактическая скорость расходов
    speed = fact / passed_days

    # Прогноз расходов на весь месяц
    forecast = speed * days_in_month

    # Сколько дополнительно потребуется,
    # если продолжить тратить с текущей скоростью
    need = max(0, forecast - limit)

    # Допустимая средняя скорость,
    # при которой лимит будет полностью выбран к концу месяца
    allowed_speed = limit / days_in_month

    return {
        "limit": limit,
        "fact": fact,
        "balance": balance,
        "speed": speed,
        "forecast": forecast,
        "need": need,
        "allowed_speed": allowed_speed
    }


def calculate_budget(limits, spending, passed_days=None, days_in_month=None):
    """
    Рассчитывает бюджет по всем категориям.
    """

    if passed_days is None:
        now = datetime.now(MOSCOW_TZ)
        passed_days = now.day - 1 + now.hour / 24
        days_in_month = monthrange(now.year, now.month)[1]

    result = {}

    for category_id, category, limit in limits:

        fact = spending.get(category, 0)

        result[category] = calculate_category_budget(
            limit=limit,
            fact=fact,
            passed_days=passed_days,
            days_in_month=days_in_month
        )

    return result
