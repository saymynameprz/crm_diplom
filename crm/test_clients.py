from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientForm
from .models import Client


class ClientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("client-manager")
        cls.administrator = User.objects.create_user("client-admin", is_superuser=True)
        cls.record = Client.objects.create(
            full_name="Вымышленный Контакт", phone="+7 (000) 123-45-67",
            email="contact@example.test", company="Учебная организация", notes="Тестовая запись",
        )

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, action):
        return reverse("client_" + action, kwargs={"pk": self.record.pk})

    def test_authenticated_list(self):
        for user in (self.manager, self.administrator):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertContains(self.client.get(reverse("client_list")), self.record.full_name)

    def test_anonymous_cannot_read_or_modify_any_client_page(self):
        self.client.logout()
        urls = [reverse("client_list"), reverse("client_create")]
        urls += [self.url(action) for action in ("detail", "update", "delete")]
        before = list(Client.objects.values())
        for url in urls:
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method.__name__):
                    response = method(url, {"full_name": "Unwanted"})
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith("/login/?next="))
        self.assertEqual(list(Client.objects.values()), before)

    def test_administrator_and_manager_can_create(self):
        for user in (self.administrator, self.manager):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(reverse("client_create"), {"full_name": user.username}, follow=True)
                record = Client.objects.get(full_name=user.username)
                self.assertRedirects(response, record.get_absolute_url())
                self.assertContains(response, "Клиент создан.")

    def test_blank_and_whitespace_name_rejected(self):
        for name in ("", "   ", "\t\n"):
            with self.subTest(name=name):
                response = self.client.post(reverse("client_create"), {"full_name": name})
                self.assertEqual(response.status_code, 200)
                self.assertIn("full_name", response.context["form"].errors)
        self.assertEqual(Client.objects.count(), 1)

    def test_email_validation_preserves_entered_data(self):
        response = self.client.post(reverse("client_create"), {
            "full_name": "Вымышленное имя", "email": "invalid", "phone": "123", "notes": "Сохранить текст",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertContains(response, "Вымышленное имя")
        self.assertContains(response, "Сохранить текст")
        self.assertContains(response, 'value="invalid"')
        self.assertContains(response, 'value="123"')
        self.assertEqual(Client.objects.count(), 1)

    def test_optional_fields_can_be_empty(self):
        form = ClientForm({"full_name": "Только имя"})
        self.assertTrue(form.is_valid(), form.errors)
        record = form.save()
        self.assertEqual((record.phone, record.email, record.company, record.notes), ("", "", "", ""))

    def test_form_strips_surrounding_whitespace(self):
        form = ClientForm({"full_name": "  Тестовый Клиент  ", "company": "   ", "phone": " 123 "})
        self.assertTrue(form.is_valid(), form.errors)
        record = form.save()
        self.assertEqual(record.full_name, "Тестовый Клиент")
        self.assertEqual(record.company, "")
        self.assertEqual(record.phone, "123")
        self.assertEqual(str(record), "Тестовый Клиент")

    def test_length_boundaries(self):
        valid = {"full_name": "x" * 200, "phone": "1" * 50, "company": "x" * 200, "notes": "x" * 5000,
                 "email": "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 56 + ".test"}
        self.assertEqual(len(valid["email"]), 254)
        form = ClientForm(valid)
        self.assertTrue(form.is_valid(), form.errors)
        for field in valid:
            with self.subTest(field=field):
                form = ClientForm({**valid, field: valid[field] + "x"})
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_duplicate_names_and_contacts_allowed(self):
        data = {"full_name": self.record.full_name, "phone": self.record.phone, "email": self.record.email}
        form = ClientForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Client.objects.count(), 2)

    def test_detail_and_escaped_notes(self):
        self.record.notes = '<script>alert("test")</script>'
        self.record.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, self.record.full_name)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_nonexistent_ids(self):
        for user in (self.manager, self.administrator):
            self.client.force_login(user)
            for action in ("detail", "update", "delete"):
                if action == "delete" and not user.is_superuser:
                    continue
                url = reverse("client_" + action, kwargs={"pk": 999999})
                with self.subTest(user=user.username, action=action):
                    self.assertEqual(self.client.get(url).status_code, 404)
                    if action != "detail":
                        self.assertEqual(self.client.post(url, {"full_name": "Новое имя"}).status_code, 404)

    def test_both_roles_edit_and_timestamps(self):
        created = self.record.created_at
        for user in (self.manager, self.administrator):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                old_updated = Client.objects.get(pk=self.record.pk).updated_at
                response = self.client.post(self.url("update"), {"full_name": user.username, "notes": "Обновлено"}, follow=True)
                self.record.refresh_from_db()
                self.assertContains(response, "Клиент сохранён.")
                self.assertEqual(self.record.full_name, user.username)
                self.assertEqual(self.record.notes, "Обновлено")
                self.assertEqual(self.record.created_at, created)
                self.assertGreater(self.record.updated_at, old_updated)

    def test_invalid_edit_does_not_change_data(self):
        before = Client.objects.values().get(pk=self.record.pk)
        response = self.client.post(self.url("update"), {"full_name": "", "email": "bad", "notes": "Новый текст"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Новый текст")
        self.assertEqual(Client.objects.values().get(pk=self.record.pk), before)

    def test_administrator_delete_with_message(self):
        self.client.force_login(self.administrator)
        response = self.client.post(self.url("delete"), follow=True)
        self.assertRedirects(response, reverse("client_list"))
        self.assertContains(response, "Клиент удалён.")
        self.assertFalse(Client.objects.filter(pk=self.record.pk).exists())

    def test_manager_and_staff_delete_forbidden(self):
        for staff in (False, True):
            self.manager.is_staff = staff
            self.manager.save()
            for method in (self.client.get, self.client.post):
                with self.subTest(staff=staff, method=method.__name__):
                    self.assertEqual(method(self.url("delete")).status_code, 403)
                    self.assertTrue(Client.objects.filter(pk=self.record.pk).exists())
        self.assertNotContains(self.client.get(self.url("detail")), '>Удалить</a>')

    def test_get_delete_only_confirms(self):
        self.client.force_login(self.administrator)
        response = self.client.get(self.url("delete"))
        self.assertContains(response, "Это действие нельзя отменить")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertTrue(Client.objects.filter(pk=self.record.pk).exists())
        self.assertEqual(self.client.delete(self.url("delete")).status_code, 405)
        self.assertTrue(Client.objects.filter(pk=self.record.pk).exists())

    def test_search_all_four_fields(self):
        Client.objects.create(full_name="Other record")
        for query in ("Вымышленный", "123-45", "CONTACT@EXAMPLE", "Учебная"):
            with self.subTest(query=query):
                response = self.client.get(reverse("client_list"), {"q": query})
                self.assertQuerySetEqual(response.context["clients"], [self.record])

    def test_company_filters_and_combined_search(self):
        empty = Client.objects.create(full_name="Вымышленный без организации")
        for filters, expected in [({"company": "yes"}, [self.record]), ({"company": "no"}, [empty]),
                                  ({"company": "no", "q": "Учебная"}, [])]:
            with self.subTest(filters=filters):
                response = self.client.get(reverse("client_list"), filters)
                self.assertQuerySetEqual(response.context["clients"], expected)

    def test_date_sorting_and_invalid_sort(self):
        earlier = Client.objects.create(full_name="Earlier")
        Client.objects.filter(pk=earlier.pk).update(created_at=timezone.now() - timedelta(days=1))
        for sort, expected in [("oldest", [earlier, self.record]), ("newest", [self.record, earlier]),
                               ("-full_name", [self.record, earlier])]:
            with self.subTest(sort=sort):
                response = self.client.get(reverse("client_list"), {"sort": sort})
                self.assertQuerySetEqual(response.context["clients"], expected)

    def test_pagination_and_filter_links(self):
        for number in range(11):
            Client.objects.create(full_name=f"Pagination {number}", company="Test")
        url = reverse("client_list")
        params = {"q": "Pagination", "company": "yes", "sort": "oldest"}
        first = self.client.get(url, params)
        second = self.client.get(url, {**params, "page": 2})
        self.assertEqual(len(first.context["clients"]), 10)
        self.assertEqual(len(second.context["clients"]), 1)
        self.assertEqual(first.context["paginator"].count, 11)
        first_ids = {obj.pk for obj in first.context["clients"]}
        self.assertNotIn(second.context["clients"][0].pk, first_ids)
        self.assertContains(first, 'q=Pagination&amp;company=yes&amp;sort=oldest&amp;page=2')
        self.assertEqual(self.client.get(url, {"page": "bad"}).status_code, 404)

    def test_empty_list_and_no_results(self):
        response = self.client.get(reverse("client_list"), {"q": "no-such-contact"})
        self.assertContains(response, "Клиенты не найдены")
        Client.objects.all().delete()
        self.assertContains(self.client.get(reverse("client_list")), "Клиентов пока нет")

    def test_csrf_missing_invalid_and_valid_on_all_mutations(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.administrator)
        for url in (reverse("client_create"), self.url("update"), self.url("delete")):
            with self.subTest(url=url):
                browser.get(url)
                before = list(Client.objects.values())
                for token in (None, "x" * 64):
                    data = {"full_name": "CSRF test"}
                    if token:
                        data["csrfmiddlewaretoken"] = token
                    self.assertEqual(browser.post(url, data).status_code, 403)
                    self.assertEqual(list(Client.objects.values()), before)
                response = browser.post(url, {"full_name": "CSRF test", "csrfmiddlewaretoken": browser.cookies["csrftoken"].value})
                self.assertEqual(response.status_code, 302)
        self.assertFalse(Client.objects.filter(pk=self.record.pk).exists())
        self.assertEqual(Client.objects.count(), 1)
