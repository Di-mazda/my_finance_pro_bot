"""
services/budget_forecast.py
============================

НОВЫЙ МОДУЛЬ. Пересчёт месячных лимитов трат по общему бюджету (план ЗП +
план трат по категориям на несколько месяцев вперёд), с размазыванием
отклонения факта от плана по всем оставшимся месяцам и защитой обязательных
категорий от урезания.

Это НЕ замена services/report_service.py и budget.py - те считают, как
идёт расход лимита ВНУТРИ уже текущего месяца (скорость трат, прогноз к
концу месяца). Этот модуль решает другую задачу: КАКИМ должен быть лимит
на месяц, чтобы в будущих "тяжёлых" месяцах (где план трат больше ЗП)
хватило накопленного запаса. Оба расчёта независимы и работают вместе:
budget_forecast пересчитывает limits.spending_limit в начале месяца,
report_service/budget показывают прогресс внутри месяца по уже
установленному лимиту.

Идея алгоритма (подробно обсуждалась и тестировалась отдельно от кода
бота, здесь - адаптированная и интегрированная версия):

1. Общий резерв (план и факт), без деления категорий на "особые":
     Резерв(i) = ЗП(i) - Сумма_всех_категорий(i)
   Считаем кумулятивную сумму - план и факт резерва на конец каждого месяца.

2. Отклонение = КумРезерв_факт(последний известный месяц)
              - КумРезерв_план(последний известный месяц)

3. Отклонение размазывается на ВСЕ оставшиеся месяцы окна планирования
   пропорционально их доле в оставшемся плане (а не скидывается целиком на
   ближайший месяц):
     вес(i) = План(i) / Сумма(План по остатку окна)
     НовыйБюджет(i) = План(i) + Отклонение * вес(i)

4. Категории из protect_categories (жильё, связь, транспорт, инвестиции -
   то, что реально нельзя урезать день в день) всегда равны своему плану.
   Вся коррекция ложится на остальные категории пропорционально их
   изначальным долям в плане на этот месяц.

НОВОЕ (Mini App "План на год"): помимо protect_categories (категория
защищена ВСЕГДА, во всех месяцах), теперь есть более точечный механизм -
category_plan.no_recalc (см. database.get_no_recalc_categories_for_month).
Пользователь может в таблице плана запретить пересчёт лимита конкретной
категории ИМЕННО на конкретный месяц - тогда лимит на этот месяц для этой
категории будет равен плану, без перераспределения, но в остальные месяцы
категория продолжает участвовать в пересчёте как обычно. См. параметр
locked_categories у recommend_month ниже.

Осознанно НЕ храним факт трат/доходов в БД - см. recalc_month_limits ниже:
данные всегда скачиваются заново из Т-Банка (spending и earning) на момент
пересчёта, чтобы учитывать пометки "не учитывать" в банке без риска
рассинхронизации между сохранённой копией и текущим состоянием банка.
"""

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import database
from services import tbank_client
from logger_config import logger


MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


@dataclass
class CategoryPlan:
    name: str
    plan: List[float]  # план на N месяцев окна планирования (см. get_plan_horizon)


def total_plan_per_month(cats: List[CategoryPlan]) -> List[float]:
    n = len(cats[0].plan) if cats else 0
    totals = [0.0] * n
    for c in cats:
        for i in range(n):
            totals[i] += c.plan[i]
    return totals


def check_plan_feasibility(categories: List[CategoryPlan], salary, initial_balance: float = 0.0) -> List[tuple]:
    """
    Проверяет, не уходит ли кумулятивный резерв в минус при точном
    следовании плану, начиная с initial_balance накоплений (по умолчанию
    0 - как и раньше). Возвращает список (индекс_месяца, кумулятивный_резерв)
    для месяцев, где резерв < 0. Полезно показать пользователю сразу после
    ввода плана - если список непустой, план сам по себе не сходится
    (нужен стартовый запас или пересмотр плана на эти месяцы), и пересчёт
    лимитов этого не решит.
    """
    TP = total_plan_per_month(categories)
    n = len(TP)
    salary_list = salary if isinstance(salary, list) else [salary] * n

    balance, problems = initial_balance, []
    for i in range(n):
        balance += salary_list[i] - TP[i]
        if balance < 0:
            problems.append((i, round(balance)))
    return problems


