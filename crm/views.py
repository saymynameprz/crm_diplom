import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import PermissionDenied, RequestDataTooBig
from django.db import DatabaseError
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.template.loader import render_to_string
from django.views.csrf import csrf_failure as default_csrf_failure
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.cache import never_cache
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView, View
from django.views.generic.detail import SingleObjectMixin

from .forms import ClientForm, ClientRequestForm, TaskForm, DealForm, DealStageForm, ClientInteractionForm, InteractionFilterForm, AnalyticsPeriodForm
from .models import Client, ClientRequest, Task, Deal, ClientInteraction
from .services import AutomationError, create_request_with_task
from .kanban import load_columns
from .analytics import build_report
from .analytics_presentation import present_report
from .forms import (OptimizationForm, OptimizationSizeForm, OPTIMIZATION_BODY_LIMIT,
                    OPTIMIZATION_NODE_LIMIT, OPTIMIZATION_TIME_LIMIT)
from .optimization import greedy, branch_and_bound, Status, Result
from .optimization_presentation import input_rows, present_result, compare_results
from .forms import (OptimizationSnapshotForm, OptimizationDetailsForm, OptimizationFinalForm,
                    OPTIMIZATION_COMMIT_BODY_LIMIT)
from .models import OptimizationCommit
from .optimization_commit import (CommitError, issue_snapshot, read_snapshot, decode_snapshot,
                                  prepare_snapshot, commit_snapshot)

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
        history = list(ClientInteraction.objects.visible_to(self.request.user).filter(client=self.object).select_related("created_by").order_by("-occurred_at", "-pk")[:11])
        context["client_interactions"] = history[:10]
        context["has_more_interactions"] = len(history) > 10
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
            form.add_error(None, "Нельзя удалить клиента: у него есть связанные заявки, сделки или история взаимодействий. Данные сохранены.")
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
        context.update(columns=load_columns(self.request.user), stage_choices=Deal.Stage.choices)
        return context



def wants_kanban_json(request):
    return any(part.split(";")[0].strip().lower() == "application/json" for part in request.headers.get("Accept", "").split(","))


def kanban_error(message, status):
    return JsonResponse({"success": False, "message": message}, status=status)


def csrf_failure(request, reason=""):
    match = getattr(request, "resolver_match", None)
    if match and match.url_name == "deal_change_stage" and wants_kanban_json(request):
        return kanban_error("Проверка CSRF не пройдена. Обновите страницу и повторите действие.", 403)
    return default_csrf_failure(request, reason=reason)


