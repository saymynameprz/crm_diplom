from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ClientRequest, Task


class AutomationError(Exception):
    pass


@transaction.atomic
def ensure_automatic_task(linked_request):
    """Return the one automatic task without changing an existing task."""
    linked_request = ClientRequest.objects.select_for_update().select_related("assigned_to").get(pk=linked_request.pk)
    existing = Task.objects.filter(request=linked_request, is_automatic=True).first()
    if existing is not None:
        return existing
    owner = linked_request.assigned_to
    if owner is None or not owner.is_active:
        raise AutomationError("Заявке нужен активный ответственный.")
    task, _ = Task.objects.get_or_create(
        request=linked_request,
        is_automatic=True,
        defaults={
            "title": f"Обработать заявку №{linked_request.pk}: {linked_request.title}",
            "assigned_to": owner,
            "status": Task.Status.NEW,
            "due_at": linked_request.due_at if linked_request.due_at is not None else timezone.now() + timedelta(hours=24),
        },
    )
    return task


@transaction.atomic
def create_request_with_task(form):
    """Persist a new valid CRM form and its automatic task as one operation."""
    if form.instance.pk is not None or not form.is_valid():
        raise ValueError("Ожидается валидная форма новой заявки.")
    linked_request = form.save()
    ensure_automatic_task(linked_request)
    return linked_request
