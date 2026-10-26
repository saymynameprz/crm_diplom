from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class CrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crm'


class CrmAdminConfig(AdminConfig):
    default_site = 'crm.admin_site.CrmAdminSite'
