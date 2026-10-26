import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import DatabaseError
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView, View
from django.views.generic.detail import SingleObjectMixin

from .forms import ClientForm, ClientRequestForm, TaskForm, DealForm, DealStageForm
from .models import Client, ClientRequest, Task, Deal
from .services import AutomationError, create_request_with_task

logger = logging.getLogger(__name__)


@login_required
def home(request):
    at = timezone.now()
    request_counts = ClientRequest.objects.visible_to(request.user).with_overdue(at).aggregate(
        total=Count("pk"), overdue=Count("pk", filter=Q(overdue_flag=True)),
    )
    task_counts = Task.objects.visible_to(request.user).with_overdue(at).aggregate(
        total=Count("pk"), overdue=Count("pk", filter=Q(overdue_flag=True)),
    )
    return render(request, "crm/home.html", {
        "request_count": request_counts["total"], "task_count": task_counts["total"],
        "overdue_request_count": request_counts["overdue"], "overdue_task_count": task_counts["overdue"],
    })


class ClientListView(LoginRequiredMixin, ListView):
    model = Client
    paginate_by = 10
    context_object_name = "clients"

    def get_queryset(self):
        clients = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query:
            clients = clients.filter(
                Q(full_name__icontains=query)
                | Q(phone__icontains=query)
                | Q(email__icontains=query)
                | Q(company__icontains=query)
            )
        company = self.request.GET.get("company", "")
        if company == "yes":
            clients = clients.exclude(company="")
        elif company == "no":
            clients = clients.filter(company="")
        if self.request.GET.get("sort") == "oldest":
            return clients.order_by("created_at", "pk")
        return clients.order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            query=self.request.GET.get("q", "").strip(),
            company_filter=self.request.GET.get("company", ""),
            sort=self.request.GET.get("sort", "newest"),
        )
        return context


class ClientDetailView(LoginRequiredMixin, DetailView):
    model = Client

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        related = self.object.requests.select_related("assigned_to")
        if not self.request.user.is_superuser:
            related = related.filter(assigned_to=self.request.user)
        context["client_requests"] = related
        context["client_deals"] = Deal.objects.visible_to(self.request.user).filter(client=self.object).select_related("assigned_to")
        return context


class ClientCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Client
    form_class = ClientForm
    success_message = "Клиент создан."


class ClientUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Client
    form_class = ClientForm
    success_message = "Клиент сохранён."


class ClientDeleteView(LoginRequiredMixin, UserPassesTestMixin, SuccessMessageMixin, DeleteView):
    model = Client
    success_url = reverse_lazy("client_list")
    success_message = "Клиент удалён."
    http_method_names = ["get", "post", "head", "options"]

    def test_func(self):
        return self.request.user.is_superuser

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            form.add_error(None, "Нельзя удалить клиента: у него есть связанные заявки или сделки. Данные сохранены.")
            return self.form_invalid(form)


class RequestAccessMixin(LoginRequiredMixin):
    model = ClientRequest

    def get_queryset(self):
        return super().get_queryset().visible_to(self.request.user).select_related("client", "assigned_to")


class RequestListView(RequestAccessMixin, ListView):
    template_name = "crm/request_list.html"
    context_object_name = "requests"
    paginate_by = 10

    def get_queryset(self):
        requests = super().get_queryset().with_overdue()
        query = self.request.GET.get("q", "").strip()
        if query:
            requests = requests.filter(title__icontains=query)
        status = self.request.GET.get("status", "")
        if status in ClientRequest.Status.values:
            requests = requests.filter(status=status)
        overdue = self.request.GET.get("overdue")
        if overdue in ("yes", "no"):
            requests = requests.filter(overdue_flag=overdue == "yes")
        if self.request.GET.get("sort") == "oldest":
            return requests.order_by("created_at", "pk")
        return requests.order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            query=self.request.GET.get("q", "").strip(),
            status_filter=self.request.GET.get("status", ""),
            overdue_filter=self.request.GET.get("overdue", ""),
            status_choices=ClientRequest.Status.choices,
            sort=self.request.GET.get("sort", "newest"),
        )
        return context


class RequestDetailView(RequestAccessMixin, DetailView):
    template_name = "crm/request_detail.html"
    context_object_name = "client_request"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["request_tasks"] = Task.objects.visible_to(self.request.user).filter(request=self.object).select_related("assigned_to")
        return context


class RequestFormMixin:
    form_class = ClientRequestForm
    template_name = "crm/request_form.html"
    http_method_names = ["get", "post", "head", "options"]

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}


class RequestCreateView(RequestAccessMixin, RequestFormMixin, CreateView):
    def form_valid(self, form):
        try:
            self.object = create_request_with_task(form)
        except (AutomationError, DatabaseError):
            logger.exception("Не удалось сохранить заявку с автоматической задачей")
            form.instance.pk = None
            form.instance._state.adding = True
            form.add_error(None, "Не удалось создать заявку и задачу. Данные не сохранены. Попробуйте ещё раз.")
            return self.form_invalid(form)
        messages.success(self.request, "Заявка создана. Автоматическая задача добавлена.")
        return HttpResponseRedirect(self.get_success_url())


class RequestUpdateView(RequestAccessMixin, RequestFormMixin, SuccessMessageMixin, UpdateView):
    success_message = "Заявка сохранена."


