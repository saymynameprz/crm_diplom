from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("deals/kanban/", views.DealKanbanView.as_view(), name="deal_kanban"),
    path("deals/<int:pk>/change-stage/", views.DealChangeStageView.as_view(), name="deal_change_stage"),
    path("deals/", views.DealListView.as_view(), name="deal_list"),
    path("deals/create/", views.DealCreateView.as_view(), name="deal_create"),
    path("deals/<int:pk>/", views.DealDetailView.as_view(), name="deal_detail"),
    path("deals/<int:pk>/edit/", views.DealUpdateView.as_view(), name="deal_update"),
    path("deals/<int:pk>/delete/", views.DealDeleteView.as_view(), name="deal_delete"),
    path("tasks/", views.TaskListView.as_view(), name="task_list"),
    path("tasks/create/", views.TaskCreateView.as_view(), name="task_create"),
    path("tasks/<int:pk>/", views.TaskDetailView.as_view(), name="task_detail"),
    path("tasks/<int:pk>/edit/", views.TaskUpdateView.as_view(), name="task_update"),
    path("tasks/<int:pk>/delete/", views.TaskDeleteView.as_view(), name="task_delete"),
    path("requests/", views.RequestListView.as_view(), name="request_list"),
    path("requests/create/", views.RequestCreateView.as_view(), name="request_create"),
    path("requests/<int:pk>/", views.RequestDetailView.as_view(), name="request_detail"),
    path("requests/<int:pk>/edit/", views.RequestUpdateView.as_view(), name="request_update"),
    path("requests/<int:pk>/delete/", views.RequestDeleteView.as_view(), name="request_delete"),
    path("clients/", views.ClientListView.as_view(), name="client_list"),
    path("clients/create/", views.ClientCreateView.as_view(), name="client_create"),
    path("clients/<int:pk>/", views.ClientDetailView.as_view(), name="client_detail"),
    path("clients/<int:pk>/edit/", views.ClientUpdateView.as_view(), name="client_update"),
    path("clients/<int:pk>/delete/", views.ClientDeleteView.as_view(), name="client_delete"),
    path("", views.home, name="home"),
    path("login/", LoginView.as_view(template_name="crm/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
