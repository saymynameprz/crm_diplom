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