class RequestDeleteView(RequestAccessMixin, UserPassesTestMixin, SuccessMessageMixin, DeleteView):
    template_name = "crm/request_confirm_delete.html"
    context_object_name = "client_request"
    success_url = reverse_lazy("request_list")
    success_message = "Заявка удалена."
    http_method_names = ["get", "post", "head", "options"]

    def test_func(self):
        return self.request.user.is_superuser

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            form.add_error(None, "Нельзя удалить заявку: у неё есть связанные задачи. Данные сохранены.")
            return self.form_invalid(form)


class TaskAccessMixin(LoginRequiredMixin):
    model = Task

    def get_queryset(self):
        return super().get_queryset().visible_to(self.request.user).select_related(
            "request__client", "request__assigned_to", "assigned_to"
        )


class TaskListView(TaskAccessMixin, ListView):
    template_name = "crm/task_list.html"
    context_object_name = "tasks"
    paginate_by = 10

    def get_queryset(self):
        tasks = super().get_queryset().with_overdue()
        query = self.request.GET.get("q", "").strip()
        if query:
            tasks = tasks.filter(title__icontains=query)
        status = self.request.GET.get("status", "")
        if status in Task.Status.values:
            tasks = tasks.filter(status=status)
        overdue = self.request.GET.get("overdue")
        if overdue in ("yes", "no"):
            tasks = tasks.filter(overdue_flag=overdue == "yes")
        if self.request.GET.get("sort") == "oldest":
            return tasks.order_by("created_at", "pk")
        return tasks.order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            query=self.request.GET.get("q", "").strip(),
            status_filter=self.request.GET.get("status", ""),
            overdue_filter=self.request.GET.get("overdue", ""),
            status_choices=Task.Status.choices,
            sort=self.request.GET.get("sort", "newest"),
        )
        return context


class TaskDetailView(TaskAccessMixin, DetailView):
    template_name = "crm/task_detail.html"
    context_object_name = "task"


class TaskFormMixin:
    form_class = TaskForm
    template_name = "crm/task_form.html"
    http_method_names = ["get", "post", "head", "options"]

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}


class TaskCreateView(TaskAccessMixin, TaskFormMixin, SuccessMessageMixin, CreateView):
    success_message = "Задача создана."


class TaskUpdateView(TaskAccessMixin, TaskFormMixin, SuccessMessageMixin, UpdateView):
    success_message = "Задача сохранена."


class TaskDeleteView(TaskAccessMixin, UserPassesTestMixin, SuccessMessageMixin, DeleteView):
    template_name = "crm/task_confirm_delete.html"
    context_object_name = "task"
    success_url = reverse_lazy("task_list")
    success_message = "Задача удалена."
    http_method_names = ["get", "post", "head", "options"]

    def test_func(self):
        return self.request.user.is_superuser


class DealAccessMixin(LoginRequiredMixin):
    model = Deal

    def get_queryset(self):
        return super().get_queryset().visible_to(self.request.user).select_related("client", "assigned_to")


class DealListView(DealAccessMixin, ListView):
    context_object_name = "deals"
    paginate_by = 10

    def get_queryset(self):
        deals = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query:
            deals = deals.filter(Q(title__icontains=query) | Q(client__full_name__icontains=query))
        stage = self.request.GET.get("stage", "")
        if stage in Deal.Stage.values:
            deals = deals.filter(stage=stage)
        if self.request.GET.get("sort") == "oldest":
            return deals.order_by("created_at", "pk")
        return deals.order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(query=self.request.GET.get("q", "").strip(), stage_filter=self.request.GET.get("stage", ""),
                       stage_choices=Deal.Stage.choices, sort=self.request.GET.get("sort", "newest"))
        return context


class DealDetailView(DealAccessMixin, DetailView):
    pass


class DealFormMixin:
    form_class = DealForm
    http_method_names = ["get", "post", "head", "options"]

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}


class DealCreateView(DealAccessMixin, DealFormMixin, SuccessMessageMixin, CreateView):
    success_message = "Сделка создана."


class DealUpdateView(DealAccessMixin, DealFormMixin, SuccessMessageMixin, UpdateView):
    success_message = "Сделка сохранена."


class DealDeleteView(DealAccessMixin, UserPassesTestMixin, SuccessMessageMixin, DeleteView):
    success_url = reverse_lazy("deal_list")
    success_message = "Сделка удалена."
    http_method_names = ["get", "post", "head", "options"]

    def test_func(self):
        return self.request.user.is_superuser


class DealKanbanView(LoginRequiredMixin, TemplateView):
    template_name = "crm/deal_kanban.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        deals = Deal.objects.visible_to(self.request.user)
        counts = dict(deals.order_by().values_list("stage").annotate(total=Count("pk")))
        columns = []
        for value, label in Deal.Stage.choices:
            cards = list(deals.filter(stage=value).select_related("client", "assigned_to").order_by("-created_at", "-pk")[:20])
            columns.append({"stage": value, "label": label, "count": counts.get(value, 0), "deals": cards})
        context.update(columns=columns, stage_choices=Deal.Stage.choices)
        return context


class DealChangeStageView(DealAccessMixin, SingleObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        deal = self.get_object()
        form = DealStageForm(request.POST)
        if not form.is_valid():
            return render(request, "crm/deal_stage_error.html", {"form": form, "deal": deal}, status=400)
        stage = form.cleaned_data["stage"]
        if deal.stage != stage:
            deal.stage = stage
            deal.save(update_fields=["stage", "updated_at"])
            messages.success(request, "Сделка перемещена.")
        else:
            messages.success(request, "Сделка уже находится на выбранном этапе.")
        return redirect("deal_kanban")
