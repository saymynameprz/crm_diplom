from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client as TestClient, TestCase
from django.urls import reverse

from .forms import DealForm
from .models import Client, Deal


class DealTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("deal-admin", is_superuser=True)
        cls.manager = User.objects.create_user("deal-manager")
        cls.other = User.objects.create_user("deal-other")
        cls.inactive = User.objects.create_user("deal-inactive", is_active=False)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент сделок")
        cls.own = Deal.objects.create(client=cls.contact, title="Своя сделка", amount=Decimal("123.45"), assigned_to=cls.manager)
        cls.foreign = Deal.objects.create(client=cls.contact, title="Чужая сделка", amount=Decimal("678.90"), assigned_to=cls.other)

    def setUp(self):
        self.client.force_login(self.manager)

    def data(self, **extra):
        return {"client": self.contact.pk, "title": "Новая учебная сделка", "amount": "1234.56", **extra}

    def url(self, action, deal=None):
        return reverse("deal_" + action, kwargs={"pk": (deal or self.own).pk})

    def test_admin_and_manager_create_with_exact_decimal_and_client(self):
        for actor, owner in ((self.admin, self.other), (self.manager, self.manager)):
            self.client.force_login(actor)
            response = self.client.post(reverse("deal_create"), self.data(assigned_to=owner.pk), follow=True)
            deal = Deal.objects.latest("pk")
            self.assertRedirects(response, deal.get_absolute_url())
            self.assertContains(response, "Сделка создана.")
            self.assertEqual(deal.amount, Decimal("1234.56"))
            self.assertEqual(deal.client, self.contact)
            self.assertEqual(deal.assigned_to, owner)
            self.assertEqual(deal.stage, "new")
            self.assertIsNone(deal.expected_close_date)

    def test_stage_forgery_ignored_for_both_roles_on_creation(self):
        for actor in (self.admin, self.manager):
            self.client.force_login(actor)
            self.assertNotContains(self.client.get(reverse("deal_create")), 'name="stage"')
            for stage in ("contact", "proposal", "won", "lost", "invalid"):
                response = self.client.post(reverse("deal_create"), self.data(stage=stage, assigned_to=self.manager.pk))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Deal.objects.latest("pk").stage, "new")

    def test_manager_owner_forgery_ignored_on_create_and_edit(self):
        for owner in (self.other.pk, self.admin.pk, self.inactive.pk, 999999):
            self.client.post(reverse("deal_create"), self.data(assigned_to=owner))
            self.assertEqual(Deal.objects.latest("pk").assigned_to, self.manager)
        response = self.client.post(self.url("update"), self.data(stage="contact", assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.manager)
        self.assertNotContains(self.client.get(self.url("update")), 'name="assigned_to"')

    def test_names_required_trimmed_and_length_limits(self):
        for title in ("", " ", "\t\n", "x" * 201):
            response = self.client.post(reverse("deal_create"), self.data(title=title))
            self.assertIn("title", response.context["form"].errors)
        for description in ("", "x" * 5000):
            form = DealForm(self.data(title="x" * 200, description=description), user=self.manager)
            self.assertTrue(form.is_valid(), form.errors)
        form = DealForm(self.data(description="x" * 5001), user=self.manager)
        self.assertFalse(form.is_valid())
        self.assertIn("description", form.errors)
        form = DealForm(self.data(title="  Trimmed  "), user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Trimmed")
        self.assertEqual(Deal.objects.count(), 2)

    def test_invalid_amounts_rejected_and_entered_data_preserved(self):
        for amount in ("", "0", "-0.01", "-100", "invalid", "10000000000.00", "1.234", "NaN", "Infinity"):
            with self.subTest(amount=amount):
                response = self.client.post(reverse("deal_create"), self.data(amount=amount, description="Не терять текст"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("amount", response.context["form"].errors)
                self.assertContains(response, "Не терять текст")
                self.assertEqual(Deal.objects.count(), 2)

    def test_valid_amount_boundaries_and_precision(self):
        for amount in ("0.01", "0.10", "9999999999.99"):
            self.assertEqual(self.client.post(reverse("deal_create"), self.data(amount=amount)).status_code, 302)
            deal = Deal.objects.latest("pk")
            self.assertIsInstance(deal.amount, Decimal)
            self.assertEqual(deal.amount, Decimal(amount))

    def test_positive_amount_database_constraint(self):
        for amount in (Decimal("0"), Decimal("-1")):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Deal.objects.create(client=self.contact, title="Некорректная", amount=amount)
        self.assertEqual(Deal.objects.count(), 2)

    def test_nonexistent_client_and_invalid_date_rejected(self):
        for data, field in ((self.data(client=999999), "client"), (self.data(client=""), "client"),
                            (self.data(expected_close_date="2026-02-30"), "expected_close_date"),
                            (self.data(expected_close_date="bad"), "expected_close_date")):
            response = self.client.post(reverse("deal_create"), data)
            self.assertIn(field, response.context["form"].errors)
        self.assertEqual(Deal.objects.count(), 2)

    def test_admin_requires_active_owner_on_create_and_edit(self):
        self.client.force_login(self.admin)
        before = Deal.objects.values().get(pk=self.own.pk)
        for owner in (self.inactive.pk, 999999, ""):
            for url in (reverse("deal_create"), self.url("update")):
                response = self.client.post(url, self.data(assigned_to=owner, stage="won"))
                self.assertIn("assigned_to", response.context["form"].errors)
        self.assertEqual(Deal.objects.count(), 2)
        self.assertEqual(Deal.objects.values().get(pk=self.own.pk), before)
        form = self.client.get(reverse("deal_create")).context["form"]
        self.assertNotIn(self.inactive, form.fields["assigned_to"].queryset)

    def test_manager_and_admin_visibility_and_client_related_lists(self):
        response = self.client.get(reverse("deal_list"))
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, self.foreign.title)
        self.assertContains(self.client.get(self.contact.get_absolute_url()), self.own.title)
        self.assertNotContains(self.client.get(self.contact.get_absolute_url()), self.foreign.title)
        self.client.force_login(self.admin)
        for record in (self.own, self.foreign):
            self.assertContains(self.client.get(reverse("deal_list")), record.title)
            self.assertContains(self.client.get(self.contact.get_absolute_url()), record.title)
            self.assertEqual(self.client.get(self.url("detail", record)).status_code, 200)

    def test_foreign_direct_urls_get_and_post_do_not_modify(self):
        before = Deal.objects.values().get(pk=self.foreign.pk)
        for action in ("detail", "update"):
            self.assertEqual(self.client.get(self.url(action, self.foreign)).status_code, 404)
        self.assertEqual(self.client.post(self.url("update", self.foreign), self.data(stage="won")).status_code, 404)
        self.assertEqual(Deal.objects.values().get(pk=self.foreign.pk), before)

    def test_edit_stage_amount_description_date_and_client(self):
        another = Client.objects.create(full_name="Другой вымышленный клиент")
        created = self.own.created_at
        for stage, label in Deal.Stage.choices:
            response = self.client.post(self.url("update"), self.data(client=another.pk, stage=stage, amount="9876.54",
                                       description="Изменено", expected_close_date="2028-01-15"), follow=True)
            self.assertContains(response, "Сделка сохранена.")
            self.assertContains(response, label)
            self.assertContains(response, f"stage-{stage}")
            self.own.refresh_from_db()
            self.assertEqual(self.own.stage, stage)
            self.assertEqual(self.own.amount, Decimal("9876.54"))
            self.assertEqual(self.own.client, another)
            self.assertEqual(self.own.description, "Изменено")
            self.assertEqual(str(self.own.expected_close_date), "2028-01-15")
            self.assertEqual(self.own.created_at, created)
            self.assertGreater(self.own.updated_at, created)
        self.assertContains(self.client.get(self.url("update")), 'value="2028-01-15"')

    def test_invalid_edit_preserves_all_database_values(self):
        before = Deal.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url("update"), self.data(stage="invalid", amount="-1", description="Для исправления"))
        self.assertIn("stage", response.context["form"].errors)
        self.assertIn("amount", response.context["form"].errors)
        self.assertContains(response, "Для исправления")
        self.assertEqual(Deal.objects.values().get(pk=self.own.pk), before)

    def test_admin_reassigns_any_deal_and_access_follows_owner(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.url("update"), self.data(stage="proposal", assigned_to=self.other.pk)).status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.other)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url("detail")).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url("detail")).status_code, 200)

    def test_delete_get_only_confirms_post_preserves_client(self):
        self.client.force_login(self.admin)
        url = self.url("delete")
        self.assertContains(self.client.get(url), "Это действие нельзя отменить")
        self.assertTrue(Deal.objects.filter(pk=self.own.pk).exists())
        self.assertEqual(self.client.delete(url).status_code, 405)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Сделка удалена.")
        self.assertRedirects(response, reverse("deal_list"))
        self.assertFalse(Deal.objects.filter(pk=self.own.pk).exists())
        self.assertTrue(Client.objects.filter(pk=self.contact.pk).exists())

    def test_manager_and_staff_delete_forbidden(self):
        for staff in (False, True):
            self.manager.is_staff = staff
            self.manager.save()
            for record in (self.own, self.foreign):
                self.assertEqual(self.client.get(self.url("delete", record)).status_code, 403)
                self.assertEqual(self.client.post(self.url("delete", record)).status_code, 403)
        self.assertEqual(Deal.objects.count(), 2)

    def test_anonymous_all_gets_posts_denied(self):
        self.client.logout()
        before = list(Deal.objects.values())
        urls = [reverse("deal_list"), reverse("deal_create")]+[self.url(a) for a in ("detail", "update", "delete")]
        for url in urls:
            for method in (self.client.get, self.client.post):
                response = method(url, self.data())
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/login/?next="))
        self.assertEqual(list(Deal.objects.values()), before)

    def test_csrf_on_create_edit_delete(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        for url in (reverse("deal_create"), self.url("update"), self.url("delete")):
            browser.get(url)
            before = list(Deal.objects.values())
            for token in (None, "x"*64):
                data = self.data(stage="new", assigned_to=self.manager.pk)
                if token:
                    data["csrfmiddlewaretoken"] = token
                self.assertEqual(browser.post(url, data).status_code, 403)
                self.assertEqual(list(Deal.objects.values()), before)
            self.assertEqual(browser.post(url, self.data(stage="new", assigned_to=self.manager.pk,
                             csrfmiddlewaretoken=browser.cookies["csrftoken"].value)).status_code, 302)

    def test_search_title_and_client_filter_combination_hides_foreign(self):
        for params in ({"q": "Своя"}, {"q": self.contact.full_name}, {"q": self.contact.full_name, "stage": "new"}):
            response = self.client.get(reverse("deal_list"), params)
            self.assertQuerySetEqual(response.context["deals"], [self.own])
            self.assertNotContains(response, self.foreign.title)
        self.assertQuerySetEqual(self.client.get(reverse("deal_list"), {"q": self.contact.full_name, "stage": "won"}).context["deals"], [])
        self.assertQuerySetEqual(self.client.get(reverse("deal_list"), {"q": "Чужая"}).context["deals"], [])

    def test_sort_pagination_and_query_parameters(self):
        deals = [Deal.objects.create(client=self.contact, title=f"Page {n}", amount=Decimal("1.00"), assigned_to=self.manager) for n in range(11)]
        params = {"q": "Page", "stage": "new", "sort": "oldest"}
        first = self.client.get(reverse("deal_list"), params)
        second = self.client.get(reverse("deal_list"), {**params, "page": 2})
        self.assertQuerySetEqual(first.context["deals"], deals[:10])
        self.assertQuerySetEqual(second.context["deals"], deals[10:])
        self.assertContains(first, "q=Page&amp;stage=new&amp;sort=oldest&amp;page=2")
        newest = self.client.get(reverse("deal_list"), {"q": "Page"})
        self.assertQuerySetEqual(newest.context["deals"], list(reversed(deals))[:10])
        self.assertEqual(self.client.get(reverse("deal_list"), {"page": "invalid"}).status_code, 404)

    def test_client_protect_friendly_error(self):
        with self.assertRaises(ProtectedError):
            self.contact.delete()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("client_delete", kwargs={"pk": self.contact.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нельзя удалить клиента")
        self.assertContains(response, "сделки")
        self.assertContains(response, "Данные сохранены")
        self.assertTrue(Client.objects.filter(pk=self.contact.pk).exists())
        self.assertEqual(Deal.objects.count(), 2)

    def test_user_deletion_preserves_deal_and_admin_can_restore_assignment(self):
        self.other.delete()
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.assigned_to)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url("detail", self.foreign)), "Не назначен")
        self.assertContains(self.client.get(reverse("deal_list")), self.foreign.title)
        response = self.client.post(self.url("update", self.foreign), self.data(stage="contact", assigned_to=self.manager.pk))
        self.assertEqual(response.status_code, 302)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.assigned_to, self.manager)

    def test_empty_lists_and_missing_ids(self):
        empty = Client.objects.create(full_name="Без сделок")
        self.assertContains(self.client.get(empty.get_absolute_url()), "Нет доступных сделок клиента")
        self.assertContains(self.client.get(reverse("deal_list"), {"q": "No match"}), "Сделки не найдены")
        self.client.force_login(self.admin)
        for action in ("detail", "update", "delete"):
            url = reverse("deal_"+action, kwargs={"pk": 999999})
            self.assertEqual(self.client.get(url).status_code, 404)
        Deal.objects.all().delete()
        self.assertContains(self.client.get(reverse("deal_list")), "Доступных сделок пока нет")

    def test_html_escaped_put_denied_and_string_representation(self):
        self.own.description = '<script>alert("test")</script>'
        self.own.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")
        self.assertEqual(str(self.own), self.own.title)
        for url in (reverse("deal_create"), self.url("update")):
            self.assertEqual(self.client.put(url, self.data()).status_code, 405)
