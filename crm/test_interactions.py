from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import Client as TestClient, RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import ClientInteractionForm
from .models import Client, ClientInteraction, ClientRequest, Deal, Task
from .views import ClientDetailView, InteractionListView

AT = datetime(2026, 10, 26, 12, 0, tzinfo=dt_timezone.utc)


class InteractionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("history-manager")
        cls.other = User.objects.create_user("history-other")
        cls.admin = User.objects.create_user("history-admin", is_superuser=True)
        cls.inactive = User.objects.create_user("history-inactive", is_active=False)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент истории")
        cls.second = Client.objects.create(full_name="Другой учебный клиент")
        cls.own = ClientInteraction.objects.create(client=cls.contact, created_by=cls.manager, interaction_type="call", subject="Своя запись", occurred_at=AT-timedelta(days=1))
        cls.foreign = ClientInteraction.objects.create(client=cls.contact, created_by=cls.other, interaction_type="meeting", subject="Чужая запись", occurred_at=AT-timedelta(days=1))

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, action, record=None):
        return reverse("interaction_"+action, kwargs={"pk": (record or self.own).pk})

    def data(self, **extra):
        return {"client": self.contact.pk, "interaction_type": "comment", "subject": "Учебная заметка", "description": "Текст записи", "occurred_at": "2020-01-15T12:30", **extra}

    def test_both_roles_create_as_self_ignoring_forged_author(self):
        for actor in (self.manager, self.admin):
            self.client.force_login(actor)
            response = self.client.post(reverse("interaction_create"), self.data(created_by=self.other.pk), follow=True)
            record = ClientInteraction.objects.latest("pk")
            self.assertRedirects(response, record.get_absolute_url())
            self.assertContains(response, "Взаимодействие добавлено.")
            self.assertEqual(record.created_by, actor)
            self.assertEqual(record.client, self.contact)
            self.assertEqual(record.occurred_at.isoformat(), "2020-01-15T12:30:00+00:00")

    def test_all_four_types_and_default_model_time(self):
        self.assertEqual(ClientInteraction.Type.values, ["call", "meeting", "email", "comment"])
        for kind in ClientInteraction.Type.values:
            self.assertEqual(self.client.post(reverse("interaction_create"), self.data(interaction_type=kind)).status_code, 302)
            self.assertEqual(ClientInteraction.objects.latest("pk").interaction_type, kind)
        with patch("django.utils.timezone.now", return_value=AT):
            # Field.default is the original callable; default must be a fresh aware time, not import-time state.
            record = ClientInteraction(client=self.contact, created_by=self.manager, subject="Default", interaction_type="comment")
        self.assertTrue(timezone.is_aware(record.occurred_at))
        self.assertLess(abs((record.occurred_at-timezone.now()).total_seconds()), 5)

    def test_form_does_not_expose_author_or_timestamps_and_prefills_client(self):
        response = self.client.get(reverse("interaction_create"), {"client": self.second.pk})
        self.assertEqual(response.context["form"].initial["client"], str(self.second.pk))
        for name in ("created_by", "created_at", "updated_at"):
            self.assertNotIn(name, response.context["form"].fields)
            self.assertNotContains(response, f'name="{name}"')
        self.assertContains(response, "UTC")

    def test_required_trimmed_subject_and_length_limits(self):
        for subject in ("", " ", "\t\n", "x"*201):
            form = ClientInteractionForm(self.data(subject=subject), user=self.manager)
            self.assertFalse(form.is_valid())
            self.assertIn("subject", form.errors)
        for description in ("", "x"*5000):
            form = ClientInteractionForm(self.data(subject="x"*200, description=description), user=self.manager)
            self.assertTrue(form.is_valid(), form.errors)
        form = ClientInteractionForm(self.data(subject="  Trimmed  ", description="x"*5001), user=self.manager)
        self.assertFalse(form.is_valid())
        self.assertIn("description", form.errors)
        self.assertEqual(form.cleaned_data["subject"], "Trimmed")

    def test_invalid_type_client_and_date_preserve_entered_data(self):
        for field, value in (("interaction_type", "invalid"), ("interaction_type", ""), ("client", 999999), ("client", ""),
                             ("occurred_at", "invalid"), ("occurred_at", "2026-02-30T10:00"), ("occurred_at", "")):
            response = self.client.post(reverse("interaction_create"), self.data(**{field:value}))
            self.assertEqual(response.status_code, 200)
            self.assertIn(field, response.context["form"].errors)
            self.assertContains(response, "Текст записи")
        self.assertEqual(ClientInteraction.objects.count(), 2)

    def test_aware_current_past_and_future_time_boundary(self):
        with patch("crm.models.timezone.now", return_value=AT):
            for delta, valid in ((-1, True), (0, True), (1, False)):
                date = AT+timedelta(microseconds=delta)
                form = ClientInteractionForm(self.data(occurred_at=date.isoformat()), user=self.manager)
                self.assertEqual(form.is_valid(), valid, form.errors)
                if valid:self.assertTrue(timezone.is_aware(form.save().occurred_at))
                else:self.assertIn("occurred_at", form.errors)
        naive = ClientInteraction(client=self.contact, created_by=self.manager, interaction_type="comment", subject="Naive", occurred_at=AT.replace(tzinfo=None))
        with self.assertRaises(ValidationError):naive.full_clean()

    def test_future_rejected_on_edit_without_mutation(self):
        before = ClientInteraction.objects.values().get(pk=self.own.pk)
        with patch("crm.models.timezone.now", return_value=AT):
            response = self.client.post(self.url("update"), self.data(occurred_at=(AT+timedelta(seconds=1)).isoformat()))
        self.assertIn("occurred_at", response.context["form"].errors)
        self.assertEqual(ClientInteraction.objects.values().get(pk=self.own.pk), before)

    def test_manager_list_detail_and_client_hide_foreign_history(self):
        for url in (reverse("interaction_list"), self.contact.get_absolute_url()):
            response = self.client.get(url)
            self.assertContains(response, self.own.subject)
            self.assertNotContains(response, self.foreign.subject)
        self.assertEqual(self.client.get(self.url("detail")).status_code, 200)
        self.assertNotContains(self.client.get(self.url("detail")), '>Удалить</a>')

    def test_admin_sees_all_and_can_edit_foreign_preserving_author(self):
        self.client.force_login(self.admin)
        for url in (reverse("interaction_list"), self.contact.get_absolute_url()):
            self.assertContains(self.client.get(url), self.foreign.subject)
        response = self.client.post(self.url("update", self.foreign), self.data(created_by=self.admin.pk), follow=True)
        self.assertContains(response, "Взаимодействие сохранено.")
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.created_by, self.other)
        self.assertEqual(self.foreign.subject, "Учебная заметка")

    def test_manager_edits_own_record_and_cannot_forge_metadata(self):
        created, updated = self.own.created_at, self.own.updated_at
        response = self.client.post(self.url("update"), self.data(client=self.second.pk, interaction_type="email", created_by=self.other.pk,
                                   created_at="2000-01-01", updated_at="2000-01-01"))
        self.assertEqual(response.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual((self.own.client, self.own.interaction_type, self.own.created_by), (self.second, "email", self.manager))
        self.assertEqual(self.own.created_at, created)
        self.assertGreater(self.own.updated_at, updated)

    def test_foreign_urls_all_404_and_data_unchanged(self):
        before = list(ClientInteraction.objects.values())
        for action in ("detail", "update", "delete"):
            self.assertEqual(self.client.get(self.url(action, self.foreign)).status_code, 404)
        for action in ("update", "delete"):
            self.assertEqual(self.client.post(self.url(action, self.foreign), self.data()).status_code, 404)
        self.assertEqual(list(ClientInteraction.objects.values()), before)

    def test_anonymous_and_inactive_cannot_read_or_mutate(self):
        for actor in (None, self.inactive):
            self.client.logout()
            if actor:self.client.force_login(actor)
            urls = [reverse("interaction_list"), reverse("interaction_create")]+[self.url(a) for a in ("detail","update","delete")]
            for url in urls:
                for method in (self.client.get,self.client.post):
                    response = method(url, self.data())
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith('/login/?next='))
        self.assertEqual(ClientInteraction.objects.count(), 2)
        self.assertFalse(ClientInteraction.objects.visible_to(AnonymousUser()).exists())
        self.assertFalse(ClientInteraction.objects.visible_to(self.inactive).exists())

    def test_delete_own_forbidden_even_staff_foreign_stays_404(self):
        for staff in (False, True):
            self.manager.is_staff=staff;self.manager.save()
            for method in (self.client.get,self.client.post):
                self.assertEqual(method(self.url("delete")).status_code, 403)
                self.assertEqual(method(self.url("delete",self.foreign)).status_code, 404)
        self.assertEqual(ClientInteraction.objects.count(), 2)

    def test_admin_delete_get_confirms_post_deletes_only_history(self):
        self.client.force_login(self.admin)
        request = ClientRequest.objects.create(client=self.contact, title="Сохранить заявку", assigned_to=self.manager)
        Task.objects.create(request=request, title="Сохранить задачу", assigned_to=self.manager)
        Deal.objects.create(client=self.contact, title="Сохранить сделку", amount=Decimal("1.00"), assigned_to=self.manager)
        before = {m:list(m.objects.values()) for m in (Client,ClientRequest,Task,Deal)}
        self.assertContains(self.client.get(self.url("delete")), "Это действие нельзя отменить")
        self.assertTrue(ClientInteraction.objects.filter(pk=self.own.pk).exists())
        self.assertRedirects(self.client.post(self.url("delete")), reverse("interaction_list"))
        self.assertFalse(ClientInteraction.objects.filter(pk=self.own.pk).exists())
        for model, rows in before.items():self.assertEqual(list(model.objects.values()), rows)

    def test_csrf_for_all_mutations_missing_invalid_and_valid(self):
        browser = TestClient(enforce_csrf_checks=True);browser.force_login(self.admin)
        for url in (reverse("interaction_create"),self.url("update"),self.url("delete")):
            browser.get(url)
            before = list(ClientInteraction.objects.values())
            for token in (None,"x"*64):
                data = self.data()
                if token:data["csrfmiddlewaretoken"]=token
                self.assertEqual(browser.post(url,data).status_code,403)
                self.assertEqual(list(ClientInteraction.objects.values()),before)
            response=browser.post(url,self.data(csrfmiddlewaretoken=browser.cookies['csrftoken'].value))
            self.assertEqual(response.status_code,302)

    def test_client_and_author_protect_with_friendly_client_error(self):
        for record in (self.contact,self.manager):
            with self.assertRaises(ProtectedError):record.delete()
        self.client.force_login(self.admin)
        response=self.client.post(reverse("client_delete",kwargs={"pk":self.contact.pk}))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"история взаимодействий")
        self.assertContains(response,"Данные сохранены")
        self.assertEqual(ClientInteraction.objects.count(),2)

    def test_record_creation_does_not_modify_other_models_or_overdue_counts(self):
        before={m:list(m.objects.values()) for m in (Client,ClientRequest,Task,Deal)}
        counts=self.client.get(reverse("home")).context
        expected={k:counts[k] for k in ("request_count","task_count","overdue_request_count","overdue_task_count")}
        self.assertEqual(self.client.post(reverse("interaction_create"),self.data()).status_code,302)
        for model,rows in before.items():self.assertEqual(list(model.objects.values()),rows)
        home=self.client.get(reverse("home")).context
        self.assertEqual({k:home[k] for k in expected},expected)

    def test_search_subject_or_client_and_combined_scoped_filters(self):
        second=ClientInteraction.objects.create(client=self.second,created_by=self.manager,interaction_type="email",subject="Search match")
        for query,expected in (({"q":"Search"},[second]),({"q":self.second.full_name},[second]),
                ({"interaction_type":"call"},[self.own]),({"client":self.second.pk},[second]),
                ({"q":"Search","client":self.second.pk,"interaction_type":"email"},[second]),
                ({"q":"Чужая"},[]),({"interaction_type":"meeting","client":self.contact.pk},[])):
            response=self.client.get(reverse("interaction_list"),query)
            self.assertQuerySetEqual(response.context["interactions"],expected)
            self.assertNotContains(response,self.foreign.subject)

    def test_invalid_filters_and_missing_ids_do_not_error_or_leak(self):
        for query in ({"interaction_type":"bad"},{"client":"bad"},{"client":999999},{"sort":"bad"},{"q":"x"*201}):
            response=self.client.get(reverse("interaction_list"),query)
            self.assertEqual(response.status_code,200)
            self.assertQuerySetEqual(response.context["interactions"],[])
            self.assertTrue(response.context["filter_form"].errors)
            self.assertNotContains(response,self.foreign.subject)
        for action in ("detail","update","delete"):
            self.assertEqual(self.client.get(reverse('interaction_'+action,kwargs={'pk':999999})).status_code,404)

    def test_sort_ties_pagination_and_parameters(self):
        records=[ClientInteraction.objects.create(client=self.contact,created_by=self.manager,interaction_type="call",subject=f"Page {n}",occurred_at=AT-timedelta(days=2)) for n in range(12)]
        params={"q":"Page","interaction_type":"call","client":self.contact.pk,"sort":"oldest"}
        first=self.client.get(reverse("interaction_list"),params)
        second=self.client.get(reverse("interaction_list"),{**params,"page":2})
        self.assertQuerySetEqual(first.context["interactions"],records[:10])
        self.assertQuerySetEqual(second.context["interactions"],records[10:])
        self.assertContains(first,f'q=Page&amp;interaction_type=call&amp;client={self.contact.pk}&amp;sort=oldest&amp;page=2')
        self.assertQuerySetEqual(self.client.get(reverse("interaction_list"),{"q":"Page"}).context["interactions"],list(reversed(records))[:10])
        self.assertEqual(self.client.get(reverse("interaction_list"),{"page":"bad"}).status_code,404)

    def test_empty_and_no_result_states(self):
        self.assertContains(self.client.get(reverse("interaction_list"),{"q":"nothing"}),"Доступных взаимодействий не найдено")
        self.assertContains(self.client.get(self.second.get_absolute_url()),"Нет доступной истории взаимодействий")

    def test_client_history_top10_order_link_and_preserved_old_sections(self):
        records=[ClientInteraction.objects.create(client=self.contact,created_by=self.manager,interaction_type="comment",subject=f"Recent {n}",occurred_at=AT) for n in range(12)]
        request=ClientRequest.objects.create(client=self.contact,title="Старая заявка",assigned_to=self.manager)
        deal=Deal.objects.create(client=self.contact,title="Старая сделка",amount=Decimal("1.00"),assigned_to=self.manager)
        response=self.client.get(self.contact.get_absolute_url())
        self.assertEqual(response.context['client_interactions'],list(reversed(records))[:10])
        self.assertTrue(response.context['has_more_interactions'])
        self.assertContains(response,f'href="/interactions/?client={self.contact.pk}"')
        self.assertContains(response,request.title);self.assertContains(response,deal.title)
        self.assertNotContains(response,self.foreign.subject)

    def test_foreign_counts_do_not_influence_client_overflow_link(self):
        for n in range(12):ClientInteraction.objects.create(client=self.contact,created_by=self.other,interaction_type="call",subject=f"Hidden {n}")
        response=self.client.get(self.contact.get_absolute_url())
        self.assertEqual(response.context['client_interactions'],[self.own])
        self.assertFalse(response.context['has_more_interactions'])
        self.assertNotContains(response,"Вся история этого клиента")
        self.client.force_login(self.admin)
        self.assertTrue(self.client.get(self.contact.get_absolute_url()).context['has_more_interactions'])

    def test_list_and_client_history_use_bounded_queries_without_n_plus_one(self):
        for n in range(12):
            owner=User.objects.create_user(f"history-query-{n}")
            client=Client.objects.create(full_name=f"Клиент запроса {n}")
            ClientInteraction.objects.create(client=client,created_by=owner,interaction_type="call",subject=f"Query {n}")
            ClientInteraction.objects.create(client=self.contact,created_by=owner,interaction_type="call",subject=f"Client query {n}")
        for view,url,total,limit in ((InteractionListView,reverse('interaction_list'),3,'LIMIT 10'),(ClientDetailView,self.contact.get_absolute_url(),4,'LIMIT 11')):
            request=RequestFactory().get(url);request.user=self.admin
            with CaptureQueriesContext(connection) as queries:
                response=view.as_view()(request,**({'pk':self.contact.pk} if view is ClientDetailView else {}));response.render()
            self.assertEqual(len(queries),total,[q['sql'] for q in queries])
            self.assertEqual(sum(limit in q['sql'] for q in queries),1)

    def test_description_and_subject_are_escaped_and_unsupported_methods_fail(self):
        self.own.subject='<script>alert("test")</script>';self.own.description='<script>secret()</script>';self.own.save()
        for url in (self.url("detail"),reverse("interaction_list"),self.contact.get_absolute_url()):
            response=self.client.get(url);self.assertContains(response,"&lt;script&gt;");self.assertNotContains(response,"<script>")
        for url in (reverse('interaction_create'),self.url('update'),self.url('delete')):
            self.assertEqual(self.client.put(url,self.data()).status_code,405)
        self.assertEqual(str(self.own),self.own.subject)


