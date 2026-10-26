"""Read-only analytics; scope in SQL, money in Decimal, one caller-supplied clock."""
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.db.models import Count, Q

from .models import ClientInteraction, ClientRequest, Deal, Task

logger = logging.getLogger(__name__)
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")


@dataclass(frozen=True)
class AnalyticsPeriod:
    start: datetime | None = None
    end: datetime | None = None  # exclusive; current clock is a separate inclusive cap


def period_bounds(kind, start_date, end_date, at):
    """Dates come from the validated period form; no second clock reading."""
    if kind == "30d":
        return AnalyticsPeriod(start=at-timedelta(days=30))
    if kind == "custom":
        return AnalyticsPeriod(datetime.combine(start_date, time.min, timezone.utc),
                               datetime.combine(end_date+timedelta(days=1), time.min, timezone.utc))
    return AnalyticsPeriod()


def visible_data(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    return {"deals": Deal.objects.visible_to(user), "interactions": ClientInteraction.objects.visible_to(user),
            "requests": ClientRequest.objects.visible_to(user), "tasks": Task.objects.visible_to(user)}


def in_period(queryset, field, period, at):
    queryset = queryset.filter(**{field+"__lte": at})
    if period.start is not None:
        queryset = queryset.filter(**{field+"__gte": period.start})
    if period.end is not None:
        queryset = queryset.filter(**{field+"__lt": period.end})
    return queryset


def distribution(queryset, field, choices):
    grouped = dict(queryset.order_by().values_list(field).annotate(total=Count("pk")))
    counts = {value: grouped.get(value, 0) for value, _ in choices}
    invalid = sum(number for value, number in grouped.items() if value not in counts)
    if invalid:
        logger.warning("Analytics encountered %s records with invalid %s", invalid, field)
    return {"total": sum(grouped.values()), "counts": counts, "invalid_count": invalid,
            "rows": [{"value": value, "label": label, "count": counts[value]} for value, label in choices]}


def closed_conversion(won, lost):
    if not won+lost:
        return None
    with localcontext() as context:
        context.prec = 40
        return (Decimal(won)/Decimal(won+lost)*Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def validate_amount(amount):
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise ValueError("Money must be a finite Decimal")
    if not CENT <= amount <= MAX_AMOUNT or amount.as_tuple().exponent < -2:
        raise ValueError("Money is outside Decimal(12,2) positive range or scale")


def won_amount(queryset):
    try:
        with localcontext() as context:
            context.prec = 40
            total = Decimal("0.00")
            for amount in queryset.filter(stage=Deal.Stage.WON).order_by().values_list("amount", flat=True).iterator(chunk_size=500):
                validate_amount(amount)
                total += amount
        return total
    except (ArithmeticError, ValueError, TypeError, DatabaseError):
        # Conversion can fail while advancing the ORM iterator, before any value reaches validation.
        logger.exception("Analytics cannot reliably calculate won deal amounts")
        return None


def current_workload(data, at):
    result = {}
    for name in ("requests", "tasks"):
        result[name] = data[name].with_overdue(at).aggregate(total=Count("pk"), overdue=Count("pk", filter=Q(overdue_flag=True)))
    return result


def build_report(user, period, at):
    data = visible_data(user)
    report = {"at": at, "workload": current_workload(data, at), "deals": None, "interactions": None}
    if period is None:  # invalid form: current workload remains meaningful; period metrics are withheld
        return report
    deals = in_period(data["deals"], "created_at", period, at)
    result = distribution(deals, "stage", Deal.Stage.choices)
    counts = result["counts"]
    result.update(open=sum(counts[stage] for stage in (Deal.Stage.NEW, Deal.Stage.CONTACT, Deal.Stage.PROPOSAL)),
                  closed=counts[Deal.Stage.WON]+counts[Deal.Stage.LOST],
                  conversion=None if result["invalid_count"] else closed_conversion(counts[Deal.Stage.WON], counts[Deal.Stage.LOST]),
                  won_amount=won_amount(deals))
    report["deals"] = result
    report["interactions"] = distribution(in_period(data["interactions"], "occurred_at", period, at), "interaction_type", ClientInteraction.Type.choices)
    return report
