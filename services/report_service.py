"""Сборка текста отчёта по бюджету. Чистые функции, без Telegram-специфики."""
from calendar import monthrange
from budget import MOSCOW_TZ, calculate_budget
from datetime import date, datetime, timedelta


def _money(value) -> str:
    return f"{value:,.2f}".replace(",", " ")


def build_budget_report_text(month, limits, spendings: dict):
    """
    Возвращает готовый HTML-текст отчёта, либо None, если трат за период нет.
    """
    if not spendings:
        return None

    # НЕРАСПРЕДЕЛЕННЫЕ КАТЕГОРИИ
    limit_categories = {row[1] for row in limits}
    unallocated = {
        category: amount
        for category, amount in spendings.items()
        if category not in limit_categories
    }

    is_current_month = month == date.today().replace(day=1)

    if is_current_month:
        result = calculate_budget(limits, spendings)
    else:
        now = datetime.now(MOSCOW_TZ)
        days_in_month = monthrange(now.year, now.month)[1]
        result = calculate_budget(limits, spendings, passed_days=days_in_month, days_in_month=days_in_month)

    report = [f"💰 <b>БЮДЖЕТ {month.strftime('%m.%Y')}</b>", ""]

    for category, data in result.items():
        limit = data["limit"]
        fact = data["fact"]
        balance = data["balance"]
        speed = data["speed"]
        forecast = data["forecast"]
        need = data["need"]

        # Индикатор состояния категории
        if balance < 0:
            icon = "🔴"
        elif need > 0:
            icon = "🟡"
        else:
            icon = "🟢"

        report.append(f"{icon} <b>{category}</b>")
        report.append(f"Лимит:       {_money(limit)} ₽")
        report.append(f"Факт:        {_money(fact)} ₽")
        report.append(f"Остаток:     {_money(balance)} ₽")

        if speed > 0:
            report.append("----------------------------------")
            report.append(f"Скорость:    {_money(speed)} ₽/день")
            if is_current_month:
                report.append(f"Прогноз:     {_money(forecast)} ₽")
                report.append(f"Потребность: {_money(need)} ₽")
            report.append("----------------------------------")

        report.append("")

    # ИТОГИ
    total_limit = sum(x["limit"] for x in result.values())
    total_fact = sum(x["fact"] for x in result.values())
    total_balance = sum(x["balance"] for x in result.values())
    total_forecast = sum(x["forecast"] for x in result.values())
    total_need = sum(x["need"] for x in result.values())

    report.append("━━━━━━━━━━━━━━━━━━━━")
    report.append("<b>ИТОГО</b>")
    report.append(f"Лимиты:      {_money(total_limit)} ₽")
    report.append(f"Факт:        {_money(total_fact)} ₽")
    report.append(f"Остаток:     {_money(total_balance)} ₽")
    if is_current_month:
        report.append(f"Прогноз:     {_money(total_forecast)} ₽")
        report.append(f"Потребность: {_money(total_need)} ₽")

    # НЕРАСПРЕДЕЛЕННЫЕ КАТЕГОРИИ
    if unallocated:
        report.append("")
        report.append("━━━━━━━━━━━━━━━━━━━━")
        report.append("")
        report.append("⚠️ <b>НЕРАСПРЕДЕЛЕННЫЕ КАТЕГОРИИ Т-БАНКА</b>")
        report.append("Эти категории отсутствуют среди твоих лимитов:")
        report.append("")

        for category, amount in unallocated.items():
            report.append(f"• {category}: <b>{_money(amount)} ₽</b>")

        report.append("")
        report.append(
            "💡 Если категория распределена неправильно, "
            "измени её в Т-Банке. При следующем отчёте "
            "она исчезнет из этого списка."
        )

    return "\n".join(report)

# Вычисляет первое число месяца, предшествующего переданной дате.
def _first_day_of_previous_month(today: date) -> date:
    first_day_of_this_month = today.replace(day=1)
    last_day_of_prev_month = first_day_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.replace(day=1)