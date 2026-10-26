from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Client(models.Model):
    full_name = models.CharField("Имя клиента", max_length=200)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    email = models.EmailField("Email", max_length=254, blank=True)
    company = models.CharField("Организация", max_length=200, blank=True)
    notes = models.TextField("Примечание", max_length=5000, blank=True)
    created_at = models.DateTimeField("Дата создания", auto_now_add=True)
    updated_at = models.DateTimeField("Дата изменения", auto_now=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.full_name

    def get_absolute_url(self):
        return reverse("client_detail", kwargs={"pk": self.pk})


class DeadlineQuerySet(models.QuerySet):
    def with_overdue(self, at=None):
        at = timezone.now() if at is None else at
        return self.annotate(overdue_flag=models.Case(
            models.When(due_at__lt=at, status__in=self.model.OPEN_STATUSES, then=True),
            default=False, output_field=models.BooleanField(),
        ))


class DeadlineMixin:
    @property
    def is_overdue(self):
        return self.due_at is not None and self.status in self.OPEN_STATUSES and self.due_at < timezone.now()


class ClientRequestQuerySet(DeadlineQuerySet):
    def visible_to(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(assigned_to=user)


class ClientRequest(DeadlineMixin, models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    OPEN_STATUSES = (Status.NEW, Status.IN_PROGRESS)
    objects = ClientRequestQuerySet.as_manager()

    client = models.ForeignKey(Client, verbose_name="Клиент", on_delete=models.PROTECT, related_name="requests")
    title = models.CharField("Название заявки", max_length=200)
    description = models.TextField("Описание", max_length=5000, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEW)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Ответственный", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="client_requests",
    )
    due_at = models.DateTimeField("Срок обработки", null=True, blank=True)
    created_at = models.DateTimeField("Дата создания", auto_now_add=True)
    updated_at = models.DateTimeField("Дата изменения", auto_now=True)

    class Meta:
        verbose_name = "Заявка"
        verbose_name_plural = "Заявки"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("request_detail", kwargs={"pk": self.pk})


class TaskQuerySet(DeadlineQuerySet):
    def visible_to(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(assigned_to=user, request__assigned_to=user)


class Task(DeadlineMixin, models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        COMPLETED = "completed", "Выполнена"

    OPEN_STATUSES = (Status.NEW, Status.IN_PROGRESS)

    request = models.ForeignKey(ClientRequest, verbose_name="Заявка", on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField("Название задачи", max_length=255)
    description = models.TextField("Описание", max_length=5000, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Ответственный", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="crm_tasks",
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEW)
    due_at = models.DateTimeField("Срок выполнения", null=True, blank=True)
    is_automatic = models.BooleanField("Автоматическая задача", default=False, editable=False)
    completed_at = models.DateTimeField("Дата завершения", null=True, blank=True, editable=False)
    created_at = models.DateTimeField("Дата создания", auto_now_add=True)
    updated_at = models.DateTimeField("Дата изменения", auto_now=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["request"], condition=models.Q(is_automatic=True), name="one_automatic_task_per_request"),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("task_detail", kwargs={"pk": self.pk})

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if not update_fields:
                return
        database = kwargs.get("using") or self._state.db or "default"
        previous = None
        if not self._state.adding:
            previous = type(self).objects.using(database).filter(pk=self.pk).values("status", "completed_at").first()
        status = self.status
        if previous and update_fields is not None and "status" not in update_fields:
            status = previous["status"]
        if status == self.Status.COMPLETED:
            if previous and previous["status"] == self.Status.COMPLETED and previous["completed_at"]:
                self.completed_at = previous["completed_at"]
            else:
                self.completed_at = timezone.now()
        else:
            self.completed_at = None
        if update_fields is not None:
            kwargs["update_fields"] = update_fields | {"completed_at", "updated_at"}
        return super().save(*args, **kwargs)


class DealQuerySet(models.QuerySet):
    def visible_to(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(assigned_to=user)


class Deal(models.Model):
    class Stage(models.TextChoices):
        NEW = "new", "Новая"
        CONTACT = "contact", "Контакт установлен"
        PROPOSAL = "proposal", "Коммерческое предложение"
        WON = "won", "Успешно завершена"
        LOST = "lost", "Проиграна"

    client = models.ForeignKey(Client, verbose_name="Клиент", on_delete=models.PROTECT, related_name="deals")
    title = models.CharField("Название сделки", max_length=200)
    description = models.TextField("Описание", max_length=5000, blank=True)
    amount = models.DecimalField("Сумма (руб.)", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    stage = models.CharField("Этап", max_length=20, choices=Stage.choices, default=Stage.NEW)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="Ответственный", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="crm_deals")
    expected_close_date = models.DateField("Планируемая дата закрытия", null=True, blank=True)
    created_at = models.DateTimeField("Дата создания", auto_now_add=True)
    updated_at = models.DateTimeField("Дата изменения", auto_now=True)

    objects = DealQuerySet.as_manager()

    class Meta:
        verbose_name = "Сделка"
        verbose_name_plural = "Сделки"
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="deal_amount_positive")]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("deal_detail", kwargs={"pk": self.pk})
