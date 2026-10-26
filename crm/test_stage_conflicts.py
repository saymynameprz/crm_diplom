from datetime import timedelta
from decimal import Decimal, InvalidOperation
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .forms import DealForm
from .models import Client, Deal, DealQuerySet
from .views import DealChangeStageView


class StageConflictTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("version-manager")
        cls.other = User.objects.create_user("version-other")
        cls.contact = Client.objects.create(full_name="Вымышленный клиент версий")
        cls.deal = Deal.objects.create(client=cls.contact, title="Карточка версии", amount=Decimal("0.10"), assigned_to=cls.manager)

    def setUp(self):
        self.client.force_login(self.manager)
        self.url = reverse("deal_change_stage", kwargs={"pk": self.deal.pk})
        self.version = self.deal.updated_at.isoformat()

    def post(self, stage, version=None, json=True, **extra):
        return self.client.post(self.url, {"stage": stage, "expected_updated_at": version or self.version, **extra},
                                HTTP_ACCEPT="application/json" if json else "text/html")

    def test_two_stale_tabs_conflict_then_fresh_version_succeeds(self):
        self.assertEqual(self.post("contact").status_code, 200)
        before = Deal.objects.values().get(pk=self.deal.pk)
        response = self.post("won")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])
        self.assertNotIn("columns", response.json())
        self.assertEqual(Deal.objects.values().get(pk=self.deal.pk), before)
        self.assertEqual(self.post("won", version=before["updated_at"].isoformat()).status_code, 200)

    def test_repeat_same_post_conflicts_but_current_noop_never_writes(self):
        self.assertEqual(self.post("contact").status_code, 200)
        self.assertEqual(self.post("contact").status_code, 409)
        before = Deal.objects.values().get(pk=self.deal.pk)
        for _ in range(2):
            with CaptureQueriesContext(connection) as queries:
                response = self.post("contact", version=before["updated_at"].isoformat())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(any(q["sql"].startswith("UPDATE") for q in queries))
            self.assertEqual(Deal.objects.values().get(pk=self.deal.pk), before)

    def test_aba_and_equal_clock_are_detected(self):
        with patch("crm.views.timezone.now", return_value=self.deal.updated_at):
            self.assertEqual(self.post("contact").status_code, 200)
            current = Deal.objects.get(pk=self.deal.pk)
            self.assertGreater(current.updated_at, self.deal.updated_at)
            self.assertEqual(self.post("new", version=current.updated_at.isoformat()).status_code, 200)
        self.assertEqual(self.post("won").status_code, 409)
        self.assertEqual(Deal.objects.get(pk=self.deal.pk).stage, "new")

    def test_html_conflict_is_generic_with_fixed_refresh_link(self):
        self.post("contact")
        response = self.post("won", json=False, next="https://untrusted.example/")
        self.assertEqual(response.status_code, 409)
        self.assertTemplateUsed(response, "crm/deal_stage_conflict.html")
        self.assertContains(response, 'href="/deals/kanban/"', status_code=409)
        self.assertNotContains(response, "untrusted.example", status_code=409)

    def test_missing_invalid_or_forged_version_and_stage_do_not_change_data(self):
        before = Deal.objects.values().get(pk=self.deal.pk)
        for payload in ({"stage": "won"}, {"stage": "won", "expected_stage": "new"},
                        {"stage": "won", "expected_updated_at": "invalid"},
                        {"stage": "invalid", "expected_updated_at": self.version}):
            response = self.client.post(self.url, payload, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post("won", version=(self.deal.updated_at + timedelta(days=1)).isoformat()).status_code, 409)
        self.assertEqual(Deal.objects.values().get(pk=self.deal.pk), before)
        # expected_stage is not a second lock or a way to bypass the required version.
        self.assertEqual(self.post("contact", expected_stage="lost").status_code, 200)

    def test_reassignment_between_read_and_update_or_noop_is_denied(self):
        original = DealChangeStageView.get_object
        for stage in ("contact", "new"):
            Deal.objects.filter(pk=self.deal.pk).update(assigned_to=self.manager)
            def read_then_reassign(view):
                deal = original(view)
                Deal.objects.filter(pk=deal.pk).update(assigned_to=self.other)
                return deal
            with patch.object(DealChangeStageView, "get_object", autospec=True, side_effect=read_then_reassign):
                response = self.post(stage)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(self.other.username, response.content.decode())
            record = Deal.objects.get(pk=self.deal.pk)
            self.assertEqual((record.stage, record.updated_at, record.assigned_to), ("new", self.deal.updated_at, self.other))

    def test_update_sql_has_version_stage_and_owner_in_where(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.post("contact").status_code, 200)
        updates = [q["sql"] for q in queries if q["sql"].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        where = updates[0].split(" WHERE ")[1]
        for predicate in ('"assigned_to_id" =', '"stage" =', '"updated_at" =', '"id" ='):
            self.assertIn(predicate, where)

    def test_other_writer_between_read_and_cas_cannot_be_overwritten(self):
        original = DealQuerySet.update
        def competing_update(queryset, **kwargs):
            original(Deal.objects.filter(pk=self.deal.pk), stage="proposal", updated_at=self.deal.updated_at + timedelta(seconds=1))
            return original(queryset, **kwargs)
        with patch.object(DealQuerySet, "update", competing_update):
            self.assertEqual(self.post("won").status_code, 409)
        self.assertEqual(Deal.objects.get(pk=self.deal.pk).stage, "proposal")

    def test_deleted_between_read_and_write_is_generic_404(self):
        original = DealChangeStageView.get_object
        def read_then_delete(view):
            deal = original(view)
            Deal.objects.filter(pk=deal.pk).delete()
            return deal
        with patch.object(DealChangeStageView, "get_object", autospec=True, side_effect=read_then_delete):
            self.assertEqual(self.post("won").status_code, 404)

    def test_fragments_and_initial_forms_have_current_iso_version(self):
        response = self.client.get(reverse("deal_kanban"))
        self.assertContains(response, f'name="expected_updated_at" value="{self.version}"')
        data = self.post("contact").json()
        current = Deal.objects.get(pk=self.deal.pk)
        self.assertIn(current.updated_at.isoformat(), ''.join(c["html"] for c in data["columns"]))

    def test_normal_form_returns_decimal_and_rejects_overflow(self):
        data = {"client": self.contact.pk, "title": "Проверка Decimal", "amount": "0.10"}
        form = DealForm(data, user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsInstance(form.cleaned_data["amount"], Decimal)
        self.assertEqual(form.cleaned_data["amount"], Decimal("0.10"))
        overflow = DealForm({**data, "amount": "10000000000.00"}, user=self.manager)
        self.assertFalse(overflow.is_valid())
        self.assertIn("amount", overflow.errors)

    @skipUnless(connection.vendor == "sqlite", "Demonstrates SQLite storage, not a cross-database guarantee")
    def test_sqlite_direct_orm_overflow_is_stored_but_cannot_be_read_as_decimal12(self):
        # Model.save does not invoke full_clean; SQLite has no max_digits constraint.
        record = Deal.objects.create(client=self.contact, title="Изолированный overflow", amount=Decimal("10000000000.00"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT amount FROM crm_deal WHERE id = %s", [record.pk])
            self.assertEqual(cursor.fetchone()[0], 10000000000)
        with self.assertRaises(InvalidOperation):
            record.refresh_from_db()