class DealChangeStageView(DealAccessMixin, SingleObjectMixin, View):
    http_method_names = ["post"]

    def handle_no_permission(self):
        if wants_kanban_json(self.request):
            return kanban_error("Сессия истекла. Войдите в систему и обновите доску.", 401)
        return super().handle_no_permission()

    def http_method_not_allowed(self, request, *args, **kwargs):
        if wants_kanban_json(request):
            response = kanban_error("Разрешён только POST.", 405)
            response["Allow"] = "POST"
            return response
        return super().http_method_not_allowed(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        try:
            return self.change_stage(request)
        except Http404:
            if wants_kanban_json(request):
                return kanban_error("Сделка не найдена или недоступна.", 404)
            raise
        except Exception:
            if not wants_kanban_json(request):
                raise
            logger.exception("Ошибка JSON-операции смены этапа")
            return kanban_error("Не удалось подтвердить результат. Обновите доску перед повтором.", 500)

    def change_stage(self, request):
        deal = self.get_object()
        form = DealStageForm(request.POST)
        if not form.is_valid():
            if wants_kanban_json(request):
                return kanban_error("Проверьте этап и версию карточки. Обновите доску. Сделка не изменена.", 400)
            return render(request, "crm/deal_stage_error.html", {"form": form, "deal": deal}, status=400)
        expected = form.cleaned_data["expected_updated_at"]
        if expected != deal.updated_at:
            return self.stage_conflict(request)
        previous = deal.stage
        stage = form.cleaned_data["stage"]
        message = "Сделка уже находится на выбранном этапе."
        current = Deal.objects.visible_to(request.user).filter(pk=deal.pk, stage=previous, updated_at=expected)
        if previous != stage:
            # The ownership/version predicate is part of the atomic SQL UPDATE, including on SQLite.
            changed = current.update(stage=stage, updated_at=max(timezone.now(), expected + timedelta(microseconds=1)))
            if not changed:
                self.get_object()  # Preserve generic 404 if ownership changed or the row disappeared.
                return self.stage_conflict(request)
            message = "Сделка перемещена."
        elif not current.exists():
            self.get_object()
            return self.stage_conflict(request)
        if wants_kanban_json(request):
            columns = load_columns(request.user, {previous, stage})
            fragments = [{"stage": column["stage"], "count": column["count"],
                          "html": render_to_string("crm/_kanban_column.html", {"column": column, "stage_choices": Deal.Stage.choices}, request=request)}
                         for column in columns]
            return JsonResponse({"success": True, "deal_id": deal.pk, "stage": stage, "message": message, "columns": fragments})
        messages.success(request, message)
        return redirect("deal_kanban")

    def stage_conflict(self, request):
        message = "Сделка была изменена после загрузки карточки. Ваше изменение не сохранено. Обновите доску."
        if wants_kanban_json(request):
            return kanban_error(message, 409)
        return render(request, "crm/deal_stage_conflict.html", {"message": message}, status=409)


class InteractionAccessMixin(LoginRequiredMixin):
    model = ClientInteraction
    context_object_name = "interaction"

    def get_queryset(self):
        return super().get_queryset().visible_to(self.request.user).select_related("client", "created_by")


class InteractionListView(InteractionAccessMixin, ListView):
    context_object_name = "interactions"
    paginate_by = 10

    def get_queryset(self):
        interactions = super().get_queryset()
        self.filter_form = InteractionFilterForm(self.request.GET or None)
        if self.filter_form.is_bound and not self.filter_form.is_valid():
            return interactions.none()
        params = self.filter_form.cleaned_data if self.filter_form.is_bound else {}
        if params.get("q"):
            interactions = interactions.filter(Q(subject__icontains=params["q"]) | Q(client__full_name__icontains=params["q"]))
        if params.get("interaction_type"):
            interactions = interactions.filter(interaction_type=params["interaction_type"])
        if params.get("client"):
            interactions = interactions.filter(client=params["client"])
        return interactions.order_by("occurred_at", "pk") if params.get("sort") == "oldest" else interactions.order_by("-occurred_at", "-pk")

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "filter_form": self.filter_form}


class InteractionDetailView(InteractionAccessMixin, DetailView):
    pass


class InteractionFormMixin:
    form_class = ClientInteractionForm
    http_method_names = ["get", "post", "head", "options"]

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_initial(self):
        return {**super().get_initial(), **({"client": self.request.GET["client"]} if "client" in self.request.GET and not self.object else {})}


class InteractionCreateView(InteractionAccessMixin, InteractionFormMixin, SuccessMessageMixin, CreateView):
    success_message = "Взаимодействие добавлено."


class InteractionUpdateView(InteractionAccessMixin, InteractionFormMixin, SuccessMessageMixin, UpdateView):
    success_message = "Взаимодействие сохранено."


class InteractionDeleteView(InteractionAccessMixin, SuccessMessageMixin, DeleteView):
    success_url = reverse_lazy("interaction_list")
    success_message = "Взаимодействие удалено."
    http_method_names = ["get", "post", "head", "options"]

    def get_object(self, queryset=None):
        interaction = super().get_object(queryset)
        if not self.request.user.is_superuser:
            raise PermissionDenied
        return interaction


