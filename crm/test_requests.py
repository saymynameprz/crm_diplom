from django.contrib.auth.models import User
from django.db.models.deletion import ProtectedError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientRequestForm
from .models import Client, ClientRequest


class RequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("request-admin", is_superuser=True)
        cls.manager = User.objects.create_user("request-manager")
        cls.other = User.objects.create_user("other-manager")
        cls.inactive = User.objects.create_user("inactive-manager", is_active=False)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент заявок")
        cls.own = ClientRequest.objects.create(client=cls.contact, title="Своя заявка", assigned_to=cls.manager)
        cls.foreign = ClientRequest.objects.create(client=cls.contact, title="Чужая заявка", assigned_to=cls.other)

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, action, record=None):
        return reverse("request_" + action, kwargs={"pk": (record or self.own).pk})

    def data(self, **overrides):
        return {"client": self.contact.pk, "title": "Учебная заявка", "status": "new", **overrides}

    def test_admin_create_and_assign_active_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_create"), self.data(assigned_to=self.other.pk), follow=True)
        record = ClientRequest.objects.get(title="Учебная заявка")
        self.assertEqual(record.assigned_to, self.other)
        self.assertEqual(record.client, self.contact)
        self.assertRedirects(response, record.get_absolute_url())
        self.assertContains(response, "Заявка создана.")

    def test_manager_create_assigns_self_ignoring_forged_post(self):
        for assigned in (None, self.other.pk, self.inactive.pk, self.admin.pk):
            with self.subTest(assigned=assigned):
                data = self.data()
                if assigned is not None:
                    data["assigned_to"] = assigned
                response = self.client.post(reverse("request_create"), data)
                self.assertEqual(response.status_code, 302)
                record = ClientRequest.objects.latest("pk")
                self.assertEqual(record.assigned_to, self.manager)
        self.assertNotContains(self.client.get(reverse("request_create")), 'name="assigned_to"')

    def test_status_default(self):
        self.assertEqual(self.own.status, ClientRequest.Status.NEW)
        self.assertEqual(str(self.own), self.own.title)
        response = self.client.get(reverse("request_create"))
        self.assertNotIn("status", response.context["form"].fields)
        self.assertEqual(response.context["form"].instance.status, "new")

    def test_anonymous_denied_all_pages_and_posts(self):
        self.client.logout()
        before = list(ClientRequest.objects.values())
        urls = [reverse("request_list"), reverse("request_create")]
        urls += [self.url(action) for action in ("detail", "update", "delete")]
        for url in urls:
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method.__name__):
                    response = method(url, self.data())
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith("/login/?next="))
        self.assertEqual(list(ClientRequest.objects.values()), before)

    def test_manager_list_and_own_detail(self):
        response = self.client.get(reverse("request_list"))
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, self.foreign.title)
        response = self.client.get(self.url("detail"))
        self.assertContains(response, self.own.title)
        self.assertContains(response, "Новая")
        self.assertContains(response, self.contact.get_absolute_url())
        self.assertNotContains(response, '>Удалить</a>')

    def test_foreign_direct_urls_get_and_post_are_denied(self):
        before = ClientRequest.objects.values().get(pk=self.foreign.pk)
        for action in ("detail", "update"):
            for method in (self.client.get, self.client.post):
                with self.subTest(action=action, method=method.__name__):
                    expected = 405 if action == "detail" and method == self.client.post else 404
                    self.assertEqual(method(self.url(action, self.foreign), self.data()).status_code, expected)
        self.assertEqual(self.client.post(self.url("delete", self.foreign)).status_code, 403)
        self.assertEqual(ClientRequest.objects.values().get(pk=self.foreign.pk), before)

    def test_admin_can_see_and_edit_all_requests(self):
        self.client.force_login(self.admin)
        listing = self.client.get(reverse("request_list"))
        for record in (self.own, self.foreign):
            self.assertContains(listing, record.title)
            self.assertEqual(self.client.get(self.url("detail", record)).status_code, 200)
            response = self.client.post(self.url("update", record), self.data(title=record.title, assigned_to=self.admin.pk))
            self.assertEqual(response.status_code, 302)
            record.refresh_from_db()
            self.assertEqual(record.assigned_to, self.admin)

    def test_client_detail_only_exposes_accessible_requests(self):
        url = self.contact.get_absolute_url()
        response = self.client.get(url)
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, self.foreign.title)
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertContains(response, self.own.title)
        self.assertContains(response, self.foreign.title)
        self.client.force_login(self.inactive)
        self.assertEqual(self.client.get(reverse("request_list")).status_code, 302)

    def test_client_empty_related_list(self):
        empty = Client.objects.create(full_name="Без заявок")
        self.assertContains(self.client.get(empty.get_absolute_url()), "Нет доступных заявок клиента")

    def test_edit_description_client_and_due_date(self):
        contact = Client.objects.create(full_name="Другой вымышленный клиент")
        created = self.own.created_at
        updated = self.own.updated_at
        response = self.client.post(self.url("update"), self.data(
            description="Новое описание", client=contact.pk, due_at="2027-01-15T12:30",
        ), follow=True)
        self.assertContains(response, "Заявка сохранена.")
        self.own.refresh_from_db()
        self.assertEqual(self.own.description, "Новое описание")
        self.assertEqual(self.own.client, contact)
        self.assertEqual(self.own.due_at.isoformat(), "2027-01-15T12:30:00+00:00")
        self.assertEqual(self.own.created_at, created)
        self.assertGreater(self.own.updated_at, updated)

    def test_all_statuses_can_be_saved(self):
        for value, label in ClientRequest.Status.choices:
            with self.subTest(status=value):
                response = self.client.post(self.url("update"), self.data(status=value), follow=True)
                self.own.refresh_from_db()
                self.assertEqual(self.own.status, value)
                self.assertContains(response, label)
                self.assertContains(response, f"status-{value}")

    def test_invalid_status_does_not_change_record(self):
        before = ClientRequest.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url("update"), self.data(status="unknown", description="Не терять"))
        self.assertIn("status", response.context["form"].errors)
        self.assertContains(response, "Не терять")
        self.assertEqual(ClientRequest.objects.values().get(pk=self.own.pk), before)

    def test_empty_and_whitespace_title_rejected(self):
        for title in ("", "  ", "\t\n"):
            with self.subTest(title=title):
                response = self.client.post(reverse("request_create"), self.data(title=title))
                self.assertIn("title", response.context["form"].errors)
        self.assertEqual(ClientRequest.objects.count(), 2)

    def test_field_length_boundaries_and_trimming(self):
        data = self.data(title="x" * 200, description="y" * 5000)
        form = ClientRequestForm(data, user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        for field in ("title", "description"):
            form = ClientRequestForm({**data, field: data[field] + "x"}, user=self.manager)
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)
        form = ClientRequestForm(self.data(title="  Trimmed  "), user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Trimmed")

    def test_missing_and_nonexistent_client_rejected(self):
        for contact in ("", 999999):
            with self.subTest(client=contact):
                response = self.client.post(reverse("request_create"), self.data(client=contact))
                self.assertIn("client", response.context["form"].errors)
        self.assertEqual(ClientRequest.objects.count(), 2)

    def test_inactive_nonexistent_and_empty_assignment_rejected_for_admin(self):
        self.client.force_login(self.admin)
        for assigned in (self.inactive.pk, 999999, ""):
            with self.subTest(assigned=assigned):
                response = self.client.post(reverse("request_create"), self.data(assigned_to=assigned))
                self.assertIn("assigned_to", response.context["form"].errors)
        self.assertEqual(ClientRequest.objects.count(), 2)
        form = self.client.get(reverse("request_create")).context["form"]
        self.assertNotIn(self.inactive, form.fields["assigned_to"].queryset)

    def test_manager_cannot_reassign_on_edit_even_with_staff_flag(self):
        self.manager.is_staff = True
        self.manager.save()
        response = self.client.post(self.url("update"), self.data(assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.manager)
        self.assertNotContains(self.client.get(self.url("update")), 'name="assigned_to"')

    def test_due_date_invalid_past_future_and_clear(self):
        before = ClientRequest.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url("update"), self.data(due_at="invalid"))
        self.assertIn("due_at", response.context["form"].errors)
        self.assertEqual(ClientRequest.objects.values().get(pk=self.own.pk), before)
        for due in ("2020-01-15T10:15", "2028-06-15T12:30", ""):
            with self.subTest(due=due):
                self.assertEqual(self.client.post(self.url("update"), self.data(due_at=due)).status_code, 302)
                self.own.refresh_from_db()
                if due:
                    self.assertTrue(timezone.is_aware(self.own.due_at))
                    self.assertEqual(self.own.due_at.strftime("%Y-%m-%dT%H:%M"), due)
                    self.assertContains(self.client.get(self.url("update")), f'value="{due}"')
                else:
                    self.assertIsNone(self.own.due_at)

    def test_manager_cannot_delete_get_or_post(self):
        for record in (self.own, self.foreign):
            for method in (self.client.get, self.client.post):
                self.assertEqual(method(self.url("delete", record)).status_code, 403)
        self.assertEqual(ClientRequest.objects.count(), 2)

    def test_admin_delete_only_post(self):
        self.client.force_login(self.admin)
        url = self.url("delete")
        self.assertContains(self.client.get(url), "Это действие нельзя отменить")
        self.assertTrue(ClientRequest.objects.filter(pk=self.own.pk).exists())
        self.assertEqual(self.client.delete(url).status_code, 405)
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, reverse("request_list"))
        self.assertContains(response, "Заявка удалена.")
        self.assertFalse(ClientRequest.objects.filter(pk=self.own.pk).exists())
        self.assertTrue(Client.objects.filter(pk=self.contact.pk).exists())

    def test_missing_request_returns_404(self):
        for user in (self.manager, self.admin):
            self.client.force_login(user)
            for action in ("detail", "update", "delete"):
                if action == "delete" and not user.is_superuser:
                    continue
                url = reverse("request_" + action, kwargs={"pk": 999999})
                self.assertEqual(self.client.get(url).status_code, 404)
                if action != "detail":
                    self.assertEqual(self.client.post(url, self.data(assigned_to=self.manager.pk)).status_code, 404)

    def test_search_and_status_filter_respect_scope(self):
        ClientRequest.objects.create(client=self.contact, title="Поиск другая", assigned_to=self.manager, status="completed")
        ClientRequest.objects.create(client=self.contact, title="Поиск чужая", assigned_to=self.other)
        for params, expected in [({"q": "Своя"}, [self.own]), ({"status": "new"}, [self.own]),
                                  ({"q": "Поиск", "status": "new"}, []), ({"q": "Секрет"}, [])]:
            with self.subTest(params=params):
                response = self.client.get(reverse("request_list"), params)
                self.assertQuerySetEqual(response.context["requests"], expected)

    def test_pagination_filters_and_order(self):
        records = [ClientRequest.objects.create(client=self.contact, title=f"Page {n}", assigned_to=self.manager) for n in range(11)]
        url = reverse("request_list")
        params = {"q": "Page", "status": "new", "sort": "oldest"}
        first = self.client.get(url, params)
        second = self.client.get(url, {**params, "page": 2})
        self.assertEqual(first.context["paginator"].count, 11)
        self.assertQuerySetEqual(first.context["requests"], records[:10])
        self.assertQuerySetEqual(second.context["requests"], records[10:])
        self.assertContains(first, "q=Page&amp;status=new&amp;sort=oldest&amp;page=2")
        newest = self.client.get(url, {"q": "Page"})
        self.assertQuerySetEqual(newest.context["requests"], list(reversed(records))[:10])
        self.assertEqual(self.client.get(url, {"page": "bad"}).status_code, 404)

    def test_empty_list_and_invalid_filters(self):
        self.assertContains(self.client.get(reverse("request_list"), {"q": "no-match"}), "Заявки не найдены")
        response = self.client.get(reverse("request_list"), {"status": "bad", "sort": "title"})
        self.assertQuerySetEqual(response.context["requests"], [self.own])
        ClientRequest.objects.filter(assigned_to=self.manager).delete()
        self.assertContains(self.client.get(reverse("request_list")), "Доступных заявок пока нет")

    def test_client_protected_in_orm_and_friendly_http_response(self):
        with self.assertRaises(ProtectedError):
            self.contact.delete()
        self.client.force_login(self.admin)
        url = reverse("client_delete", kwargs={"pk": self.contact.pk})
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нельзя удалить клиента")
        self.assertContains(response, "Данные сохранены")
        self.assertNotContains(response, "Клиент удалён.")
        self.assertEqual(ClientRequest.objects.count(), 2)
        self.assertTrue(Client.objects.filter(pk=self.contact.pk).exists())

    def test_user_deletion_preserves_history(self):
        self.other.delete()
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.assigned_to)
        self.assertEqual(self.client.get(self.url("detail", self.foreign)).status_code, 404)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url("detail", self.foreign)), "Не назначен")
        self.assertContains(self.client.get(reverse("request_list")), self.foreign.title)
        self.assertTrue(Client.objects.filter(pk=self.contact.pk).exists())

    def test_csrf_required_and_valid_on_all_mutations(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        urls = [reverse("request_create"), self.url("update"), self.url("delete")]
        for url in urls:
            with self.subTest(url=url):
                browser.get(url)
                before = list(ClientRequest.objects.values())
                for token in (None, "x" * 64):
                    data = self.data(assigned_to=self.manager.pk)
                    if token:
                        data["csrfmiddlewaretoken"] = token
                    self.assertEqual(browser.post(url, data).status_code, 403)
                    self.assertEqual(list(ClientRequest.objects.values()), before)
                data = self.data(assigned_to=self.manager.pk, csrfmiddlewaretoken=browser.cookies["csrftoken"].value)
                self.assertEqual(browser.post(url, data).status_code, 302)

    def test_put_cannot_create_or_edit(self):
        for url in (reverse("request_create"), self.url("update")):
            self.assertEqual(self.client.put(url, self.data()).status_code, 405)
        self.assertEqual(ClientRequest.objects.count(), 2)

    def test_description_is_escaped(self):
        self.own.description = '<script>alert("test")</script>'
        self.own.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_admin_rejects_inactive_reassignment_without_data_change(self):
        self.client.force_login(self.admin)
        before = ClientRequest.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url("update"), self.data(assigned_to=self.inactive.pk))
        self.assertIn("assigned_to", response.context["form"].errors)
        self.assertEqual(ClientRequest.objects.values().get(pk=self.own.pk), before)

    def test_admin_can_reassign_orphaned_request(self):
        self.other.delete()
        self.client.force_login(self.admin)
        response = self.client.post(self.url("update", self.foreign), self.data(assigned_to=self.manager.pk))
        self.assertEqual(response.status_code, 302)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.assigned_to, self.manager)