class InteractionMigrationTests(TransactionTestCase):
    def test_upgrade_after6c_preserves_old_records_and_kanban_cas(self):
        executor=MigrationExecutor(connection)
        try:
            executor.migrate([('crm','0005_deal')])
            apps=executor.loader.project_state([('crm','0005_deal')]).apps
            contact=apps.get_model('crm','Client').objects.create(full_name='Старый вымышленный клиент')
            linked=apps.get_model('crm','ClientRequest').objects.create(client=contact,title='Старая заявка')
            apps.get_model('crm','Task').objects.create(request=linked,title='Старая задача')
            deal=apps.get_model('crm','Deal').objects.create(client=contact,title='Старая сделка',amount=Decimal('0.10'))
            before={name:list(apps.get_model('crm',name).objects.values()) for name in ('Client','ClientRequest','Task','Deal')}
            executor=MigrationExecutor(connection);executor.migrate([('crm','0006_clientinteraction')])
            apps=executor.loader.project_state([('crm','0006_clientinteraction')]).apps
            for name,rows in before.items():self.assertEqual(list(apps.get_model('crm',name).objects.values()),rows)
            self.assertEqual(apps.get_model('crm','ClientInteraction').objects.count(),0)
            admin=User.objects.create_user('migration-history-admin',is_superuser=True)
            self.client.force_login(admin)
            data={'stage':'contact','expected_updated_at':deal.updated_at.isoformat()}
            url=reverse('deal_change_stage',kwargs={'pk':deal.pk})
            self.assertEqual(self.client.post(url,data,HTTP_ACCEPT='application/json').status_code,200)
            self.assertEqual(self.client.post(url,data,HTTP_ACCEPT='application/json').status_code,409)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
