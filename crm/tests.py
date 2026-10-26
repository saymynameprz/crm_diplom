from django.test import TestCase
from django.urls import reverse


class ProjectSmokeTest(TestCase):
    def test_admin_login_page_is_available(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/login.html")
        self.assertContains(response, "csrfmiddlewaretoken")
