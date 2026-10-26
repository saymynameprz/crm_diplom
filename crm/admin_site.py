from django.contrib.admin import AdminSite
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError


class AdministratorAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_superuser:
            raise ValidationError(
                "Доступ разрешён только администратору.", code="no_admin_access"
            )


class CrmAdminSite(AdminSite):
    login_form = AdministratorAuthenticationForm

    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser
