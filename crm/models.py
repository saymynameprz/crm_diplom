from django.conf import settings
from django.db import models
from django.urls import reverse


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


class ClientRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

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
