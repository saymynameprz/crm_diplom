"""Pure presentation of an already scoped analytics report; no ORM or business metrics."""
from decimal import Decimal, ROUND_HALF_UP, localcontext


def percentage(count, total):
    if type(count) is not int or type(total) is not int or not 0 <= count <= total or total == 0:
        return None
    with localcontext() as context:
        context.prec = 40
        return (Decimal(count)*100/Decimal(total)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def visual_row(row, total):
    share = percentage(row["count"], total)
    # format() supplies an ASCII decimal point regardless of Django's Russian locale.
    return {**row, "percentage": share, "width": format(share, ".1f") if share is not None else "0.0"}


def distribution_chart(group):
    if group is None:
        return None
    total = group["total"]
    invalid = bool(group["invalid_count"]) or type(total) is not int or total < 0
    invalid = invalid or any(type(row["count"]) is not int or not 0 <= row["count"] <= total for row in group["rows"])
    return {"state": "invalid" if invalid else "empty" if total == 0 else "ready", "total": total,
            "rows": [visual_row(row, total) for row in group["rows"]]}


def present_report(report):
    workload = []
    for name, label in (("requests", "Заявки"), ("tasks", "Задачи")):
        data = report["workload"][name]
        workload.append(visual_row({"value": name, "label": label, "count": data["overdue"], "total": data["total"]}, data["total"]))
    return {"deals": distribution_chart(report["deals"]), "interactions": distribution_chart(report["interactions"]), "workload": workload}
