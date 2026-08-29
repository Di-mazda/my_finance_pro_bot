"""
budget_limits.py
=================

v2: пересчёт месячных лимитов трат ПО ОБЩЕМУ БЮДЖЕТУ (а не по каждой
категории изолированно), с размазыванием отклонения по всем оставшимся
месяцам года и защитой минимального процента на инвестиции.

Идея
----
1. Общий резерв (а не отдельные "конверты" по категориям):

     Резерв(i) = ЗП(i) − Инвестиции(i) − Сумма_гибких_категорий(i)

   Считаем эту величину и по ПЛАНУ, и по ФАКТУ (для уже прошедших месяцев),
   и берём кумулятивную сумму — это и есть план/факт резерва на конец
   каждого месяца.

2. Отклонение = КумРезерв_факт(последний известный месяц)
              − КумРезерв_план(последний известный месяц)

   Положительное — по факту накопили больше, чем должны были по плану
   (можно немного ослабить лимиты). Отрицательное — накопили меньше
   (нужно ужаться).

3. Отклонение НЕ скидывается целиком на ближайший месяц, а размазывается
   на все оставшиеся месяцы года пропорционально их доле в оставшемся
   плане гибких категорий:

     вес(i)          = План_гибких(i) / Сумма(План_гибких по остатку года)
     Корректировка(i) = Отклонение × вес(i)
     НовыйБюджет(i)   = План_гибких(i) + Корректировка(i)

   Сумма всех Корректировка(i) по оставшимся месяцам точно равна
   Отклонению — коррекция происходит постепенно, с учётом того, что
   в "тяжёлые" по плану месяцы и так много трат.

4. Инвестиции защищены минимальным порогом и не участвуют в
   перераспределении:

     Инвестиции(i) = max(План_инвестиций(i), invest_floor_pct × ЗП(i))

5. Общий новый бюджет месяца раскладывается обратно по категориям
   пропорционально их изначальным долям в плане на этот месяц:

     scale            = НовыйБюджет(месяц) / План_гибких(месяц)
     Лимит_категории  = План_категории(месяц) × scale

   Опционально можно "защитить" целые группы категорий (например,
   "Обязательные" — жильё, связь, транспорт) от урезания — тогда
   вся коррекция ложится только на "Переменные"/"Жизнь".

check_plan_feasibility() — отдельная проверка: сходится ли план сам
с собой, если считать, что тратят строго по плану, начиная с нуля
накоплений в январе. Если кумулятивный резерв уходит в минус —
это структурная проблема плана, которую пересчёт лимитов не решает
(нужен либо стартовый запас, либо пересмотр самого плана на эти месяцы).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

MONTHS = 12


@dataclass
class CategoryPlan:
    name: str
    group: str                 # просто для твоей навигации по коду, в расчётах не участвует
    plan: List[float]          # план на 12 месяцев (индекс 0 = январь)
    is_investment: bool = False
    is_buffer: bool = False    # старая категория "Прочее (подушка)" — больше не используется
                                # как рычаг, оставлена только для справки/отображения


def total_plan_per_month(cats: List[CategoryPlan]) -> List[float]:
    totals = [0.0] * MONTHS
    for c in cats:
        for i in range(MONTHS):
            totals[i] += c.plan[i]
    return totals


def check_plan_feasibility(categories: List[CategoryPlan], salary, invest_floor_pct: float = 0.10):
    """
    Проверяет, не уходит ли кумулятивный резерв в минус при точном
    следовании плану, начиная с нуля накоплений. Возвращает список
    (индекс_месяца, кумулятивный_резерв) для месяцев, где резерв < 0.
    """
    invest_cat = next(c for c in categories if c.is_investment)
    flex_cats = [c for c in categories if not c.is_investment and not c.is_buffer]
    salary_list = salary if isinstance(salary, list) else [salary] * MONTHS
    invest_eff = [max(invest_cat.plan[i], invest_floor_pct * salary_list[i]) for i in range(MONTHS)]
    TP = total_plan_per_month(flex_cats)

    balance, problems = 0.0, []
    for i in range(MONTHS):
        balance += salary_list[i] - invest_eff[i] - TP[i]
        if balance < 0:
            problems.append((i, round(balance)))
    return problems


def recommend_month(
    categories: List[CategoryPlan],
    actuals: Dict[str, List[float]],
    month_idx: int,
    salary,
    invest_floor_pct: float = 0.10,
    protect_categories: Optional[List[str]] = None,
) -> dict:
    """
    Считает рекомендованные лимиты на месяц month_idx (0 = январь) для всех
    категорий, на основе общего бюджета и факта за месяцы [0..month_idx-1].

    actuals         — {категория: [факт_январь, факт_февраль, ...]}.
                       Не нужно указывать все категории/месяцы — то, чего
                       нет, считается выполненным строго по плану.
    salary          — число (постоянная ЗП) или список из 12 чисел.
    invest_floor_pct   — минимальная доля ЗП, обязательная к инвестированию.
    protect_categories — список НАЗВАНИЙ категорий (например
                          ["Жильё (коммуналка / аренда / ипотека)",
                           "Связь и подписки", "Транспорт"]), которые
                          НЕ участвуют в перераспределении и всегда равны
                          своему исходному плану на месяц.
    """
    protect_categories = set(protect_categories or [])
    invest_cat = next(c for c in categories if c.is_investment)
    flex_cats = [c for c in categories if not c.is_investment and not c.is_buffer]

    salary_list = salary if isinstance(salary, list) else [salary] * MONTHS
    invest_eff = [max(invest_cat.plan[i], invest_floor_pct * salary_list[i]) for i in range(MONTHS)]

    TP = total_plan_per_month(flex_cats)
    RC_plan = [salary_list[i] - invest_eff[i] - TP[i] for i in range(MONTHS)]

    TA, invest_actual_hist = [], []
    for i in range(month_idx):
        tot = 0.0
        for c in flex_cats:
            a = actuals.get(c.name)
            tot += a[i] if a and i < len(a) else c.plan[i]
        TA.append(tot)
        inv_a = actuals.get(invest_cat.name)
        invest_actual_hist.append(inv_a[i] if inv_a and i < len(inv_a) else invest_eff[i])

    RC_actual = [salary_list[i] - invest_actual_hist[i] - TA[i] for i in range(month_idx)]

    reserve_plan_balance = sum(RC_plan[:month_idx])
    reserve_fact_balance = sum(RC_actual)
    deviation_total = reserve_fact_balance - reserve_plan_balance

    remaining_idx = list(range(month_idx, MONTHS))
    remaining_TP_sum = sum(TP[i] for i in remaining_idx)
    weights = {
        i: (TP[i] / remaining_TP_sum if remaining_TP_sum > 0 else 1 / len(remaining_idx))
        for i in remaining_idx
    }
    adjustments = {i: deviation_total * weights[i] for i in remaining_idx}
    new_total_limit = {i: TP[i] + adjustments[i] for i in remaining_idx}

    m = month_idx
    scale_cats = [c for c in flex_cats if c.name not in protect_categories]
    protected_cats = [c for c in flex_cats if c.name in protect_categories]
    protected_sum = sum(c.plan[m] for c in protected_cats)
    scalable_plan_sum = sum(c.plan[m] for c in scale_cats)
    target_for_scalable = new_total_limit[m] - protected_sum
    scale_factor = target_for_scalable / scalable_plan_sum if scalable_plan_sum > 0 else 1.0

    warnings = []
    if scale_factor < 0:
        warnings.append(
            "Даже обнулив все гибкие категории, бюджет месяца не сходится — "
            "нужно брать деньги из резерва/инвестиций или пересматривать план."
        )
        scale_factor = 0.0

    category_limits = {}
    for c in flex_cats:
        if c.name in protect_categories:
            category_limits[c.name] = round(c.plan[m])
        else:
            category_limits[c.name] = round(c.plan[m] * scale_factor)
    category_limits[invest_cat.name] = round(invest_eff[m])

    return {
        "month_idx": m,
        "deviation_total": round(deviation_total),
        "reserve_fact_balance": round(reserve_fact_balance),
        "reserve_plan_balance": round(reserve_plan_balance),
        "scale_factor": round(scale_factor, 4),
        "category_limits": category_limits,
        "adjustments_all_remaining_months": {i: round(v) for i, v in adjustments.items()},
        "warnings": warnings,
    }


# ---------------------------------------------------------------------
# Годовой план 2026 — из таблицы пользователя. Каждый список из 12 чисел:
# [Янв, Фев, Мар, Апр, Май, Июн, Июл, Авг, Сен, Окт, Ноя, Дек]
# ---------------------------------------------------------------------

SALARY = 87000.0

PLAN_2026: List[CategoryPlan] = [
    CategoryPlan("Жильё (коммуналка / аренда / ипотека)", "Обязательные",
                 [1824, 5371, 6378, 6198, 5324, 4710, 5300, 5100, 5100, 5100, 7331, 4105]),
    CategoryPlan("Связь и подписки", "Обязательные",
                 [859, 859, 4600, 859, 859, 859, 859, 859, 859, 859, 859, 859]),
    CategoryPlan("Транспорт", "Обязательные",
                 [4600, 4600, 6600, 4600, 14842, 5090, 9600, 4600, 4600, 6600, 4600, 4600]),
    CategoryPlan("Продукты", "Переменные",
                 [28000, 28000, 27530, 27350, 28000, 24808, 28200, 25000, 25000, 25000, 25000, 25000]),
    CategoryPlan("Кафе, рестораны, пекарни", "Переменные",
                 [5000, 5000, 5000, 5000, 5000, 7647, 2400, 5000, 5000, 5000, 5000, 5000]),
    CategoryPlan("Одежда, красота и уход", "Переменные",
                 [10000, 10000, 10000, 10000, 14000, 8100, 11600, 10000, 10000, 14000, 10000, 10000]),
    CategoryPlan("Покупки (техника, дом)", "Переменные",
                 [2000, 2000, 2000, 2000, 2000, 5414, 0, 0, 2000, 2000, 2000, 2000]),
    CategoryPlan("Развлечения", "Жизнь",
                 [2000, 2000, 2000, 2000, 2000, 1510, 2000, 2000, 2000, 2000, 2000, 2000]),
    CategoryPlan("Путешествия", "Жизнь",
                 [3000, 3000, 3000, 3000, 3000, 0, 6000, 3000, 3000, 3000, 3000, 3000]),
    CategoryPlan("Подарки", "Жизнь",
                 [1500, 1500, 1500, 1500, 1500, 7989, 0, 0, 0, 0, 1500, 1500]),
    CategoryPlan("Здоровье", "Жизнь",
                 [2500, 2500, 2500, 2500, 2500, 1266, 4000, 2500, 2500, 2500, 2500, 2500]),
    CategoryPlan("Саморазвитие", "Жизнь",
                 [1000, 1000, 1000, 1000, 1000, 754, 1000, 1000, 1000, 1000, 1000, 1000]),
    CategoryPlan("Вредные привычки", "Жизнь",
                 [10000, 10000, 10000, 10000, 10000, 5341, 15000, 10000, 10000, 10000, 10000, 10000]),
    CategoryPlan("Максавит", "Личное/прочее", [0] * 12),
    CategoryPlan("Наличные", "Личное/прочее", [0] * 12),
    CategoryPlan("Прочее (подушка безопасности)", "Личное/прочее",
                 [4717.28, 1169.85, -5107.66, 992.91, -13025.00, 3512.00,
                  -8959.00, 7941.00, 5941.00, -59.00, 2209.77, 5436.29], is_buffer=True),
    CategoryPlan("На инвестиции", "Личное/прочее", [10000] * 12, is_investment=True),
]


if __name__ == "__main__":
    # 0) Проверяем, сходится ли сам план (без единого рубля факта).
    problems = check_plan_feasibility(PLAN_2026, SALARY)
    if problems:
        print("⚠ План структурно уходит в минус (при точном исполнении, с нуля накоплений):")
        for month_idx, balance in problems:
            month_name = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"][month_idx]
            print(f"    {month_name}: накопленный резерв {balance} ₽")
        print()

    # Пример использования: узнаём лимиты на ФЕВРАЛЬ (month_idx = 1),
    # зная факт трат за январь (month_idx 0). Сценарий: крупный перерасход
    # по Транспорту (ремонт машины) — 14600 вместо плановых 4600.
    actual_january = {
        "Жильё (коммуналка / аренда / ипотека)": [1824],
        "Связь и подписки": [859],
        "Транспорт": [14600],
        "Продукты": [28000],
        "Кафе, рестораны, пекарни": [5000],
        "Одежда, красота и уход": [10000],
        "Покупки (техника, дом)": [2000],
        "Развлечения": [2000],
        "Путешествия": [3000],
        "Подарки": [1500],
        "Здоровье": [2500],
        "Саморазвитие": [1000],
        "Вредные привычки": [10000],
        "На инвестиции": [10000],
    }

    # Без защиты — коррекция размазывается по ВСЕМ гибким категориям,
    # включая жильё и транспорт, которые в реальности урезать нельзя.
    result = recommend_month(PLAN_2026, actual_january, month_idx=1, salary=SALARY)

    # С защитой — перечисленные категории всегда равны своему плану,
    # вся коррекция ложится на остальные (гибкие) категории.
    PROTECTED = [
        "Жильё (коммуналка / аренда / ипотека)",
        "Связь и подписки",
        "Транспорт",
    ]
    result_protected = recommend_month(
        PLAN_2026, actual_january, month_idx=1, salary=SALARY,
        protect_categories=PROTECTED,
    )

    print(f"Отклонение по итогам января: {result['deviation_total']} ₽\n")
    print(f"{'Категория':<45}{'План фев':>10}{'Без защиты':>13}{'С защитой':>12}")
    for name, limit in result["category_limits"].items():
        plan_val = next(c.plan[1] for c in PLAN_2026 if c.name == name)
        print(f"{name:<45}{plan_val:>10}{limit:>13}{result_protected['category_limits'][name]:>12}")

    print(f"\nscale_factor без защиты: {result['scale_factor']}")
    print(f"scale_factor с защитой:  {result_protected['scale_factor']}")
    for w in result["warnings"] + result_protected["warnings"]:
        print(f"⚠ {w}")
