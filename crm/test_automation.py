from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as TestClient, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientRequestForm, TaskForm
from .models import Client, ClientRequest, Task
from .services import AutomationError, create_request_with_task, ensure_automatic_task

AT = datetime(2026, 10, 26, 12, 0, tzinfo=datetime_timezone.utc)
MIGRATION = "0004_task_is_automatic_alter_task_title_and_more"


class AutomationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("automation-manager")
        cls.other = User.objects.create_user("automation-other")
        cls.admin = User.objects.create_user("automation-admin", is_superuser=True)
        cls.inactive = User.objects.create_user("automation-inactive", is_active=False)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент автоматизации")

    def setUp(self):
        self.client.force_login(self.manager)

    def data(self, **overrides):
        return {"client": self.contact.pk, "title": "Учебная заявка", "status": "new", **overrides}

    def test_interface_creation_for_both_roles(self):
        for actor, owner in ((self.manager, self.manager), (self.admin, self.other)):
            with self.subTest(actor=actor.username), patch("crm.services.timezone.now", return_value=AT):
                self.client.force_login(actor)
                response = self.client.post(reverse("request_create"), self.data(assigned_to=owner.pk), follow=True)
                linked = ClientRequest.objects.latest("pk")
                self.assertContains(response, "Заявка создана.")
                self.assertEqual(linked.tasks.count(), 1)
                task = linked.tasks.get()
                self.assertTrue(task.is_automatic)
                self.assertEqual(task.title, f"Обработать заявку №{linked.pk}: {linked.title}")
                self.assertEqual(task.request, linked)
                self.assertEqual(task.assigned_to, owner)
                self.assertEqual(task.status, "new")
                self.assertIsNone(task.completed_at)
                self.assertEqual(task.due_at, AT + timedelta(hours=24))

    def test_explicit_due_date_is_copied(self):
        response = self.client.post(reverse("request_create"), self.data(due_at="2028-02-15T12:30"))
        self.assertEqual(response.status_code, 302)
        linked = ClientRequest.objects.get()
        self.assertEqual(linked.tasks.get().due_at, linked.due_at)
        self.assertTrue(timezone.is_aware(linked.tasks.get().due_at))

    def test_manager_forged_owner_ignored_for_request_and_automatic_task(self):
        self.client.post(reverse("request_create"), self.data(assigned_to=self.other.pk))
        linked = ClientRequest.objects.get()
        self.assertEqual(linked.assigned_to, self.manager)
        self.assertEqual(linked.tasks.get().assigned_to, self.manager)

    def test_idempotent_service_preserves_existing_task(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Идемпотентная", assigned_to=self.manager)
        with patch("crm.services.timezone.now", return_value=AT):
            first = ensure_automatic_task(linked)
        first.status = "completed"
        first.save()
        before = Task.objects.values().get(pk=first.pk)
        linked.title = "Переименована"
        linked.assigned_to = self.other
        linked.due_at = AT + timedelta(days=5)
        linked.save()
        second = ensure_automatic_task(linked)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(Task.objects.values().get(pk=first.pk), before)

    def test_database_constraint_blocks_duplicates_but_allows_manual_tasks(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Ограничение", assigned_to=self.manager)
        automatic = ensure_automatic_task(linked)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.create(request=linked, title="Дубликат", assigned_to=self.manager, is_automatic=True)
        for n in range(2):
            Task.objects.create(request=linked, title=f"Ручная {n}", assigned_to=self.manager)
        self.assertEqual(linked.tasks.filter(is_automatic=True).get(), automatic)
        self.assertEqual(linked.tasks.filter(is_automatic=False).count(), 2)

    def test_editing_request_does_not_create_or_sync_tasks(self):
        self.client.post(reverse("request_create"), self.data())
        linked = ClientRequest.objects.get()
        before = list(Task.objects.values())
        response = self.client.post(reverse("request_update", kwargs={"pk": linked.pk}), self.data(title="Изменено", due_at="2028-02-15T12:30"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Task.objects.values()), before)

    def test_manual_creation_remains_independent_and_origin_cannot_be_forged(self):
        self.client.post(reverse("request_create"), self.data())
        linked = ClientRequest.objects.get()
        response = self.client.post(reverse("task_create"), {
            "request": linked.pk, "title": "Дополнительная ручная", "status": "new", "is_automatic": True,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(linked.tasks.filter(is_automatic=True).count(), 1)
        self.assertFalse(Task.objects.get(title="Дополнительная ручная").is_automatic)

    def test_failure_rolls_back_service_and_http_creation(self):
        form = ClientRequestForm(self.data(), user=self.manager)
        with patch("crm.services.Task.objects.get_or_create", side_effect=IntegrityError("Test insert failure")):
            with self.assertRaises(IntegrityError):
                create_request_with_task(form)
        self.assertEqual(ClientRequest.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
        with patch("crm.services.Task.objects.get_or_create", side_effect=IntegrityError("Test insert failure")):
            with self.assertLogs("crm.views", level="ERROR"):
                response = self.client.post(reverse("request_create"), self.data(description="Введённое описание"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Данные не сохранены")
        self.assertContains(response, "Введённое описание")
        self.assertNotContains(response, "Заявка создана.")
        self.assertEqual(ClientRequest.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)

    def test_invalid_request_creates_neither_record(self):
        for data in (self.data(title=" "), self.data(client=999999), self.data(due_at="invalid")):
            response = self.client.post(reverse("request_create"), data)
            self.assertEqual(response.status_code, 200)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_create"), self.data(assigned_to=self.inactive.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClientRequest.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)

    def test_service_rejects_invalid_or_existing_form_and_inactive_owner(self):
        with self.assertRaises(ValueError):
            create_request_with_task(ClientRequestForm(self.data(title=""), user=self.manager))
        linked = ClientRequest.objects.create(client=self.contact, title="Старая", assigned_to=self.inactive)
        with self.assertRaises(AutomationError):
            ensure_automatic_task(linked)
        with self.assertRaises(ValueError):
            create_request_with_task(ClientRequestForm(self.data(), instance=linked, user=self.manager))
        self.assertEqual(Task.objects.count(), 0)

    def test_raw_orm_does_not_run_automation(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Старая", assigned_to=self.manager)
        linked.save()
        self.assertEqual(Task.objects.count(), 0)

    def test_long_automatic_title_preserved_and_editable(self):
        self.client.post(reverse("request_create"), self.data(title="x" * 200))
        linked = ClientRequest.objects.get()
        task = linked.tasks.get()
        self.assertEqual(task.title, f"Обработать заявку №{linked.pk}: {'x' * 200}")
        self.assertGreater(len(task.title), 200)
        self.assertLessEqual(len(task.title), 255)
        task.full_clean()
        other_request = ClientRequest.objects.create(client=self.contact, title="Другая", assigned_to=self.manager)
        response = self.client.post(reverse("task_update", kwargs={"pk": task.pk}), {
            "request": other_request.pk, "title": task.title, "status": "in_progress", "is_automatic": False,
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.request, linked)
        self.assertTrue(task.is_automatic)
        self.assertEqual(task.status, "in_progress")
        form = TaskForm({"request": linked.pk, "title": "x" * 201, "status": "new"}, user=self.manager)
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_creation_csrf_and_anonymous_access(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.manager)
        browser.get(reverse("request_create"))
        for token in (None, "x" * 64):
            data = self.data()
            if token:
                data["csrfmiddlewaretoken"] = token
            self.assertEqual(browser.post(reverse("request_create"), data).status_code, 403)
        self.assertEqual(Task.objects.count(), 0)
        response = browser.post(reverse("request_create"), self.data(csrfmiddlewaretoken=browser.cookies["csrftoken"].value))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Task.objects.count(), 1)
        browser.logout()
        self.assertEqual(browser.get(reverse("home")).status_code, 302)
        browser.get(reverse("login"))
        self.assertEqual(browser.post(reverse("request_create"), self.data(csrfmiddlewaretoken=browser.cookies["csrftoken"].value)).status_code, 302)
        self.assertEqual(Task.objects.count(), 1)


class DeadlineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("deadline-manager")
        cls.other = User.objects.create_user("deadline-other")
        cls.admin = User.objects.create_user("deadline-admin", is_superuser=True)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент сроков")
        cls.linked = ClientRequest.objects.create(client=cls.contact, title="Своя заявка", assigned_to=cls.manager)

    def setUp(self):
        self.client.force_login(self.manager)

    def test_task_and_request_boundaries_python_and_sql_match(self):
        for model, parent in ((Task, {"request": self.linked}), (ClientRequest, {"client": self.contact})):
            for status in model.Status.values:
                for offset in (-1, 0, 1, None):
                    due = None if offset is None else AT + timedelta(microseconds=offset)
                    with self.subTest(model=model.__name__, status=status, offset=offset):
                        record = model.objects.create(title="Граница", status=status, due_at=due, assigned_to=self.manager, **parent)
                        expected = offset == -1 and status in ("new", "in_progress")
                        before = model.objects.values().get(pk=record.pk)
                        with patch("crm.models.timezone.now", return_value=AT):
                            self.assertEqual(record.is_overdue, expected)
                            self.assertEqual(model.objects.with_overdue().get(pk=record.pk).overdue_flag, expected)
                        self.assertEqual(model.objects.values().get(pk=record.pk), before)

    def test_task_completion_and_reopening_change_overdue_not_request(self):
        self.linked.due_at = AT + timedelta(days=1)
        self.linked.save()
        task = Task.objects.create(request=self.linked, title="Цикл", assigned_to=self.manager, due_at=AT - timedelta(hours=1))
        with patch("crm.models.timezone.now", return_value=AT):
            self.assertTrue(task.is_overdue)
            task.status = "completed"
            task.save()
            completed = task.completed_at
            self.assertFalse(task.is_overdue)
            self.assertNotContains(self.client.get(task.get_absolute_url()), "Просрочена")
            task.save()
            self.assertEqual(task.completed_at, completed)
            task.status = "in_progress"
            task.save()
            self.assertIsNone(task.completed_at)
            self.assertTrue(task.is_overdue)
            self.assertContains(self.client.get(task.get_absolute_url()), "Просрочена")
        self.linked.refresh_from_db()
        self.assertEqual(self.linked.status, "new")
        with patch("crm.models.timezone.now", return_value=AT):
            self.assertFalse(self.linked.is_overdue)
        with patch("crm.models.timezone.now", return_value=AT + timedelta(minutes=1)):
            task.status = "completed"
            task.save()
            self.assertGreater(task.completed_at, completed)
            self.assertFalse(task.is_overdue)

    def test_overdue_request_and_task_are_independent(self):
        self.linked.due_at = AT - timedelta(days=1)
        self.linked.save()
        task = Task.objects.create(request=self.linked, title="Не просрочена", assigned_to=self.manager, due_at=AT + timedelta(days=1))
        with patch("crm.models.timezone.now", return_value=AT):
            self.assertTrue(self.linked.is_overdue)
            self.assertFalse(task.is_overdue)

    def seed_records(self, model):
        parent = {"request": self.linked} if model is Task else {"client": self.contact}
        late = model.objects.create(title="Find late", assigned_to=self.manager, due_at=AT - timedelta(days=1), **parent)
        future = model.objects.create(title="Find future", assigned_to=self.manager, due_at=AT + timedelta(days=1), **parent)
        completed = model.objects.create(title="Find completed", assigned_to=self.manager, status="completed", due_at=AT - timedelta(days=1), **parent)
        model.objects.create(title="Foreign late", assigned_to=self.other, due_at=AT - timedelta(days=1), **parent)
        return late, future, completed

    def test_yes_no_combined_filters_and_scope_for_both_models(self):
        for model, route, name in ((Task, "task_list", "tasks"), (ClientRequest, "request_list", "requests")):
            late, future, completed = self.seed_records(model)
            for overdue, expected in (("yes", [late]), ("no", [completed, future])):
                with self.subTest(model=model.__name__, overdue=overdue), patch("crm.models.timezone.now", return_value=AT):
                    response = self.client.get(reverse(route), {"q": "Find", "overdue": overdue})
                    self.assertQuerySetEqual(response.context[name], expected)
                    self.assertNotContains(response, "Foreign late")
                    if overdue == "yes":
                        self.assertContains(response, "Просрочена")
            with patch("crm.models.timezone.now", return_value=AT):
                response = self.client.get(reverse(route), {"q": "Find", "overdue": "no", "status": "new", "sort": "oldest"})
                self.assertQuerySetEqual(response.context[name], [future])

    def test_overdue_pagination_retains_all_parameters(self):
        for model, route, name in ((Task, "task_list", "tasks"), (ClientRequest, "request_list", "requests")):
            parent = {"request": self.linked} if model is Task else {"client": self.contact}
            records = [model.objects.create(title=f"Page {n}", assigned_to=self.manager, due_at=AT - timedelta(days=1), **parent) for n in range(11)]
            params = {"q": "Page", "status": "new", "overdue": "yes", "sort": "oldest"}
            with patch("crm.models.timezone.now", return_value=AT):
                first = self.client.get(reverse(route), params)
                second = self.client.get(reverse(route), {**params, "page": 2})
            self.assertQuerySetEqual(first.context[name], records[:10])
            self.assertQuerySetEqual(second.context[name], records[10:])
            self.assertContains(first, "q=Page&amp;status=new&amp;overdue=yes&amp;sort=oldest&amp;page=2")

    def test_home_counters_for_manager_and_admin(self):
        self.seed_records(Task)
        self.seed_records(ClientRequest)
        # An inconsistent task is visible only to admin, despite being assigned to manager.
        foreign_request = ClientRequest.objects.create(client=self.contact, title="Переназначена", assigned_to=self.other, due_at=AT - timedelta(days=1))
        Task.objects.create(request=foreign_request, title="Старое назначение", assigned_to=self.manager, due_at=AT - timedelta(days=1))
        with patch("crm.models.timezone.now", return_value=AT):
            response = self.client.get(reverse("home"))
            expected = {"request_count": 4, "task_count": 3, "overdue_request_count": 1, "overdue_task_count": 1}
            for key, value in expected.items():
                self.assertEqual(response.context[key], value)
            self.assertContains(response, 'id="overdue-task-count">1</dd>')
            self.client.force_login(self.admin)
            response = self.client.get(reverse("home"))
            expected = {"request_count": 6, "task_count": 5, "overdue_request_count": 3, "overdue_task_count": 3}
            for key, value in expected.items():
                self.assertEqual(response.context[key], value)
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("home")), "/login/?next=/")

    def test_reassignment_filters_and_counts_keep_both_managers_safe(self):
        automatic = ensure_automatic_task(self.linked)
        automatic.due_at = AT - timedelta(days=1)
        automatic.save()
        self.linked.assigned_to = self.other
        self.linked.due_at = AT - timedelta(days=1)
        self.linked.save()
        for user, expected_requests in ((self.manager, 0), (self.other, 1)):
            self.client.force_login(user)
            with patch("crm.models.timezone.now", return_value=AT):
                response = self.client.get(reverse("home"))
                self.assertEqual(response.context["request_count"], expected_requests)
                self.assertEqual(response.context["task_count"], 0)
                self.assertEqual(response.context["overdue_task_count"], 0)
                response = self.client.get(reverse("task_list"), {"overdue": "yes"})
                self.assertNotContains(response, automatic.title)
            self.assertEqual(self.client.get(automatic.get_absolute_url()).status_code, 404)
        self.client.force_login(self.admin)
        with patch("crm.models.timezone.now", return_value=AT):
            self.assertEqual(self.client.get(reverse("home")).context["overdue_task_count"], 1)
        response = self.client.post(reverse("task_update", kwargs={"pk": automatic.pk}), {
            "request": self.linked.pk, "title": automatic.title, "status": "new", "assigned_to": self.other.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(automatic.get_absolute_url()).status_code, 200)

    def test_deadline_reads_do_not_modify_database_or_status_choices(self):
        task = Task.objects.create(request=self.linked, title="Без записи", assigned_to=self.manager, due_at=AT - timedelta(days=1))
        before = Task.objects.values().get(pk=task.pk)
        with patch("crm.models.timezone.now", return_value=AT):
            self.client.get(reverse("home"))
            self.client.get(reverse("task_list"), {"overdue": "yes"})
            self.client.get(task.get_absolute_url())
        self.assertEqual(Task.objects.values().get(pk=task.pk), before)
        self.assertNotIn("overdue", Task.Status.values)
        self.assertNotIn("overdue", ClientRequest.Status.values)


class AutomationMigrationTests(TransactionTestCase):
    def test_existing_records_stay_manual_and_requests_get_no_new_tasks(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("crm", "0003_task")])
            apps = executor.loader.project_state([("crm", "0003_task")]).apps
            contact = apps.get_model("crm", "Client").objects.create(full_name="Старый вымышленный клиент")
            linked = apps.get_model("crm", "ClientRequest").objects.create(client=contact, title="Старая заявка")
            task = apps.get_model("crm", "Task").objects.create(request=linked, title="Старая ручная задача", status="completed", completed_at=AT)
            old_task = apps.get_model("crm", "Task").objects.values().get(pk=task.pk)
            executor = MigrationExecutor(connection)
            executor.migrate([("crm", MIGRATION)])
            new_apps = executor.loader.project_state([("crm", MIGRATION)]).apps
            new_task_model = new_apps.get_model("crm", "Task")
            self.assertEqual(new_task_model.objects.values(*old_task).get(pk=task.pk), old_task)
            self.assertFalse(new_task_model.objects.get(pk=task.pk).is_automatic)
            self.assertEqual(new_task_model.objects.count(), 1)
            self.assertEqual(new_task_model.objects.filter(is_automatic=True).count(), 0)
            self.assertEqual(new_apps.get_model("crm", "ClientRequest").objects.count(), 1)
            self.assertEqual(new_apps.get_model("crm", "Client").objects.get(pk=contact.pk).full_name, contact.full_name)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
