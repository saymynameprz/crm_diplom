from datetime import timezone
from decimal import Decimal
from fractions import Fraction
import re

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone as django_timezone

from .analytics import period_bounds
from .models import Client, ClientRequest, Task, Deal, ClientInteraction
from .optimization import Employee, Request, Problem, InputError, hours_to_minutes


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


class DealStageForm(forms.Form):
    stage = forms.ChoiceField(label="Этап", choices=Deal.Stage.choices)
    expected_updated_at = forms.DateTimeField(label="Версия карточки", widget=forms.HiddenInput)


class ClientInteractionForm(forms.ModelForm):
    class Meta:
        model = ClientInteraction
        fields = ["client", "interaction_type", "subject", "description", "occurred_at"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "occurred_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        }
        help_texts = {"occurred_at": "Дата и время UTC. Только состоявшиеся события; будущая дата запрещена."}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # All clients are shared by authenticated users under the existing client policy.
        self.fields["client"].queryset = Client.objects.order_by("full_name", "pk")

    def save(self, commit=True):
        if self.instance._state.adding:
            self.instance.created_by = self.user
        return super().save(commit=commit)


class InteractionFilterForm(forms.Form):
    q = forms.CharField(label="Поиск по теме или клиенту", required=False, max_length=200)
    interaction_type = forms.ChoiceField(label="Тип", required=False, choices=[("", "Все типы"), *ClientInteraction.Type.choices])
    client = forms.ModelChoiceField(label="Клиент", required=False, queryset=Client.objects.order_by("full_name", "pk"), empty_label="Все клиенты")
    sort = forms.ChoiceField(label="Порядок", required=False, initial="newest", choices=[("newest", "Сначала последние"), ("oldest", "Сначала ранние")])


class AnalyticsPeriodForm(forms.Form):
    period = forms.ChoiceField(label="Период", initial="all", choices=[("all", "Всё время"), ("30d", "Последние 30 суток"), ("custom", "Произвольный период")])
    start_date = forms.DateField(label="Начальная дата UTC", required=False, input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    end_date = forms.DateField(label="Конечная дата UTC", required=False, input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))

    def __init__(self, *args, at, **kwargs):
        super().__init__(*args, **kwargs)
        self.at = at

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("period") == "custom":
            for field in ("start_date", "end_date"):
                value = cleaned.get(field)
                if not value and field not in self.errors:
                    self.add_error(field, "Укажите обе даты периода.")
                elif value and value > self.at.astimezone(timezone.utc).date():
                    self.add_error(field, "Дата не может быть позже сегодняшней даты UTC.")
            start, end = cleaned.get("start_date"), cleaned.get("end_date")
            if start and end and end < start:
                self.add_error("end_date", "Конечная дата не может быть раньше начальной.")
        return cleaned

    def selected_period(self):
        return period_bounds(self.cleaned_data["period"], self.cleaned_data.get("start_date"), self.cleaned_data.get("end_date"), self.at)


OPTIMIZATION_MAX_REQUESTS = 6
OPTIMIZATION_MAX_EMPLOYEES = 4
OPTIMIZATION_NODE_LIMIT = 10_000
OPTIMIZATION_TIME_LIMIT = 1
OPTIMIZATION_BODY_LIMIT = 16_384


class SingleValueForm(forms.Form):
    """Reject repeated scalar fields rather than silently taking the last value."""

    def clean(self):
        cleaned = super().clean()
        if hasattr(self.data, "getlist"):
            for name in self.fields:
                if len(self.data.getlist(self.add_prefix(name))) > 1:
                    self.add_error(name, "Передайте одно значение поля.")
        return cleaned


class OptimizationSizeForm(SingleValueForm):
    request_count = forms.TypedChoiceField(label="Планируемых заявок", coerce=int, initial=2,
        choices=[(n, str(n)) for n in range(1, OPTIMIZATION_MAX_REQUESTS + 1)])
    employee_count = forms.TypedChoiceField(label="Сотрудников", coerce=int, initial=2,
        choices=[(n, str(n)) for n in range(1, OPTIMIZATION_MAX_EMPLOYEES + 1)])


class OptimizationHoursField(forms.CharField):
    """Bound text before parsing; accept exact decimal hours or hours:minutes."""

    def __init__(self, *, positive=False, **kwargs):
        self.positive = positive
        super().__init__(max_length=8, widget=forms.TextInput(attrs={"placeholder": "2:15 или 2.25"}), **kwargs)

    def clean(self, value):
        text = super().clean(value)
        if not text:
            return None
        if re.fullmatch(r"[0-9]{1,4}:[0-5][0-9]", text):
            hours, minutes = text.split(":")
            result = int(hours) * 60 + int(minutes)
        elif re.fullmatch(r"[0-9]{1,4}(?:[.,][0-9]{1,2})?", text):
            try:
                result = hours_to_minutes(Decimal(text.replace(",", ".")))
            except InputError as error:
                raise forms.ValidationError("Оценка должна составлять целое число минут; используйте часы:минуты.") from error
        else:
            raise forms.ValidationError("Укажите часы как 2:15 или 2.25, без знака и экспоненты.")
        if result > 60_000 or (self.positive and result <= 0):
            raise forms.ValidationError("Допустимо от одной минуты до 1000 часов." if self.positive else "Допустимо от 0 до 1000 часов.")
        return result


class OptimizationDecimalField(forms.CharField):
    def __init__(self, *, maximum, **kwargs):
        self.maximum = Decimal(maximum)
        super().__init__(max_length=10, widget=forms.TextInput(attrs={"inputmode": "decimal"}), **kwargs)

    def clean(self, value):
        text = super().clean(value)
        if not text:
            return None
        if not re.fullmatch(r"[0-9]{1,7}(?:[.,][0-9]{1,2})?", text):
            raise forms.ValidationError("Укажите неотрицательное число, не более двух знаков после запятой, без экспоненты.")
        result = Decimal(text.replace(",", "."))
        if result > self.maximum:
            raise forms.ValidationError(f"Значение не должно превышать {self.maximum}.")
        return result


class OptimizationForm(SingleValueForm):
    alpha = OptimizationDecimalField(label="Вес стоимости α", maximum="1", initial="0.50")
    beta = OptimizationDecimalField(label="Вес загрузки β", maximum="1", initial="0.50")

    def __init__(self, *args, request_count, employee_count, users, **kwargs):
        super().__init__(*args, **kwargs)
        if not (type(request_count) is int and 1 <= request_count <= OPTIMIZATION_MAX_REQUESTS
                and type(employee_count) is int and 1 <= employee_count <= OPTIMIZATION_MAX_EMPLOYEES):
            raise ValueError("Optimization form dimensions exceed server limits")
        self.request_count, self.employee_count = request_count, employee_count
        # A fresh server-side snapshot of active users, matching ClientRequestForm.
        self.users = {user.pk: user for user in users if user.is_active}
        choices = [(pk, user.get_username()) for pk, user in self.users.items()]
        for j in range(employee_count):
            self.fields[f"employee_{j}"] = forms.TypedChoiceField(label=f"Сотрудник {j + 1}",
                choices=[("", "Выберите сотрудника"), *choices], coerce=int)
            self.fields[f"capacity_{j}"] = OptimizationHoursField(label="Мощность A, ч", positive=True)
            self.fields[f"occupied_{j}"] = OptimizationHoursField(label="Уже занято L, ч", initial="0")
            self.fields[f"rate_{j}"] = OptimizationDecimalField(label="Ставка, ₽/ч", maximum="1000000")
        for i in range(request_count):
            self.fields[f"request_id_{i}"] = forms.RegexField(label="Локальный ID", regex=r"\A[0-9]{1,6}\Z",
                max_length=6, initial=str(i + 1), widget=forms.TextInput(attrs={"inputmode": "numeric"}))
            self.fields[f"title_{i}"] = forms.CharField(label="Название", max_length=120)
            for j in range(employee_count):
                self.fields[f"effort_{i}_{j}"] = OptimizationHoursField(
                    label=f"Заявка {i + 1}, сотрудник {j + 1}: ч", positive=True, required=False)

    def clean(self):
        cleaned = super().clean()
        seen_employees, seen_requests = set(), set()
        for j in range(self.employee_count):
            key = f"employee_{j}"
            employee = cleaned.get(key)
            if employee is not None:
                if employee in seen_employees:
                    self.add_error(key, "Сотрудник уже выбран в другой строке.")
                seen_employees.add(employee)
            capacity, occupied = cleaned.get(f"capacity_{j}"), cleaned.get(f"occupied_{j}")
            if capacity is not None and occupied is not None and occupied > capacity:
                self.add_error(f"occupied_{j}", "Занятое время L не может превышать мощность A.")
        for i in range(self.request_count):
            key = f"request_id_{i}"
            identifier = cleaned.get(key)
            if identifier is not None:
                identifier = int(identifier)
                if identifier <= 0:
                    self.add_error(key, "Локальный ID должен быть положительным.")
                elif identifier in seen_requests:
                    self.add_error(key, "Локальный ID заявки уже используется.")
                seen_requests.add(identifier)
        alpha, beta = cleaned.get("alpha"), cleaned.get("beta")
        if alpha is not None and beta is not None and Fraction(alpha) + Fraction(beta) != 1:
            self.add_error("beta", "Сумма весов α и β должна точно равняться 1.")
        return cleaned

    def to_problem(self):
        """Only translate validated expert inputs; all objective math stays in M2."""
        if not self.is_valid():
            raise ValueError("Cannot build a problem from an invalid form")
        data = self.cleaned_data
        employees = tuple(Employee(data[f"employee_{j}"], data[f"capacity_{j}"],
            data[f"occupied_{j}"], Fraction(data[f"rate_{j}"])) for j in range(self.employee_count))
        requests = tuple(Request(int(data[f"request_id_{i}"]), tuple(
            (data[f"employee_{j}"], data[f"effort_{i}_{j}"])
            for j in range(self.employee_count) if data[f"effort_{i}_{j}"] is not None))
            for i in range(self.request_count))
        return Problem(requests, employees, Fraction(data["alpha"]), Fraction(data["beta"]))


OPTIMIZATION_SNAPSHOT_LIMIT = 131_072
OPTIMIZATION_COMMIT_BODY_LIMIT = 262_144


class OptimizationSnapshotForm(SingleValueForm):
    snapshot = forms.CharField(max_length=OPTIMIZATION_SNAPSHOT_LIMIT, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if set(self.data) - set(self.fields) - {"csrfmiddlewaretoken"}:
            self.add_error(None, "Состав подтверждения изменён. Используйте исходную форму.")
        return cleaned


class OptimizationDateTimeField(forms.DateTimeField):
    def clean(self, value):
        if isinstance(value, str) and len(value) > 32:
            raise forms.ValidationError("Слишком длинная дата.")
        result = super().clean(value)
        return result.astimezone(timezone.utc) if result is not None else None


class OptimizationDetailsForm(OptimizationSnapshotForm):
    horizon_start = OptimizationDateTimeField(label="Начало планового горизонта, UTC",
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))
    due_at = OptimizationDateTimeField(label="Общий срок окончания пакета, UTC",
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))
    estimates_confirmed = forms.BooleanField(label="Подтверждаю: A и L относятся к этому горизонту; экспертные оценки проверены мной. Глобального резервирования часов и календарной гарантии нет.")

    def __init__(self, *args, local_ids, **kwargs):
        super().__init__(*args, **kwargs)
        for identifier in local_ids:
            self.fields[f"client_{identifier}"] = forms.ModelChoiceField(
                label=f"Клиент заявки {identifier}", queryset=Client.objects.order_by("full_name", "pk"))
            self.fields[f"description_{identifier}"] = forms.CharField(label=f"Описание заявки {identifier}",
                max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("horizon_start"), cleaned.get("due_at")
        if end is not None and end <= django_timezone.now():
            self.add_error("due_at", "Срок должен быть в будущем относительно подтверждения.")
        if start is not None and end is not None and start >= end:
            self.add_error("horizon_start", "Начало горизонта должно быть раньше общего срока.")
        return cleaned


class OptimizationFinalForm(OptimizationSnapshotForm):
    confirm_create = forms.BooleanField(label="Подтверждаю создание всего показанного пакета заявок и автоматических задач.")
    accept_unproven = forms.BooleanField(label="Принимаю допустимое решение без доказательства глобальной оптимальности.", required=False)

    def __init__(self, *args, unproven=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["accept_unproven"].required = unproven
        if not unproven:
            del self.fields["accept_unproven"]
