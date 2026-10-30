from html.parser import HTMLParser

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


class ShellMarkup(HTMLParser):
    """Inspect actual responses without coupling shell assertions to whitespace."""
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, body):
        super().__init__()
        self.stack, self.errors, self.ids, self.elements, self.navs, self.forms = [], [], [], [], [], []
        self.nav = self.form = None
        self.feed(body)
        if self.stack:
            self.errors.append("Unclosed elements")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "nav" and "navigation" in attrs.get("class", "").split():
            self.nav = []
            self.navs.append(self.nav)
        if tag == "a" and self.nav is not None:
            self.nav.append(attrs)
        if tag == "form":
            self.form = {**attrs, "inputs": []}
            self.forms.append(self.form)
        if tag == "input" and self.form is not None:
            self.form["inputs"].append(attrs)
        if tag not in self.void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"Unexpected closing {tag}")
        else:
            self.stack.pop()
        if tag == "nav":
            self.nav = None
        if tag == "form":
            self.form = None


class ApplicationShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from .models import Client as Contact, ClientRequest, Task, Deal, ClientInteraction
        cls.admin = User.objects.create_user("shell-admin", is_superuser=True, is_staff=False)
        cls.manager = User.objects.create_user("shell-manager", first_name="Учебный", last_name="Менеджер")
        cls.staff = User.objects.create_user("shell-staff", is_staff=True)
        cls.contact = Contact.objects.create(full_name="Вымышленный клиент каркаса")
        cls.request_record = ClientRequest.objects.create(client=cls.contact, title="Учебная заявка", assigned_to=cls.manager)
        cls.task = Task.objects.create(request=cls.request_record, title="Учебная задача", assigned_to=cls.manager)
        cls.deal = Deal.objects.create(client=cls.contact, title="Учебная сделка", amount="12.34", assigned_to=cls.manager)
        cls.interaction = ClientInteraction.objects.create(client=cls.contact, created_by=cls.manager, interaction_type="call", subject="Учебный звонок")

    def setUp(self):
        self.client.force_login(self.admin)

    def markup(self, response):
        markup = ShellMarkup(response.content.decode())
        self.assertEqual(markup.errors, [])
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        return markup

    def assert_active(self, response, name, kind="page"):
        markup = self.markup(response)
        self.assertEqual(len(markup.navs), 2)  # One fragment, desktop and native mobile presentation.
        for nav in markup.navs:
            active = [item for item in nav if "aria-current" in item]
            self.assertEqual([(item["href"], item["aria-current"]) for item in active], [(reverse(name), kind)])

    def test_authenticated_shell_contains_all_existing_section_links(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'class="sidebar"')
        self.assertContains(response, 'class="topbar"')
        expected = {reverse(name) for name in ("home", "client_list", "request_list", "task_list", "deal_list", "deal_kanban", "interaction_list", "analytics", "optimization")}
        for nav in self.markup(response).navs:
            self.assertEqual({item["href"] for item in nav}, expected)

    def test_login_has_no_working_shell_even_for_authenticated_user(self):
        for actor in (None, self.admin, self.manager):
            if actor:
                self.client.force_login(actor)
            else:
                self.client.logout()
            response = self.client.get(reverse("login"))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'class="sidebar"')
            self.assertNotContains(response, 'class="topbar"')
            self.assertEqual(self.markup(response).navs, [])
            self.assertContains(response, 'name="next"')

    def test_manager_and_staff_have_no_optimization_or_admin_links(self):
        for actor in (self.manager, self.staff):
            self.client.force_login(actor)
            response = self.client.get(reverse("home"))
            self.assertContains(response, 'class="sidebar"')
            self.assertContains(response, "Менеджер")
            self.assertNotContains(response, 'href="/optimization/"')
            self.assertNotContains(response, 'href="/admin/"')
            for nav in self.markup(response).navs:
                self.assertEqual(len(nav), 8)
            self.assertEqual(self.client.get(reverse("optimization")).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("home")), 'href="/admin/"')

    def test_section_roots_have_one_current_page_in_each_presentation(self):
        for name in ("home", "client_list", "request_list", "task_list", "deal_list", "interaction_list", "analytics", "optimization"):
            with self.subTest(name=name):
                self.assert_active(self.client.get(reverse(name)), name)

    def test_nested_crud_urls_keep_parent_section_active(self):
        groups = (("client", "client_list", self.contact.pk), ("request", "request_list", self.request_record.pk),
                  ("task", "task_list", self.task.pk), ("deal", "deal_list", self.deal.pk),
                  ("interaction", "interaction_list", self.interaction.pk))
        for prefix, parent, pk in groups:
            for suffix in ("create", "detail", "update", "delete"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    url = reverse(f"{prefix}_{suffix}", kwargs=None if suffix == "create" else {"pk": pk})
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assert_active(response, parent, "location")

    def test_kanban_and_real_conflict_have_their_own_active_section(self):
        from datetime import timedelta
        self.assert_active(self.client.get(reverse("deal_kanban")), "deal_kanban")
        response = self.client.post(reverse("deal_change_stage", kwargs={"pk": self.deal.pk}),
            {"stage": "won", "expected_updated_at": (self.deal.updated_at-timedelta(seconds=1)).isoformat()})
        self.assertEqual(response.status_code, 409)
        self.assert_active(response, "deal_kanban", "location")

    def test_all_optimization_post_stages_and_receipt_keep_active_section(self):
        from .test_optimization_commit import source_data, details_data
        raw_page = self.client.post(reverse("optimization"), source_data((self.manager, self.staff)))
        self.assert_active(raw_page, "optimization")
        raw = raw_page.context["snapshot"]
        choose = self.client.post(reverse("optimization_prepare"), {"snapshot": raw})
        self.assert_active(choose, "optimization", "location")
        preview = self.client.post(reverse("optimization_preview"), details_data(raw, [self.contact]))
        self.assert_active(preview, "optimization", "location")
        signed = preview.context["final_form"].initial["snapshot"]
        missing_consent = self.client.post(reverse("optimization_confirm"), {"snapshot": signed})
        self.assertEqual(missing_consent.status_code, 400)
        self.assert_active(missing_consent, "optimization", "location")
        result = self.client.post(reverse("optimization_confirm"), {"snapshot": signed, "confirm_create": "on"}, follow=True)
        self.assertEqual(result.status_code, 200)
        self.assert_active(result, "optimization", "location")

    def test_global_logout_on_subpage_keeps_csrf_post_and_session_protection(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.manager)
        response = browser.get(reverse("client_list"))
        logout = [form for form in self.markup(response).forms if form.get("action") == reverse("logout")]
        self.assertEqual(len(logout), 1)
        self.assertEqual(logout[0]["method"], "post")
        self.assertTrue(any(item.get("name") == "csrfmiddlewaretoken" for item in logout[0]["inputs"]))
        self.assertEqual(browser.get(reverse("logout")).status_code, 405)
        self.assertIn(SESSION_KEY, browser.session)
        self.assertEqual(browser.post(reverse("logout")).status_code, 403)
        self.assertIn(SESSION_KEY, browser.session)
        self.assertRedirects(browser.post(reverse("logout"), {"csrfmiddlewaretoken": browser.cookies["csrftoken"].value}), reverse("login"))
        self.assertNotIn(SESSION_KEY, browser.session)
        self.assertRedirects(browser.get(reverse("client_list")), "/login/?next=/clients/")

    def test_main_content_and_h1_are_not_duplicated_on_core_pages(self):
        for name in ("home", "login", "client_list", "request_list", "task_list", "deal_list", "deal_kanban", "interaction_list", "analytics", "optimization"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                markup = self.markup(response)
                self.assertEqual(sum(tag == "h1" for tag, _ in markup.elements), 1)
                self.assertEqual(sum(tag == "main" for tag, _ in markup.elements), 1)

    def test_django_success_message_remains_in_main_live_region(self):
        response = self.client.post(reverse("client_create"), {"full_name": "Вымышленный новый клиент"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Клиент создан.")
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'class="notice"')
        self.markup(response)

    def test_native_mobile_menu_is_closed_by_default_without_script_handlers(self):
        markup = self.markup(self.client.get(reverse("home")))
        details = [attrs for tag, attrs in markup.elements if tag == "details" and attrs.get("class") == "mobile-navigation"]
        self.assertEqual(len(details), 1)
        self.assertNotIn("open", details[0])
        self.assertTrue(any(tag == "summary" for tag, _ in markup.elements))
        self.assertFalse(any(key.startswith("on") for _, attrs in markup.elements for key in attrs))
        self.assertFalse(any(tag == "script" for tag, _ in markup.elements))

    def test_topbar_keeps_complete_escaped_user_identity(self):
        self.manager.first_name = '<script>Учебное имя</script>'
        self.manager.last_name = "ДлиннаяФамилия"*8
        self.manager.save(update_fields=["first_name", "last_name"])
        self.client.force_login(self.manager)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "&lt;script&gt;Учебное имя&lt;/script&gt;")
        self.assertContains(response, self.manager.last_name)
        self.assertNotContains(response, "<script>")
