from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


class ProjectSmokeTest(TestCase):
    def test_admin_login_page_is_available(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/login.html")
        self.assertContains(response, "csrfmiddlewaretoken")


class AuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Test-only-password-27!"
        cls.manager = User.objects.create_user(
            "manager", password=cls.password, first_name="Тестовый", last_name="Менеджер"
        )
        # The administrator role depends on is_superuser, independently of is_staff.
        cls.administrator = User.objects.create_user(
            "administrator", password=cls.password, is_superuser=True, is_staff=False
        )
        cls.staff = User.objects.create_user("staff", password=cls.password, is_staff=True)
        cls.inactive = User.objects.create_user(
            "inactive", password=cls.password, is_active=False
        )

    def login(self, client=None, **extra):
        return (client or self.client).post(
            reverse("login"),
            {"username": self.manager.username, "password": self.password, **extra},
        )

    def test_login_page(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_valid_credentials(self):
        self.assertRedirects(self.login(), reverse("home"))
        self.assertEqual(self.client.session[SESSION_KEY], str(self.manager.pk))

    def test_invalid_password(self):
        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Не удалось войти")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_inactive_user_cannot_login(self):
        response = self.login(username=self.inactive.username)
        self.assertContains(response, "Не удалось войти")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_missing_credentials(self):
        response = self.client.post(reverse("login"), {})
        self.assertContains(response, "Не удалось войти")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_anonymous_home_redirects_to_login(self):
        self.assertRedirects(self.client.get(reverse("home")), "/login/?next=/")

    def test_manager_home(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Тестовый Менеджер")
        self.assertContains(response, "Менеджер")
        self.assertNotContains(response, "Управление пользователями")
        self.assertContains(response, 'action="/logout/"')

    def test_administrator_home(self):
        self.client.force_login(self.administrator)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "administrator")
        self.assertContains(response, "Администратор")
        self.assertContains(response, "Управление пользователями")

    def test_inactive_session_cannot_access_home(self):
        self.client.force_login(self.inactive)
        self.assertRedirects(self.client.get(reverse("home")), "/login/?next=/")

    def test_manager_and_staff_cannot_access_admin(self):
        for user in (self.manager, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("admin:index"))
                self.assertRedirects(response, "/admin/login/?next=/admin/")
                self.assertNotEqual(
                    self.client.get(reverse("admin:auth_user_changelist")).status_code, 200
                )

    def test_admin_login_rejects_manager_and_staff(self):
        for user in (self.manager, self.staff):
            with self.subTest(user=user.username):
                self.client.logout()
                response = self.client.post(
                    reverse("admin:login"),
                    {"username": user.username, "password": self.password},
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(SESSION_KEY, self.client.session)

    def test_superuser_can_login_to_admin_and_manage_users(self):
        response = self.client.post(
            reverse("admin:login"),
            {"username": self.administrator.username, "password": self.password, "next": reverse("admin:index")},
        )
        self.assertRedirects(response, reverse("admin:index"))
        self.assertEqual(self.client.get(reverse("admin:auth_user_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:auth_user_add")).status_code, 200)

    def test_post_logout_ends_session_and_protects_home(self):
        self.client.force_login(self.manager)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertRedirects(self.client.get(reverse("home")), "/login/?next=/")

    def test_get_logout_does_not_end_session(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.manager.pk))
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_external_redirect_is_rejected(self):
        for target in ("https://untrusted.example/", "//untrusted.example/", "https://testserver.evil.example/"):
            with self.subTest(target=target):
                self.client.logout()
                self.assertRedirects(self.login(next=target), reverse("home"))

    def test_local_next_is_preserved(self):
        self.assertRedirects(self.login(next="/?from=login"), "/?from=login")

    def test_csrf_login_rejects_missing_and_invalid_tokens(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        for token in (None, "x" * 64):
            with self.subTest(token=token):
                extra = {} if token is None else {"csrfmiddlewaretoken": token}
                self.assertEqual(self.login(client, **extra).status_code, 403)
                self.assertNotIn(SESSION_KEY, client.session)

    def test_csrf_logout_rejects_missing_and_invalid_tokens(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        client.get(reverse("home"))
        for token in (None, "x" * 64):
            with self.subTest(token=token):
                data = {} if token is None else {"csrfmiddlewaretoken": token}
                self.assertEqual(client.post(reverse("logout"), data).status_code, 403)
                self.assertIn(SESSION_KEY, client.session)

    def test_csrf_valid_login_and_logout(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        self.assertRedirects(
            self.login(client, csrfmiddlewaretoken=client.cookies["csrftoken"].value),
            reverse("home"),
        )
        # Login rotates the CSRF token; use the new value for logout.
        self.assertRedirects(
            client.post(reverse("logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}),
            reverse("login"),
        )
        self.assertNotIn(SESSION_KEY, client.session)
        self.assertRedirects(client.get(reverse("home")), "/login/?next=/")
