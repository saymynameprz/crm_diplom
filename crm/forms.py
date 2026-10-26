from django import forms
from django.contrib.auth import get_user_model

from .models import Client, ClientRequest


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["full_name", "phone", "email", "company", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 5})}


class ClientRequestForm(forms.ModelForm):
    class Meta:
        model = ClientRequest
        fields = ["client", "title", "description", "status", "assigned_to", "due_at"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "due_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        }
        help_texts = {"due_at": "Дата и время UTC. Прошедший срок допускается."}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if user.is_superuser:
            self.fields["assigned_to"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
            self.fields["assigned_to"].required = True
        else:
            del self.fields["assigned_to"]

    def save(self, commit=True):
        if not self.instance.pk and not self.user.is_superuser:
            self.instance.assigned_to = self.user
        return super().save(commit=commit)