@login_required
@require_GET
def analytics(request):
    at = timezone.now()
    form = AnalyticsPeriodForm(request.GET if request.GET else {"period": "all"}, at=at)
    period = form.selected_period() if form.is_valid() else None
    report = build_report(request.user, period, at)
    return render(request, "crm/analytics.html", {"form": form, "report": report, "charts": present_report(report)})


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def optimization(request):
    if not (request.user.is_active and request.user.is_superuser):
        raise PermissionDenied
    context = {"node_limit": OPTIMIZATION_NODE_LIMIT, "time_limit": OPTIMIZATION_TIME_LIMIT}
    if request.method == "POST":
        try:
            if int(request.META.get("CONTENT_LENGTH") or 0) > OPTIMIZATION_BODY_LIMIT:
                raise RequestDataTooBig
        except (RequestDataTooBig, ValueError):
            context["input_error"] = "Слишком большой запрос: допустимо не более 16 КиБ. Откройте новый расчёт."
            return render(request, "crm/optimization.html", context, status=400)
        data = request.POST
    else:
        # Only nonprivate dimensions belong in GET; expert inputs are POST-only.
        data = request.GET.copy()
        data.setdefault("request_count", "2")
        data.setdefault("employee_count", "2")
    size_form = OptimizationSizeForm(data)
    context["size_form"] = size_form
    if not size_form.is_valid():
        context["input_error"] = "Выберите от 1 до 6 заявок и от 1 до 4 сотрудников."
        return render(request, "crm/optimization.html", context, status=400)
    users = list(get_user_model().objects.filter(is_active=True).order_by("username", "pk"))
    form = OptimizationForm(request.POST if request.method == "POST" else None,
                            users=users, **size_form.cleaned_data)
    context["form"] = form
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            problem = form.to_problem()
            g = greedy(problem)
            b = branch_and_bound(problem, node_limit=OPTIMIZATION_NODE_LIMIT,
                                 time_limit_seconds=OPTIMIZATION_TIME_LIMIT)
            titles = {int(form.cleaned_data[f"request_id_{i}"]): form.cleaned_data[f"title_{i}"]
                      for i in range(form.request_count)}
            names = {user.pk: user.get_username() for user in users}
            context["recommendations"] = [
                {"name": "Жадный алгоритм", **present_result(g, titles, names)},
                {"name": "Ветви и границы", **present_result(b, titles, names)},
            ]
            context["comparison"] = compare_results(g, b)
            context["rescued"] = g.status == Status.HEURISTIC_FAILED and b.assignment is not None
            context["results"] = (g, b)
            context["snapshot"] = issue_snapshot(form, b, request.user)
        except Exception:
            # Only the web boundary converts failures; M2 still raises bugs in direct tests.
            logger.exception("Технический отказ расчёта оптимизации")
            failed = Result(Status.TECHNICAL_ERROR, termination_reason="calculation_failed")
            context["recommendations"] = [{"name": "Расчёт", **present_result(failed, {}, {})}]
            status = 503
    context["employee_rows"], context["request_rows"] = input_rows(form)
    return render(request, "crm/optimization.html", context, status=status)


def _optimization_admin(request):
    if not (request.user.is_active and request.user.is_superuser):
        raise PermissionDenied


def _optimization_post(request):
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > OPTIMIZATION_COMMIT_BODY_LIMIT:
            raise ValueError
    except ValueError as error:
        raise CommitError("Слишком большой запрос подтверждения (предел 256 КиБ).", 400) from error
    return request.POST


def _optimization_preview_context(verified):
    form, result, payload = verified.form, verified.result, verified.payload
    titles = {int(form.cleaned_data[f"request_id_{i}"]): form.cleaned_data[f"title_{i}"]
              for i in range(form.request_count)}
    names = {int(pk): name for pk, name in payload["users"].items()}
    employees, requests = input_rows(form)
    return {"recommendation": present_result(result, titles, names), "source_employees": employees,
            "source_requests": requests, "source_form": form,
            "package_rows": [{"id": i, "title": titles[i], "employee": names[j]}
                             for i, j in result.assignment]}


def _optimization_error(request, error, retry_form=None):
    if error.status >= 500 and error.__cause__ is not None:
        cause = error.__cause__
        logger.error("Ошибка базы данных при подтверждении оптимизации",
                     exc_info=(type(cause), cause, cause.__traceback__))
    return render(request, "crm/optimization_confirm.html", {
        "error": error.message, "retry_form": retry_form if error.retry else None,
    }, status=error.status)


@never_cache
@login_required
@require_POST
def optimization_prepare(request):
    _optimization_admin(request)
    try:
        form = OptimizationSnapshotForm(_optimization_post(request))
        if not form.is_valid():
            raise CommitError("Не удалось прочитать снимок расчёта.", 400)
        raw = form.cleaned_data["snapshot"]
        verified = read_snapshot(raw, request.user)
        details_form = OptimizationDetailsForm(local_ids=[i for i, _ in verified.result.assignment],
            initial={"snapshot": raw, "horizon_start": timezone.now()})
        context = _optimization_preview_context(verified)
        context["details_form"] = details_form
        return render(request, "crm/optimization_confirm.html", context)
    except CommitError as error:
        return _optimization_error(request, error)
    except Exception:
        logger.exception("Не удалось подготовить подтверждение оптимизации")
        return _optimization_error(request, CommitError("Не удалось подготовить пакет. Повторите расчёт.", 503))


