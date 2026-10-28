"""Russian display of M2 results; rounding here never affects the mathematical plan."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction

from .optimization import Status


STATUS_TEXT = {
    Status.OPTIMAL: "Глобальная оптимальность доказана",
    Status.INFEASIBLE: "Невыполнимость доказана",
    Status.FEASIBLE_NOT_PROVEN: "Допустимое решение найдено, оптимальность не доказана",
    Status.UNKNOWN: "Поиск остановлен без полного решения",
    Status.HEURISTIC_FAILED: "Жадный алгоритм не нашёл полный план",
    Status.INPUT_INVALID: "Неверные исходные данные",
    Status.UNSUPPORTED_DEADLINE: "Гарантия календарного срока не поддерживается",
    Status.TECHNICAL_ERROR: "Технический отказ расчёта",
}

REASON_TEXT = {
    "search_exhausted": "Поиск завершён полностью.",
    "greedy_complete": "Последовательное назначение завершено.",
    "node_limit": "Достигнут серверный лимит посещённых узлов.",
    "time_limit": "Достигнут защитный лимит времени.",
    "no_employees": "Нет допустимых сотрудников.",
    "no_qualified_employee": "Для одной из заявок нет квалифицированного сотрудника.",
    "individual_capacity": "Одна из работ не помещается ни одному допустимому сотруднику.",
    "total_capacity": "Суммарной остаточной мощности недостаточно.",
    "individual_deadline_guarantee_unsupported": "Для индивидуальных сроков требуется календарное расписание.",
    "calculation_failed": "Не удалось выполнить расчёт. Повторите попытку; ошибка зарегистрирована на сервере.",
    "empty_problem": "Пакет не содержит работ.",
}


def display_number(value, places=2):
    """Fixed display precision only; inputs and result comparisons remain Fraction."""
    value = Fraction(value)
    with localcontext() as context:
        context.prec = 40
        number = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(number, f".{places}f").replace(".", ",")


def input_rows(form):
    data = getattr(form, "cleaned_data", {})
    employees, requests = [], []
    for j in range(form.employee_count):
        user = form.users.get(data.get(f"employee_{j}"))
        capacity, occupied = data.get(f"capacity_{j}"), data.get(f"occupied_{j}")
        valid_load = capacity is not None and occupied is not None and occupied <= capacity
        employees.append({
            "label": user.get_username() if user else f"Сотрудник {j + 1}",
            "fields": [form[f"{name}_{j}"] for name in ("employee", "capacity", "occupied", "rate")],
            "residual": display_number(Fraction(capacity - occupied, 60), 6) if valid_load else None,
            "initial_load": display_number(Fraction(occupied * 100, capacity)) if valid_load else None,
        })
    for i in range(form.request_count):
        requests.append({"identifier": form[f"request_id_{i}"], "title": form[f"title_{i}"],
                         "efforts": [form[f"effort_{i}_{j}"] for j in range(form.employee_count)]})
    return employees, requests


def present_result(result, titles, employee_names):
    complete = (result.status in (Status.OPTIMAL, Status.FEASIBLE_NOT_PROVEN)
                and result.assignment is not None and result.metrics is not None)
    reason = REASON_TEXT.get(result.termination_reason)
    if result.termination_reason.startswith("no_fitting_employee_for_request:"):
        reason = "Последовательный выбор зашёл в тупик; это не доказательство невыполнимости."
    if reason is None:
        reason = "Проверьте соответствие исходных данных условиям модели."
    shown = {"status": result.status.value, "description": STATUS_TEXT[result.status],
             "reason": reason, "nodes": result.nodes_visited, "complete": complete,
             "proven": result.optimality_proven,
             "warning": result.status in (Status.FEASIBLE_NOT_PROVEN, Status.UNKNOWN),
             "assignments": [], "metrics": None}
    if complete:
        metrics = result.metrics
        shown["assignments"] = [{"id": i, "title": titles[i], "employee": employee_names[j]}
                                for i, j in result.assignment]
        shown["metrics"] = {
            "cost": display_number(metrics.cost),
            "utilization": display_number(metrics.utilization * 100),
            "labor": display_number(metrics.labor_hours, 6),
            "objective": display_number(metrics.objective, 6),
            "exact_objective": str(metrics.objective),
        }
    return shown


def compare_results(greedy_result, exact_result):
    if (greedy_result.assignment is None or exact_result.assignment is None
            or greedy_result.metrics is None or exact_result.metrics is None):
        return None
    g, b = greedy_result.metrics, exact_result.metrics
    rows = []
    for label, first, second, places in (
        ("Целевая функция F", g.objective, b.objective, 6),
        ("Расчётные затраты C, ₽", g.cost, b.cost, 2),
        ("Максимальная загрузка U, %", g.utilization * 100, b.utilization * 100, 2),
        ("Трудозатраты T, чел.-ч", g.labor_hours, b.labor_hours, 6),
    ):
        rows.append({"label": label, "greedy": display_number(first, places),
                     "bnb": display_number(second, places),
                     "difference": display_number(first - second, places)})
    gain = ((g.objective - b.objective) / g.objective if g.objective else
            Fraction(0) if b.objective == 0 else None)
    return {"rows": rows, "gain": display_number(gain * 100) if gain is not None else None,
            "proven": exact_result.optimality_proven,
            "better": b.objective < g.objective}
