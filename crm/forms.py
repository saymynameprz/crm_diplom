from django import forms
from django.contrib.auth import get_user_model

from .models import Client, ClientRequest, Task, Deal


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
        if self.instance._state.adding:
            del self.fields["status"]
        if user.is_superuser:
            self.fields["assigned_to"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
            self.fields["assigned_to"].required = True
        else:
            del self.fields["assigned_to"]

    def save(self, commit=True):
        if self.instance._state.adding:
            self.instance.status = ClientRequest.Status.NEW
        if not self.instance.pk and not self.user.is_superuser:
            self.instance.assigned_to = self.user
        return super().save(commit=commit)


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["request", "title", "description", "assigned_to", "status", "due_at"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "due_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        }
        help_texts = {"due_at": "Дата и время UTC. Прошедший срок допускается."}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        linked_requests = ClientRequest.objects.select_related("assigned_to")
        if user.is_superuser:
            self.fields["assigned_to"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
            self.fields["assigned_to"].required = True
        else:
            del self.fields["assigned_to"]
            linked_requests = linked_requests.filter(assigned_to=user)
        self.fields["request"].queryset = linked_requests
        if self.instance.is_automatic:
            self.fields["request"].disabled = True
        else:
            self.fields["title"] = forms.CharField(label="Название задачи", max_length=200)

    def clean(self):
        cleaned = super().clean()
        linked_request = cleaned.get("request")
        if linked_request:
            owner = linked_request.assigned_to
            if owner is None or not owner.is_active:
                self.add_error("request", "Сначала назначьте активного ответственного через редактирование заявки.")
            elif self.user.is_superuser:
                assignee = cleaned.get("assigned_to")
                if assignee and assignee.pk != owner.pk:
                    self.add_error("assigned_to", "Ответственный задачи должен совпадать с ответственным заявки.")
        return cleaned

    def save(self, commit=True):
        if not self.instance.pk and not self.user.is_superuser:
            self.instance.assigned_to = self.user
        return super().save(commit=commit)


class DealForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = ["client", "title", "description", "amount", "stage", "assigned_to", "expected_close_date"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "expected_close_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if self.instance._state.adding:
            del self.fields["stage"]
        if user.is_superuser:
            self.fields["assigned_to"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
            self.fields["assigned_to"].required = True
        else:
            del self.fields["assigned_to"]

    def save(self, commit=True):
        if self.instance._state.adding:
            self.instance.stage = Deal.Stage.NEW
            if not self.user.is_superuser:
                self.instance.assigned_to = self.user
        return super().save(commit=commit)