@never_cache
@login_required
@require_POST
def optimization_preview(request):
    _optimization_admin(request)
    try:
        data = _optimization_post(request)
        raw_form = OptimizationSnapshotForm({"snapshot": data.get("snapshot", "")})
        if not raw_form.is_valid():
            raise CommitError("Некорректный снимок расчёта.", 400)
        verified = read_snapshot(raw_form.cleaned_data["snapshot"], request.user)
        details_form = OptimizationDetailsForm(data, local_ids=[i for i, _ in verified.result.assignment])
        context = _optimization_preview_context(verified)
        if not details_form.is_valid():
            context["details_form"] = details_form
            return render(request, "crm/optimization_confirm.html", context, status=400)
        signed = prepare_snapshot(verified, details_form, request.user)
        for row in context["package_rows"]:
            row["client"] = details_form.cleaned_data[f"client_{row['id']}"]
            row["description"] = details_form.cleaned_data[f"description_{row['id']}"]
        context.update(horizon_start=details_form.cleaned_data["horizon_start"],
                       due_at=details_form.cleaned_data["due_at"], prepared=True,
                       final_form=OptimizationFinalForm(initial={"snapshot": signed},
                           unproven=verified.result.status == Status.FEASIBLE_NOT_PROVEN))
        return render(request, "crm/optimization_confirm.html", context)
    except CommitError as error:
        return _optimization_error(request, error)
    except Exception:
        logger.exception("Не удалось проверить предпросмотр оптимизации")
        return _optimization_error(request, CommitError("Не удалось проверить предпросмотр. Повторите подготовку.", 503))


@never_cache
@login_required
@require_POST
def optimization_confirm(request):
    _optimization_admin(request)
    form = None
    try:
        data = _optimization_post(request)
        raw_form = OptimizationSnapshotForm({"snapshot": data.get("snapshot", "")})
        if not raw_form.is_valid():
            raise CommitError("Некорректный снимок подтверждения.", 400)
        payload = decode_snapshot(raw_form.cleaned_data["snapshot"], request.user, stage="prepared", allow_expired=True)
        form = OptimizationFinalForm(data, unproven=payload["plan"]["status"] == Status.FEASIBLE_NOT_PROVEN.value)
        if not form.is_valid():
            return _optimization_error(request, CommitError("Подтвердите пакет и необходимые согласия. Данные не созданы.", 400, retry=True), form)
        record = commit_snapshot(form.cleaned_data["snapshot"], request.user,
                                 accept_unproven=form.cleaned_data.get("accept_unproven", False))
    except CommitError as error:
        return _optimization_error(request, error, form)
    except Exception:
        logger.exception("Технический сбой при подтверждении пакета оптимизации")
    else:
        try:
            return redirect("optimization_result", token=record.token)
        except Exception:
            logger.exception("Не удалось сформировать ответ после подтверждения пакета оптимизации")
    return _optimization_error(request, CommitError(
        "Не удалось подтвердить получение результата операции. Пакет мог быть создан. "
        "Повторите это же подтверждение для проверки результата.", 503, retry=True), form)


@never_cache
@login_required
@require_GET
def optimization_result(request, token):
    _optimization_admin(request)
    try:
        record = OptimizationCommit.objects.get(token=token, created_by=request.user)
    except OptimizationCommit.DoesNotExist:
        raise Http404
    requests = ClientRequest.objects.in_bulk([r["request_id"] for r in record.created_ids])
    tasks = Task.objects.in_bulk([r["task_id"] for r in record.created_ids])
    users = get_user_model().objects.in_bulk([r["employee_id"] for r in record.created_ids])
    rows = [{**r, "request": requests.get(r["request_id"]), "task": tasks.get(r["task_id"]),
             "employee": users.get(r["employee_id"])} for r in record.created_ids]
    return render(request, "crm/optimization_confirm.html", {"commit": record, "created_rows": rows})
