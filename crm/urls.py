from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", LoginView.as_view(template_name="crm/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