def recommend_month(
    categories: List[CategoryPlan],
    actuals: Dict[str, List[float]],
    month_idx: int,
    salary,
    protect_categories: Optional[List[str]] = None,
    actual_salary: Optional[List[float]] = None,
    locked_categories: Optional[List[str]] = None,
) -> dict:
    """
    Считает рекомендованные лимиты на месяц month_idx (0 = первый месяц
    окна планирования) для всех категорий, на основе общего бюджета и
    факта за месяцы [0..month_idx-1].

    actuals            - {категория: [факт_месяц0, факт_месяц1, ...]}.
                          Не нужно указывать все категории/месяцы - то,
                          чего нет, считается выполненным строго по плану.
    salary             - число (постоянная ЗП) или список по числу месяцев
                          окна планирования - ПЛАНОВАЯ ЗП.
    actual_salary      - список ФАКТИЧЕСКОЙ ЗП/поступлений за уже
                          прошедшие месяцы [0..month_idx-1] (из Т-Банка,
                          блок earning - см. tbank_client.download_earnings).
                          Если не передано - используется плановая ЗП и
                          для прошлых месяцев тоже (старое поведение).
    protect_categories - список НАЗВАНИЙ категорий, которые НЕ участвуют
                          в перераспределении и всегда равны своему
                          исходному плану на месяц, В ЛЮБОМ месяце окна
                          (см. categories.is_protected).
    locked_categories   - НОВОЕ: список НАЗВАНИЙ категорий, для которых
                          пересчёт запрещён ИМЕННО на месяц month_idx (см.
                          category_plan.no_recalc). В отличие от
                          protect_categories, действует только для этого
                          конкретного вызова (этого месяца), не для всего
                          окна планирования.
    """
    protect_categories = set(protect_categories or [])
    locked_categories = set(locked_categories or [])

    TP = total_plan_per_month(categories)
    n = len(TP)
    salary_list = salary if isinstance(salary, list) else [salary] * n
    RC_plan = [salary_list[i] - TP[i] for i in range(n)]

    TA = []
    for i in range(month_idx):
        tot = 0.0
        for c in categories:
            a = actuals.get(c.name)
            tot += a[i] if a and i < len(a) else c.plan[i]
        TA.append(tot)

    # Используем фактическую ЗП там, где она известна, иначе - плановую
    # (тот же принцип, что и для трат по категориям выше).
    if actual_salary is not None:
        salary_actual_list = [
            actual_salary[i] if i < len(actual_salary) else salary_list[i]
            for i in range(month_idx)
        ]
    else:
        salary_actual_list = salary_list[:month_idx]

    RC_actual = [salary_actual_list[i] - TA[i] for i in range(month_idx)]

    reserve_plan_balance = sum(RC_plan[:month_idx])
    reserve_fact_balance = sum(RC_actual)
    deviation_total = reserve_fact_balance - reserve_plan_balance

    remaining_idx = list(range(month_idx, n))
    remaining_TP_sum = sum(TP[i] for i in remaining_idx)
    weights = {
        i: (TP[i] / remaining_TP_sum if remaining_TP_sum > 0 else 1 / len(remaining_idx))
        for i in remaining_idx
    }
    adjustments = {i: deviation_total * weights[i] for i in remaining_idx}
    new_total_limit = {i: TP[i] + adjustments[i] for i in remaining_idx}

    m = month_idx

    # НОВОЕ: locked_categories действуют так же, как protect_categories, но
    # только для расчёта этого конкретного месяца m - объединяем обе группы
    # в единый набор "защищённых в этом месяце" категорий.
    protected_this_month = protect_categories | locked_categories

    scale_cats = [c for c in categories if c.name not in protected_this_month]
    protected_cats = [c for c in categories if c.name in protected_this_month]
    protected_sum = sum(c.plan[m] for c in protected_cats)
    scalable_plan_sum = sum(c.plan[m] for c in scale_cats)
    target_for_scalable = new_total_limit[m] - protected_sum
    scale_factor = target_for_scalable / scalable_plan_sum if scalable_plan_sum > 0 else 1.0

    warnings = []
    if scale_factor < 0:
        warnings.append(
            "Даже обнулив все незащищённые категории, бюджет месяца не "
            "сходится - нужно брать деньги из резерва или пересматривать план."
        )
        scale_factor = 0.0

    category_limits = {}
    for c in categories:
        if c.name in protected_this_month:
            category_limits[c.name] = round(c.plan[m])
        else:
            category_limits[c.name] = round(c.plan[m] * scale_factor)

    return {
        "month_idx": m,
        "deviation_total": round(deviation_total),
        "reserve_fact_balance": round(reserve_fact_balance),
        "reserve_plan_balance": round(reserve_plan_balance),
        "scale_factor": round(scale_factor, 4),
        "category_limits": category_limits,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------
# НОВОЕ: оркестрация - связывает чистый алгоритм выше с БД (план) и
# Т-Банком (факт), и сохраняет результат обратно в limits.
# ---------------------------------------------------------------------

async def _load_category_plans(phone: str, months: List[date]) -> List[CategoryPlan]:
    """
    Собирает список CategoryPlan (для recommend_month), выровненный по
    списку months. Категории без плана на какой-то месяц получают 0 за
    этот месяц. Категории, для которых вообще нет ни одной строки в
    category_plan, тоже возвращаются - с планом [0, 0, ..., 0] - чтобы
    отличить "план = 0" от "план не заведён" на уровне вызывающего кода
    (recalc_month_limits).
    """
    rows = await database.get_category_plan_rows(phone)
    categories_meta = await database.get_categories_full(phone)

    plan_by_cat: Dict[int, Dict[str, int]] = {}
    for category_id, month_value, amount in rows:
        month_key = month_value.isoformat() if hasattr(month_value, "isoformat") else str(month_value)
        plan_by_cat.setdefault(category_id, {})[month_key] = amount

    month_keys = [m.isoformat() for m in months]
    result = []
    for meta in categories_meta:
        values = plan_by_cat.get(meta["id"], {})
        result.append(CategoryPlan(meta["name"], [values.get(mk, 0) for mk in month_keys]))
    return result


async def recalc_month_limits(phone: str, target_month: date, context, page) -> Optional[dict]:
    """
    Пытается автоматически пересчитать лимиты на target_month и сохраняет
    их в limits (см. database.save_recalculated_limits - не трогает
    строки, которые пользователь уже поправил вручную за этот месяц).

    ВАЖНО: пересчёт затрагивает ТОЛЬКО таблицу limits (текущие лимиты,
    которые видны в "Текущие лимиты" и в отчётах) - сам план (category_plan/
    salary_plan) этой функцией никогда не меняется и не должен меняться.
    Если пользователь хочет, чтобы конкретная категория в конкретном месяце
    не трогалась пересчётом вообще - он выставляет в таблице плана флаг
    "не пересчитывать" для этой ячейки (category_plan.no_recalc, см.
    database.get_no_recalc_categories_for_month), а не правит limits напрямую.

    Возвращает результат recommend_month(), либо None, если пересчитать
    нечем (план не заведён) или не удалось (например, не получилось
    скачать факт за один из прошлых месяцев) - в обоих случаях вызывающий
    код (handlers/reports.py) должен вести себя так, как будто этой
    функции не существует (старое поведение: показываем лимиты как есть).

    context/page - уже авторизованные Playwright context/page (тот же
    браузер, что используется для скачивания отчёта в
    handlers/reports.py:download_and_send_report) - отдельный вход в
    Т-Банк здесь не требуется.
    """
    horizon_months = await database.get_plan_horizon(phone)
    if target_month not in horizon_months:
        # плана на этот месяц нет вообще (пользователь не заводил план
        # так далеко вперёд, либо не заводил план вовсе) - не наша забота
        return None

    category_plans = await _load_category_plans(phone, horizon_months)
    if not category_plans or all(sum(c.plan) == 0 for c in category_plans):
        # план трат по категориям не заведён ни для одной категории
        return None

    salary_plan = await database.get_salary_plan(phone, horizon_months)
    if all(s == 0 for s in salary_plan):
        # план ЗП не заведён
        return None

    month_idx = horizon_months.index(target_month)

    actuals: Dict[str, List[float]] = {c.name: [] for c in category_plans}
    actual_salary: List[float] = []

    for i in range(month_idx):
        month = horizon_months[i]
        try:
            month_spendings, month_earning = await tbank_client.download_spendings_and_earnings(
                context, page, month
            )
        except Exception as e:
            # Без полной истории факта за все прошедшие месяцы окна
            # пересчёт ненадёжен (одно "дырявое" место сломает весь
            # резерв) - лучше откатиться к старому поведению, чем
            # посчитать лимиты неправильно.
            logger.exception(
                f"recalc_month_limits: не удалось скачать факт за {month} "
                f"(phone={phone}), пересчёт отменён: {e}"
            )
            return None

        for c in category_plans:
            actuals[c.name].append(month_spendings.get(c.name, 0.0))
        actual_salary.append(month_earning)

    categories_meta = await database.get_categories_full(phone)
    protect_categories = [m["name"] for m in categories_meta if m["is_protected"]]

    # НОВОЕ: категории, для которых пользователь в таблице плана явно
    # запретил пересчёт лимита именно на target_month (см.
    # database.get_no_recalc_categories_for_month) - лимит останется равным
    # плану на этот месяц, независимо от общего пересчёта по бюджету.
    locked_categories = await database.get_no_recalc_categories_for_month(phone, target_month)

    result = recommend_month(
        category_plans, actuals, month_idx, salary_plan,
        protect_categories=protect_categories,
        actual_salary=actual_salary,
        locked_categories=locked_categories,
    )

    name_to_id = {m["name"]: m["id"] for m in categories_meta}
    limits_to_save = {
        name_to_id[name]: limit
        for name, limit in result["category_limits"].items()
        if name in name_to_id
    }
    await database.save_recalculated_limits(phone, target_month, limits_to_save)

    logger.info(
        f"recalc_month_limits: лимиты на {target_month} для phone={phone} "
        f"пересчитаны (отклонение={result['deviation_total']})."
    )

    return result


def format_recalc_note(result: dict) -> str:
    """
    Короткое сообщение для пользователя о том, что лимиты были
    пересчитаны автоматически - используется в handlers/reports.py перед
    основным текстом отчёта.
    """
    deviation = result["deviation_total"]
    if deviation > 0:
        deviation_text = f"вы накопили запас +{deviation} ₽ сверх плана"
    elif deviation < 0:
        deviation_text = f"перерасход {abs(deviation)} ₽ относительно плана"
    else:
        deviation_text = "точно по плану"

    warnings = result.get("warnings") or []
    warning_text = ""
    if warnings:
        warning_text = "\n⚠️ " + " ".join(warnings)

    return (
        "🔄 <i>Лимиты на этот месяц автоматически пересчитаны с учётом "
        f"фактических трат и доходов прошлых месяцев ({deviation_text}).</i>"
        f"{warning_text}"
    )
