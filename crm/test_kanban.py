from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client as TestClient, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Client, Deal
from .views import DealKanbanView


class KanbanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("kanban-admin", is_superuser=True)
        cls.manager = User.objects.create_user("kanban-manager")
        cls.other = User.objects.create_user("kanban-other")
        cls.contact = Client.objects.create(full_name="Вымышленный клиент доски")
        cls.own = Deal.objects.create(client=cls.contact, title="Своя карточка", amount=Decimal("1234.56"), assigned_to=cls.manager,
                                     expected_close_date="2028-01-15")
        cls.foreign = Deal.objects.create(client=cls.contact, title="Чужая карточка", amount=Decimal("200.01"), assigned_to=cls.other)
        cls.orphan = Deal.objects.create(client=cls.contact, title="Без ответственного", amount=Decimal("1.00"), stage="won")

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, deal=None):
        return reverse("deal_change_stage", kwargs={"pk": (deal or self.own).pk})

    def version(self, deal=None):
        return Deal.objects.get(pk=(deal or self.own).pk).updated_at.isoformat()

    def test_five_ordered_columns_manager_scope_and_counts(self):
        response = self.client.get(reverse("deal_kanban"))
        self.assertEqual(response.status_code, 200)
        columns = response.context["columns"]
        self.assertEqual([c["stage"] for c in columns], Deal.Stage.values)
        self.assertEqual([c["label"] for c in columns], Deal.Stage.labels)
        self.assertEqual([c["count"] for c in columns], [1, 0, 0, 0, 0])
        ids = [d.pk for col in columns for d in col["deals"]]
        self.assertEqual(ids, [self.own.pk])
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, self.foreign.title)
        self.assertNotContains(response, self.orphan.title)
        self.assertContains(response, "В этом этапе сделок нет.", count=4)

    def test_admin_sees_everyone_including_orphans(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("deal_kanban"))
        self.assertEqual([c["count"] for c in response.context["columns"]], [2, 0, 0, 1, 0])
        for deal in (self.own, self.foreign, self.orphan):
            self.assertContains(response, deal.title)
        self.assertContains(response, "Не назначен")
        self.assertEqual(response.context["columns"][3]["deals"], [self.orphan])

    def test_card_contains_real_fields_links_and_accessible_form(self):
        response = self.client.get(reverse("deal_kanban"))
        self.assertContains(response, self.contact.full_name)
        self.assertContains(response, "1234,56 ₽")
        self.assertContains(response, self.manager.username)
        self.assertContains(response, "15.01.2028")
        self.assertContains(response, f'href="{self.own.get_absolute_url()}"')
        self.assertContains(response, f'for="deal-stage-{self.own.pk}"')
        self.assertContains(response, f'id="deal-stage-{self.own.pk}"')
        self.assertContains(response, f'action="{self.url()}"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, f'aria-label="Переместить сделку №{self.own.pk}"')

    def test_limit_20_full_counts_and_stable_tie_order(self):
        for n in range(25):
            Deal.objects.create(client=self.contact, title=f"Лимит {n}", amount=Decimal("1.00"), assigned_to=self.manager)
        # All dates match: the newest PK is the stable secondary descending key.
        Deal.objects.filter(assigned_to=self.manager).update(created_at=self.own.created_at)
        response = self.client.get(reverse("deal_kanban"))
        column = response.context["columns"][0]
        expected = list(Deal.objects.filter(assigned_to=self.manager).order_by("-pk")[:20])
        self.assertEqual(column["count"], 26)
        self.assertEqual(column["deals"], expected)
        self.assertEqual(len(column["deals"]), 20)
        self.assertNotContains(response, self.own.title)
        self.assertContains(response, 'href="/deals/?stage=new">Посмотреть все</a>')
        self.assertContains(self.client.get(reverse("deal_list"), {"stage": "new"}), "Лимит")

    def test_six_queries_no_n_plus_one_and_sql_limits(self):
        for n in range(8):
            Deal.objects.create(client=self.contact, title=f"Запрос {n}", amount=Decimal("1.00"), assigned_to=self.manager, stage="proposal")
        request = RequestFactory().get(reverse("deal_kanban"))
        request.user = self.manager
        with CaptureQueriesContext(connection) as queries:
            response = DealKanbanView.as_view()(request)
            response.render()
        self.assertEqual(len(queries), 6)
        self.assertEqual(sum("LIMIT 20" in q["sql"] for q in queries), 5)
        self.assertEqual(len(response.context_data["columns"]), 5)

    def test_no_all_link_when_at_most_20(self):
        self.assertNotContains(self.client.get(reverse("deal_kanban")), "Посмотреть все")

    def test_admin_and_manager_can_move_across_all_stages(self):
        for actor, target in ((self.manager, self.own), (self.admin, self.foreign), (self.admin, self.orphan)):
            self.client.force_login(actor)
            for stage in Deal.Stage.values:
                response = self.client.post(self.url(target), {"stage": stage, "expected_updated_at": self.version(target)}, follow=True)
                self.assertRedirects(response, reverse("deal_kanban"))
                target.refresh_from_db()
                self.assertEqual(target.stage, stage)
                column = next(c for c in response.context["columns"] if c["stage"] == stage)
                self.assertIn(target, column["deals"])

    def test_new_won_contact_updates_counts_from_database(self):
        for stage in ("won", "contact"):
            response = self.client.post(self.url(), {"stage": stage, "expected_updated_at": self.version()}, follow=True)
            self.assertContains(response, "Сделка перемещена.")
            self.own.refresh_from_db()
            self.assertEqual(self.own.stage, stage)
            columns = response.context["columns"]
            for col in columns:
                self.assertEqual(col["count"], Deal.objects.visible_to(self.manager).filter(stage=col["stage"]).count())
            self.assertEqual(sum(d.pk == self.own.pk for c in columns for d in c["deals"]), 1)

    def test_only_stage_and_updated_at_can_change_forged_fields_ignored(self):
        before = Deal.objects.values().get(pk=self.own.pk)
        at = self.own.updated_at + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=at):
            response = self.client.post(self.url(), {"stage": "won", "expected_updated_at": self.version(), "assigned_to": self.other.pk, "amount": "-100",
                "client": 999999, "title": "Подмена", "description": "Подмена", "created_at": "2000-01-01", "updated_at": "2000-01-01",
                "expected_close_date": "2000-01-01", "pk": self.foreign.pk, "id": self.foreign.pk})
        self.assertEqual(response.status_code, 302)
        after = Deal.objects.values().get(pk=self.own.pk)
        self.assertEqual(after, {**before, "stage": "won", "updated_at": at})
        self.assertEqual(Deal.objects.count(), 3)

    def test_same_stage_is_noop_and_no_duplicate(self):
        before = Deal.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url(), {"stage": "new", "expected_updated_at": self.version()}, follow=True)
        self.assertContains(response, "уже находится на выбранном этапе")
        self.assertEqual(Deal.objects.values().get(pk=self.own.pk), before)
        self.assertEqual(Deal.objects.count(), 3)

    def test_invalid_and_missing_stage_400_without_mutation(self):
        before = list(Deal.objects.values())
        for data in ({}, {"stage": ""}, {"stage": "invalid", "amount": "0"}, {"stage": "<script>bad</script>"}):
            response = self.client.post(self.url(), {"expected_updated_at": self.version(), **data})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "Сделка не изменена", status_code=400)
            self.assertNotContains(response, "<script>", status_code=400)
            self.assertEqual(list(Deal.objects.values()), before)

    def test_foreign_and_nonexistent_objects_are_404_even_with_invalid_stage(self):
        before = Deal.objects.values().get(pk=self.foreign.pk)
        for data in ({"stage": "won"}, {"stage": "invalid"}):
            self.assertEqual(self.client.post(self.url(self.foreign), data).status_code, 404)
            self.assertEqual(self.client.post(reverse("deal_change_stage", kwargs={"pk": 999999}), data).status_code, 404)
        self.assertEqual(Deal.objects.values().get(pk=self.foreign.pk), before)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("deal_change_stage", kwargs={"pk": 999999}), {"stage": "won"}).status_code, 404)

    def test_get_and_other_methods_405_without_changes(self):
        before = list(Deal.objects.values())
        for method in (self.client.get, self.client.put, self.client.delete, self.client.patch):
            self.assertEqual(method(self.url(), {"stage": "won"}).status_code, 405)
        self.assertEqual(list(Deal.objects.values()), before)

    def test_anonymous_board_and_post_redirect_to_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("deal_kanban")), "/login/?next=/deals/kanban/")
        self.assertEqual(self.client.post(self.url(), {"stage": "won"}).status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.stage, "new")

    def test_csrf_missing_invalid_and_valid(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.manager)
        browser.get(reverse("deal_kanban"))
        before = Deal.objects.values().get(pk=self.own.pk)
        for data in ({"stage": "won"}, {"stage": "won", "csrfmiddlewaretoken": "x"*64}):
            self.assertEqual(browser.post(self.url(), data).status_code, 403)
            self.assertEqual(Deal.objects.values().get(pk=self.own.pk), before)
        self.assertRedirects(browser.post(self.url(), {"stage": "won", "expected_updated_at": self.version(), "csrfmiddlewaretoken": browser.cookies["csrftoken"].value}), reverse("deal_kanban"))
        self.own.refresh_from_db()
        self.assertEqual(self.own.stage, "won")

    def test_external_redirect_parameters_ignored(self):
        response = self.client.post(self.url()+"?next=https://untrusted.example/", {"stage": "won", "expected_updated_at": self.version(), "next": "//untrusted.example/", "return_url": "https://untrusted.example/"})
        self.assertEqual(response.url, reverse("deal_kanban"))

    def test_reassignment_changes_cards_counts_and_mutation_access(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("deal_update", kwargs={"pk": self.own.pk}), {
            "client": self.contact.pk, "title": self.own.title, "amount": "1234.56", "stage": "new", "assigned_to": self.other.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.manager)
        board = self.client.get(reverse("deal_kanban"))
        self.assertNotContains(board, self.own.title)
        self.assertEqual(sum(c["count"] for c in board.context["columns"]), 0)
        self.assertEqual(self.client.post(self.url(), {"stage": "won"}).status_code, 404)
        self.client.force_login(self.other)
        board = self.client.get(reverse("deal_kanban"))
        self.assertContains(board, self.own.title)
        self.assertEqual(sum(c["count"] for c in board.context["columns"]), 2)
        self.assertEqual(self.client.post(self.url(), {"stage": "won", "expected_updated_at": self.version()}).status_code, 302)

    def test_long_titles_escape_html_and_keep_alternative_links(self):
        self.own.title = '<script>alert("test")</script>' + 'Я'*150
        self.own.save()
        response = self.client.get(reverse("deal_kanban"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")
        self.assertContains(self.client.get(reverse("deal_list")), 'href="/deals/kanban/"')
        self.assertContains(self.client.get(self.own.get_absolute_url()), 'href="/deals/kanban/"')


class KanbanPresentationTests(TestCase):
    """Protect the fragment and fallback contracts used by the redesigned board."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user('layout-kanban-manager')
        cls.contact = Client.objects.create(full_name='Учебный клиент с длинным названием')
        cls.deal = Deal.objects.create(client=cls.contact, assigned_to=cls.manager,
            title='Полное название без сокращения ' + 'Я' * 120, amount=Decimal('9999999999.99'))

    def setUp(self):
        self.client.force_login(self.manager)

    def test_initial_and_replaced_columns_preserve_labeled_forms_and_versions(self):
        from .tests import ShellMarkup
        page = self.client.get(reverse('deal_kanban'))
        markup = ShellMarkup(page.content.decode())
        self.assertEqual(markup.errors, [])
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        for hook in ('kanban-feedback', 'kanban-status', 'kanban-refresh', 'kanban-dismiss'):
            self.assertIn(hook, markup.ids)
        controls = {a['id']: (tag, a) for tag, a in markup.elements if 'id' in a}
        self.assertIn('hidden', controls['kanban-refresh'][1])
        self.assertContains(page, 'class="kanban-column-header"', count=5)
        self.assertContains(page, 'class="kanban-amount">9999999999,99 ₽')
        self.assertContains(page, self.deal.title)
        self.assertContains(page, f'class="button" href="{reverse("deal_create")}"')
        response = self.client.post(reverse('deal_change_stage', args=[self.deal.pk]),
            {'stage': 'contact', 'expected_updated_at': self.deal.updated_at.isoformat()},
            HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        for column in response.json()['columns']:
            fragment = ShellMarkup(column['html'])
            self.assertEqual(fragment.errors, [])
            self.assertEqual(fragment.elements[0][0], 'section')
            self.assertEqual(fragment.elements[0][1]['data-stage'], column['stage'])
            if column['stage'] == 'contact':
                self.deal.refresh_from_db()
                inputs = fragment.forms[0]['inputs']
                self.assertEqual({a['name'] for a in inputs}, {'csrfmiddlewaretoken', 'expected_updated_at'})
                self.assertEqual(next(a['value'] for a in inputs if a['name'] == 'expected_updated_at'), self.deal.updated_at.isoformat())
                self.assertIn(('select', {'name': 'stage', 'id': f'deal-stage-{self.deal.pk}'}), fragment.elements)
                self.assertIn('kanban-drag-handle', column['html'])

    def test_error_and_conflict_keep_distinct_alerts_and_safe_recovery(self):
        from .tests import ShellMarkup
        url = reverse('deal_change_stage', args=[self.deal.pk])
        response = self.client.post(url, {'stage': 'invalid', 'expected_updated_at': 'broken'})
        self.assertEqual(response.status_code, 400)
        markup = ShellMarkup(response.content.decode())
        self.assertEqual(markup.errors, [])
        self.assertContains(response, 'message message-error', status_code=400)
        self.assertContains(response, 'aria-invalid="true"', status_code=400)
        self.assertContains(response, 'id="id_expected_updated_at_error"', status_code=400)
        form = next(f for f in markup.forms if f.get('action') == url)
        self.assertEqual(next(a['value'] for a in form['inputs'] if a['name'] == 'expected_updated_at'), 'broken')
        Deal.objects.filter(pk=self.deal.pk).update(updated_at=self.deal.updated_at + timedelta(seconds=1))
        response = self.client.post(url, {'stage': 'won', 'expected_updated_at': self.deal.updated_at.isoformat()})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'message message-warning', status_code=409)
        self.assertContains(response, 'Карточка устарела', status_code=409)
        self.assertNotContains(response, 'Сделка перемещена', status_code=409)
        self.assertContains(response, 'class="button" href="/deals/kanban/">Обновить доску', status_code=409)


class KanbanFullStageLabelTests(TestCase):
    def test_full_current_stage_is_present_initially_and_in_updated_fragment(self):
        manager = User.objects.create_user('stage-label-manager')
        contact = Client.objects.create(full_name='Учебный клиент метки')
        deal = Deal.objects.create(client=contact, title='Учебная сделка без даты', assigned_to=manager, amount=Decimal('1.00'), stage='proposal')
        self.client.force_login(manager)
        page = self.client.get(reverse('deal_kanban'))
        self.assertContains(page, 'Текущий этап: <strong>Коммерческое предложение</strong>')
        self.assertNotContains(page, 'Планируемая дата закрытия')
        response = self.client.post(reverse('deal_change_stage', args=[deal.pk]),
            {'stage': 'won', 'expected_updated_at': deal.updated_at.isoformat()}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        target = next(c for c in response.json()['columns'] if c['stage'] == 'won')
        self.assertIn('Текущий этап: <strong>Успешно завершена</strong>', target['html'])
        self.assertIn('name="expected_updated_at"', target['html'])
        self.assertIn('name="stage"', target['html'])
