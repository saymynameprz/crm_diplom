from django.db.models import Count

from .models import Deal


def load_columns(user, stages=None):
    choices = [(value, label) for value, label in Deal.Stage.choices if stages is None or value in stages]
    deals = Deal.objects.visible_to(user).filter(stage__in=[value for value, _ in choices])
    counts = dict(deals.order_by().values_list("stage").annotate(total=Count("pk")))
    return [{"stage": value, "label": label, "count": counts.get(value, 0),
             "deals": list(deals.filter(stage=value).select_related("client", "assigned_to").order_by("-created_at", "-pk")[:20])}
            for value, label in choices]
