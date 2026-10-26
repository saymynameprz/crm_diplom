from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, DatabaseError
from django.test import Client as TestClient, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Client, ClientRequest, Deal, Task
from .views import DealChangeStageView


class DragDropTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("drag-admin", is_superuser=True)
        cls.manager = User.objects.create_user("drag-manager")
        cls.other = User.objects.create_user("drag-other")
        cls.contact = Client.objects.create(full_name="Вымышленный клиент JSON")
        cls.own = Deal.objects.create(client=cls.contact, title="Своя JSON", amount=Decimal("123.45"), assigned_to=cls.manager)
        cls.foreign = Deal.objects.create(client=cls.contact, title="Чужая JSON", amount=Decimal("1.00"), assigned_to=cls.other)

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, deal=None):
        return reverse("deal_change_stage", kwargs={"pk": (deal or self.own).pk})

    def post(self, data, deal=None):
        return self.client.post(self.url(deal), data, HTTP_ACCEPT="application/json")

    def test_html_redirect_and_explicit_json_are_compatible(self):
        self.assertRedirects(self.client.post(self.url(), {"stage": "contact"}), reverse("deal_kanban"))
        response = self.post({"stage": "proposal"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual((data["deal_id"], data["stage"]), (self.own.pk, "proposal"))
        self.assertEqual({c["stage"] for c in data["columns"]}, {"contact", "proposal"})
        self.own.refresh_from_db()
        self.assertEqual(self.own.stage, "proposal")
        self.assertEqual(self.client.post(self.url(), {"stage": "won"}, HTTP_ACCEPT="*/*").status_code, 302)

    def test_only_stage_updated_at_change_and_no_new_objects(self):
        before = Deal.objects.values().get(pk=self.own.pk)
        counts = (Deal.objects.count(), ClientRequest.objects.count(), Task.objects.count())
        at = self.own.updated_at + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=at):
            response = self.post({"stage": "won", "amount": "-1", "title": "Подмена", "assigned_to": self.other.pk,
                                  "client": 999999, "description": "Подмена", "expected_close_date": "2000-01-01",
                                  "created_at": "2000-01-01", "next": "https://untrusted.example/"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Deal.objects.values().get(pk=self.own.pk), {**before, "stage": "won", "updated_at": at})
        self.assertEqual((Deal.objects.count(), ClientRequest.objects.count(), Task.objects.count()), counts)

    def test_noop_preserves_timestamp_and_returns_one_column(self):
        before = Deal.objects.values().get(pk=self.own.pk)
        response = self.post({"stage": "new"})
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(response.json()["columns"]), 1)
        self.assertEqual(Deal.objects.values().get(pk=self.own.pk), before)

    def test_invalid_missing_stage_and_get_json_errors(self):
        before = list(Deal.objects.values())
        for data in ({}, {"stage": "invalid"}, {"stage": ""}):
            response = self.post(data)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json()["success"])
            self.assertNotIn("columns", response.json())
        response = self.client.get(self.url(), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()["success"])
        self.assertEqual(list(Deal.objects.values()), before)

    def test_foreign_missing_and_anonymous_json_errors(self):
        self.assertEqual(self.post({"stage": "won"}, self.foreign).status_code, 404)
        response = self.client.post(reverse("deal_change_stage", kwargs={"pk": 999999}), {"stage": "won"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])
        self.client.logout()
        response = self.post({"stage": "won"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["success"])
        self.own.refresh_from_db()
        self.assertEqual(self.own.stage, "new")

    def test_csrf_json_failure_and_success_and_html_failure_unchanged(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.manager)
        browser.get(reverse("deal_kanban"))
        for token in (None, "x"*64):
            kwargs = {} if token is None else {"HTTP_X_CSRFTOKEN": token}
            response = browser.post(self.url(), {"stage": "won"}, HTTP_ACCEPT="application/json", **kwargs)
            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.json()["success"])
        response = browser.post(self.url(), {"stage": "won"}, HTTP_ACCEPT="application/json", HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stage"], "won")
        self.assertEqual(browser.post(self.url(), {"stage": "new"}).status_code, 403)
        self.assertEqual(browser.post(reverse("deal_create"), {}, HTTP_ACCEPT="application/json").status_code, 403)

    def test_fragments_and_counts_scoped_and_html_escaped(self):
        self.own.title = '<script>alert("test")</script>'
        self.own.save()
        data = self.post({"stage": "contact"}).json()
        self.assertEqual({c["stage"]: c["count"] for c in data["columns"]}, {"new": 0, "contact": 1})
        html = ''.join(c["html"] for c in data["columns"])
        self.assertNotIn(self.foreign.title, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("В этом этапе сделок нет.", html)
        self.assertIn('name="csrfmiddlewaretoken"', html)

    def test_admin_all_columns_include_foreign_records(self):
        self.client.force_login(self.admin)
        data = self.post({"stage": "won"}, self.foreign).json()
        self.assertTrue(data["success"])
        self.assertEqual({c["stage"]: c["count"] for c in data["columns"]}, {"new": 1, "won": 1})
        self.assertIn(self.foreign.title, ''.join(c["html"] for c in data["columns"]))

    def test_full_columns_refill_and_never_exceed_limit(self):
        # Own is old enough to be initially hidden, then moves into a full target.
        for stage in ("new", "contact"):
            for n in range(25):
                Deal.objects.create(client=self.contact, title=f"{stage} {n}", amount=Decimal("1.00"), assigned_to=self.manager, stage=stage)
        moving = Deal.objects.filter(stage="new", assigned_to=self.manager).latest("pk")
        expected_next = list(Deal.objects.visible_to(self.manager).filter(stage="new").order_by("-created_at", "-pk"))[20].pk
        data = self.post({"stage": "contact"}, moving).json()
        for column in data["columns"]:
            self.assertEqual(column["html"].count('class="kanban-deal"'), 20)
            self.assertEqual(column["count"], Deal.objects.visible_to(self.manager).filter(stage=column["stage"]).count())
            self.assertIn('>Посмотреть все</a>', column["html"])
            if column["stage"] == "new":
                self.assertIn(f'data-deal-id="{expected_next}"', column["html"])
                self.assertNotIn(f'data-deal-id="{moving.pk}"', column["html"])
        self.assertEqual(data["columns"][1]["count"], 26)

    def test_sequential_moves_follow_database_and_leave_empty_columns(self):
        for stage in ("contact", "proposal", "won", "lost", "new"):
            data = self.post({"stage": stage}).json()
            self.own.refresh_from_db()
            self.assertEqual(self.own.stage, stage)
            self.assertEqual(data["stage"], stage)
            self.assertEqual(sum(c["html"].count(f'data-deal-id="{self.own.pk}"') for c in data["columns"]), 1)

    def test_json_data_queries_are_bounded(self):
        request = RequestFactory().post(self.url(), {"stage": "contact"}, HTTP_ACCEPT="application/json")
        request.user = self.manager
        with CaptureQueriesContext(connection) as queries:
            response = DealChangeStageView.as_view()(request, pk=self.own.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(queries), 5)  # object, UPDATE, grouped count, two limited columns
        self.assertEqual(sum("LIMIT 20" in q["sql"] for q in queries), 2)

    def test_server_failure_returns_generic_json_not_traceback(self):
        with patch("crm.views.Deal.save", side_effect=DatabaseError("PRIVATE DATABASE DETAILS")):
            with self.assertLogs("crm.views", level="ERROR"):
                response = self.post({"stage": "won"})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json()["success"])
        self.assertNotContains(response, "PRIVATE DATABASE DETAILS", status_code=500)
        self.assertNotContains(response, "Traceback", status_code=500)
        self.own.refresh_from_db()
        self.assertEqual(self.own.stage, "new")
