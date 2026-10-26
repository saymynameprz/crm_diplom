from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import ClientForm, ClientRequestForm
from .models import Client, ClientRequest


@login_required
def home(request):
    return render(request, "crm/home.html")


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
            form.add_error(None, "Нельзя удалить клиента: у него есть связанные заявки. Данные сохранены.")
            return self.form_invalid(form)


class RequestAccessMixin(LoginRequiredMixin):
    model = ClientRequest

    def get_queryset(self):
        requests = super().get_queryset().select_related("client", "assigned_to")
        if not self.request.user.is_superuser:
            requests = requests.filter(assigned_to=self.request.user)
        return requests


class RequestListView(RequestAccessMixin, ListView):
    template_name = "crm/request_list.html"
    context_object_name = "requests"
    paginate_by = 10

    def get_queryset(self):
        requests = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query:
            requests = requests.filter(title__icontains=query)
        status = self.request.GET.get("status", "")
        if status in ClientRequest.Status.values:
            requests = requests.filter(status=status)
        if self.request.GET.get("sort") == "oldest":
            return requests.order_by("created_at", "pk")
        return requests.order_by("-created_at", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            query=self.request.GET.get("q", "").strip(),
            status_filter=self.request.GET.get("status", ""),
            status_choices=ClientRequest.Status.choices,
            sort=self.request.GET.get("sort", "newest"),
        )
        return context


class RequestDetailView(RequestAccessMixin, DetailView):
    template_name = "crm/request_detail.html"
    context_object_name = "client_request"


class RequestFormMixin:
    form_class = ClientRequestForm
    template_name = "crm/request_form.html"
    http_method_names = ["get", "post", "head", "options"]

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}


class RequestCreateView(RequestAccessMixin, RequestFormMixin, SuccessMessageMixin, CreateView):
    success_message = "Заявка создана."


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
