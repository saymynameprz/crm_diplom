from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models.deletion import ProtectedError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import TaskForm
from .models import Client, ClientRequest, Task


class TaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("task-admin", is_superuser=True)
        cls.manager = User.objects.create_user("task-manager")
        cls.other = User.objects.create_user("task-other")
        cls.inactive = User.objects.create_user("task-inactive", is_active=False)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент задач")
        cls.own_request = ClientRequest.objects.create(client=cls.contact, title="Своя заявка задач", assigned_to=cls.manager)
        cls.foreign_request = ClientRequest.objects.create(client=cls.contact, title="Чужая заявка задач", assigned_to=cls.other)
        cls.own = Task.objects.create(request=cls.own_request, title="Своя задача", assigned_to=cls.manager)
        cls.foreign = Task.objects.create(request=cls.foreign_request, title="Чужая задача", assigned_to=cls.other)

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, action, task=None):
        return reverse("task_" + action, kwargs={"pk": (task or self.own).pk})

    def data(self, **overrides):
        return {"request": self.own_request.pk, "title": "Учебная задача", "status": "new", **overrides}

    def test_admin_create_and_assign(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("task_create"), self.data(assigned_to=self.manager.pk), follow=True)
        task = Task.objects.get(title="Учебная задача")
        self.assertRedirects(response, task.get_absolute_url())
        self.assertContains(response, "Задача создана.")
        self.assertEqual(task.request, self.own_request)
        self.assertEqual(task.assigned_to, self.manager)
        self.assertIsNone(task.completed_at)

    def test_manager_creation_uses_http_user_not_post(self):
        for assignee in (None, self.other.pk, self.admin.pk, self.inactive.pk, 999999):
            with self.subTest(assignee=assignee):
                data = self.data()
                if assignee is not None:
                    data["assigned_to"] = assignee
                self.assertEqual(self.client.post(reverse("task_create"), data).status_code, 302)
                self.assertEqual(Task.objects.latest("pk").assigned_to, self.manager)
        self.assertNotContains(self.client.get(reverse("task_create")), 'name="assigned_to"')

    def test_foreign_request_not_selectable_or_postable_by_manager(self):
        form = self.client.get(reverse("task_create")).context["form"]
        self.assertNotIn(self.foreign_request, form.fields["request"].queryset)
        response = self.client.post(reverse("task_create"), self.data(request=self.foreign_request.pk))
        self.assertIn("request", response.context["form"].errors)
        self.assertEqual(Task.objects.count(), 2)

    def test_admin_cannot_assign_user_different_from_request(self):
        self.client.force_login(self.admin)
        for url in (reverse("task_create"), self.url("update")):
            response = self.client.post(url, self.data(assigned_to=self.other.pk))
            self.assertIn("assigned_to", response.context["form"].errors)
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.manager)
        self.assertEqual(Task.objects.count(), 2)

    def test_admin_requires_active_assignee(self):
        self.client.force_login(self.admin)
        for assignee in (self.inactive.pk, 999999, ""):
            with self.subTest(assignee=assignee):
                response = self.client.post(reverse("task_create"), self.data(assigned_to=assignee))
                self.assertIn("assigned_to", response.context["form"].errors)
        self.assertNotIn(self.inactive, self.client.get(reverse("task_create")).context["form"].fields["assigned_to"].queryset)
        self.assertEqual(Task.objects.count(), 2)

    def test_request_without_active_owner_blocks_creation(self):
        self.client.force_login(self.admin)
        for owner in (None, self.inactive):
            with self.subTest(owner=owner):
                linked = ClientRequest.objects.create(client=self.contact, title="Нет активного ответственного", assigned_to=owner)
                response = self.client.post(reverse("task_create"), self.data(request=linked.pk, assigned_to=self.manager.pk))
                self.assertIn("request", response.context["form"].errors)
                self.assertContains(response, "Сначала назначьте активного ответственного")
        self.assertEqual(Task.objects.count(), 2)

    def test_name_required_and_trimmed(self):
        for title in ("", "  ", "\t\n"):
            response = self.client.post(reverse("task_create"), self.data(title=title))
            self.assertIn("title", response.context["form"].errors)
        form = TaskForm(self.data(title="  Trimmed  "), user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Trimmed")
        self.assertEqual(Task.objects.count(), 2)

    def test_length_limits(self):
        data = self.data(title="x" * 200, description="x" * 5000)
        form = TaskForm(data, user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        for field in ("title", "description"):
            form = TaskForm({**data, field: data[field] + "x"}, user=self.manager)
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)

    def test_nonexistent_or_missing_request_rejected(self):
        for request_id in (999999, ""):
            response = self.client.post(reverse("task_create"), self.data(request=request_id))
            self.assertIn("request", response.context["form"].errors)
        self.assertEqual(Task.objects.count(), 2)

    def test_invalid_status_and_date_preserve_data(self):
        before = Task.objects.values().get(pk=self.own.pk)
        response = self.client.post(self.url("update"), self.data(status="cancelled", due_at="bad", description="Сохранить введённый текст"))
        self.assertIn("status", response.context["form"].errors)
        self.assertIn("due_at", response.context["form"].errors)
        self.assertContains(response, "Сохранить введённый текст")
        self.assertEqual(Task.objects.values().get(pk=self.own.pk), before)

    def test_optional_fields_and_past_future_dates(self):
        for due in ("", "2020-01-15T12:30", "2028-02-15T15:40", ""):
            response = self.client.post(self.url("update"), self.data(due_at=due))
            self.assertEqual(response.status_code, 302)
            self.own.refresh_from_db()
            self.assertEqual(self.own.description, "")
            if due:
                self.assertTrue(timezone.is_aware(self.own.due_at))
                self.assertEqual(self.own.due_at.strftime("%Y-%m-%dT%H:%M"), due)
                self.assertContains(self.client.get(self.url("update")), f'value="{due}"')
            else:
                self.assertIsNone(self.own.due_at)

    def test_default_status(self):
        self.assertEqual(self.own.status, Task.Status.NEW)
        self.assertIsNone(self.own.completed_at)
        self.assertEqual(str(self.own), self.own.title)
        self.assertEqual(self.client.get(reverse("task_create")).context["form"]["status"].value(), "new")

    def test_manager_reads_only_own_tasks(self):
        response = self.client.get(reverse("task_list"))
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, self.foreign.title)
        self.assertNotContains(response, self.foreign_request.title)
        response = self.client.get(self.url("detail"))
        self.assertContains(response, self.own.title)
        self.assertContains(response, self.own_request.get_absolute_url())
        self.assertNotContains(response, '>Удалить</a>')

    def test_admin_sees_all_tasks(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("task_list"))
        for task in (self.own, self.foreign):
            self.assertContains(response, task.title)
            self.assertEqual(self.client.get(self.url("detail", task)).status_code, 200)

    def test_foreign_task_direct_urls_and_mutations_blocked(self):
        before = Task.objects.values().get(pk=self.foreign.pk)
        for action in ("detail", "update"):
            self.assertEqual(self.client.get(self.url(action, self.foreign)).status_code, 404)
        self.assertEqual(self.client.post(self.url("update", self.foreign), self.data()).status_code, 404)
        self.assertEqual(self.client.post(self.url("delete", self.foreign)).status_code, 403)
        self.assertEqual(Task.objects.values().get(pk=self.foreign.pk), before)

    def test_missing_task_404(self):
        for user in (self.manager, self.admin):
            self.client.force_login(user)
            for action in ("detail", "update", "delete"):
                if action == "delete" and not user.is_superuser:
                    continue
                url = reverse("task_" + action, kwargs={"pk": 999999})
                self.assertEqual(self.client.get(url).status_code, 404)
                if action != "detail":
                    self.assertEqual(self.client.post(url, self.data(assigned_to=self.manager.pk)).status_code, 404)

    def test_manager_edit_ignores_owner_and_completion_timestamp_post(self):
        created = self.own.created_at
        response = self.client.post(self.url("update"), self.data(
            assigned_to=self.other.pk, completed_at="2000-01-01T00:00", description="Изменено", status="in_progress",
        ), follow=True)
        self.assertContains(response, "Задача сохранена.")
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.manager)
        self.assertEqual(self.own.description, "Изменено")
        self.assertEqual(self.own.status, "in_progress")
        self.assertEqual(self.own.created_at, created)
        self.assertIsNone(self.own.completed_at)
        form = self.client.get(self.url("update")).context["form"]
        self.assertNotIn("completed_at", form.fields)
        self.assertNotIn("assigned_to", form.fields)

    def test_completion_lifecycle_centralized_in_model(self):
        first = timezone.now()
        second = first + timedelta(days=1)
        with patch("crm.models.timezone.now", return_value=first):
            self.own.status = "completed"
            self.own.save()
        self.own.refresh_from_db()
        self.assertEqual(self.own.completed_at, first)
        with patch("crm.models.timezone.now", return_value=second):
            self.own.description = "Повторное сохранение"
            self.own.save()
        self.own.refresh_from_db()
        self.assertEqual(self.own.completed_at, first)
        for status in ("new", "in_progress"):
            with self.subTest(status=status):
                self.own.status = status
                self.own.save()
                self.own.refresh_from_db()
                self.assertIsNone(self.own.completed_at)
                with patch("crm.models.timezone.now", return_value=second):
                    self.own.status = "completed"
                    self.own.save()
                self.own.refresh_from_db()
                self.assertEqual(self.own.completed_at, second)

    def test_completion_lifecycle_through_http_forms(self):
        self.assertEqual(self.client.post(self.url("update"), self.data(status="completed")).status_code, 302)
        self.own.refresh_from_db()
        first = self.own.completed_at
        self.assertIsNotNone(first)
        self.client.post(self.url("update"), self.data(status="completed", completed_at="2000-01-01T00:00"))
        self.own.refresh_from_db()
        self.assertEqual(self.own.completed_at, first)
        self.client.post(self.url("update"), self.data(status="in_progress"))
        self.own.refresh_from_db()
        self.assertIsNone(self.own.completed_at)
        self.client.post(self.url("update"), self.data(status="completed"))
        self.own.refresh_from_db()
        self.assertGreater(self.own.completed_at, first)
        self.assertContains(self.client.get(self.url("detail")), "Выполнена")

    def test_partial_save_preserves_completion_invariants(self):
        self.own.status = "completed"
        self.own.save(update_fields={"status"})
        self.own.refresh_from_db()
        first = self.own.completed_at
        self.assertIsNotNone(first)
        self.own.completed_at = timezone.now() + timedelta(days=1)
        self.own.title = "Новое название"
        self.own.save(update_fields={"title", "completed_at"})
        self.own.refresh_from_db()
        self.assertEqual(self.own.completed_at, first)
        self.own.status = "new"
        self.own.save(update_fields={"status"})
        self.own.refresh_from_db()
        self.assertIsNone(self.own.completed_at)

    def test_empty_update_fields_is_noop(self):
        before = Task.objects.values().get(pk=self.own.pk)
        self.own.status = "completed"
        self.own.save(update_fields=[])
        self.assertEqual(Task.objects.values().get(pk=self.own.pk), before)

    def test_create_completed_task_sets_date(self):
        task = Task.objects.create(request=self.own_request, title="Сразу выполнена", assigned_to=self.manager, status="completed")
        self.assertIsNotNone(task.completed_at)

    def test_admin_delete_only_via_post(self):
        self.client.force_login(self.admin)
        url = self.url("delete")
        self.assertContains(self.client.get(url), "Это действие нельзя отменить")
        self.assertTrue(Task.objects.filter(pk=self.own.pk).exists())
        self.assertEqual(self.client.delete(url).status_code, 405)
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, reverse("task_list"))
        self.assertContains(response, "Задача удалена.")
        self.assertFalse(Task.objects.filter(pk=self.own.pk).exists())
        self.assertTrue(ClientRequest.objects.filter(pk=self.own_request.pk).exists())

    def test_manager_and_staff_cannot_delete(self):
        for staff in (False, True):
            self.manager.is_staff = staff
            self.manager.save()
            for task in (self.own, self.foreign):
                self.assertEqual(self.client.get(self.url("delete", task)).status_code, 403)
                self.assertEqual(self.client.post(self.url("delete", task)).status_code, 403)
        self.assertEqual(Task.objects.count(), 2)

    def test_request_deletion_protected_with_friendly_message(self):
        with self.assertRaises(ProtectedError):
            self.own_request.delete()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_delete", kwargs={"pk": self.own_request.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нельзя удалить заявку")
        self.assertContains(response, "Данные сохранены")
        self.assertNotContains(response, "Заявка удалена.")
        self.assertTrue(Task.objects.filter(pk=self.own.pk).exists())
        self.assertTrue(ClientRequest.objects.filter(pk=self.own_request.pk).exists())

    def test_search_filter_and_scope(self):
        matching = Task.objects.create(request=self.own_request, title="Search own", assigned_to=self.manager, status="completed")
        Task.objects.create(request=self.foreign_request, title="Search foreign", assigned_to=self.other, status="completed")
        for params, expected in [({"q": "SEARCH"}, [matching]), ({"status": "new"}, [self.own]),
                                  ({"q": "Search", "status": "new"}, [])]:
            response = self.client.get(reverse("task_list"), params)
            self.assertQuerySetEqual(response.context["tasks"], expected)
            self.assertNotContains(response, self.foreign_request.title)

    def test_date_sort_and_pagination_preserve_filters(self):
        tasks = [Task.objects.create(request=self.own_request, title=f"Page {n}", assigned_to=self.manager) for n in range(11)]
        url = reverse("task_list")
        params = {"q": "Page", "status": "new", "sort": "oldest"}
        first = self.client.get(url, params)
        second = self.client.get(url, {**params, "page": 2})
        self.assertEqual(first.context["paginator"].count, 11)
        self.assertQuerySetEqual(first.context["tasks"], tasks[:10])
        self.assertQuerySetEqual(second.context["tasks"], tasks[10:])
        self.assertContains(first, "q=Page&amp;status=new&amp;sort=oldest&amp;page=2")
        self.assertQuerySetEqual(self.client.get(url, {"q": "Page"}).context["tasks"], list(reversed(tasks))[:10])
        self.assertEqual(self.client.get(url, {"page": "bad"}).status_code, 404)

    def test_empty_list_and_bad_filters(self):
        self.assertContains(self.client.get(reverse("task_list"), {"q": "No match"}), "Задачи не найдены")
        self.assertQuerySetEqual(self.client.get(reverse("task_list"), {"status": "bad", "sort": "title"}).context["tasks"], [self.own])
        Task.objects.filter(assigned_to=self.manager).delete()
        self.assertContains(self.client.get(reverse("task_list")), "Доступных задач пока нет")

    def test_anonymous_reads_and_posts_denied(self):
        self.client.logout()
        before = list(Task.objects.values())
        urls = [reverse("task_list"), reverse("task_create")]
        urls += [self.url(action) for action in ("detail", "update", "delete")]
        for url in urls:
            for method in (self.client.get, self.client.post):
                response = method(url, self.data())
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/login/?next="))
        self.assertEqual(list(Task.objects.values()), before)

    def test_csrf_missing_invalid_and_valid_tokens(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        for url in (reverse("task_create"), self.url("update"), self.url("delete")):
            browser.get(url)
            before = list(Task.objects.values())
            for token in (None, "x" * 64):
                data = self.data(assigned_to=self.manager.pk)
                if token:
                    data["csrfmiddlewaretoken"] = token
                self.assertEqual(browser.post(url, data).status_code, 403)
                self.assertEqual(list(Task.objects.values()), before)
            response = browser.post(url, self.data(assigned_to=self.manager.pk, csrfmiddlewaretoken=browser.cookies["csrftoken"].value))
            self.assertEqual(response.status_code, 302)

    def test_reassignment_removes_access_for_old_and_new_manager(self):
        self.own_request.assigned_to = self.other
        self.own_request.save()
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.manager)
        for user in (self.manager, self.other):
            self.client.force_login(user)
            listing = self.client.get(reverse("task_list"), {"q": self.own.title})
            self.assertQuerySetEqual(listing.context["tasks"], [])
            self.assertNotContains(listing, f'href="{self.own.get_absolute_url()}"')
            self.assertNotContains(listing, self.own_request.title)
            self.assertEqual(self.client.get(self.url("detail")).status_code, 404)
            self.assertEqual(self.client.get(self.url("update")).status_code, 404)
            self.assertEqual(self.client.post(self.url("update"), self.data()).status_code, 404)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.own_request.get_absolute_url()), self.own.title)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url("detail")), self.own.title)
        self.assertContains(self.client.get(self.own_request.get_absolute_url()), self.own.title)

    def test_admin_repairs_assignment_after_request_reassignment(self):
        self.own_request.assigned_to = self.other
        self.own_request.save()
        self.client.force_login(self.admin)
        response = self.client.post(self.url("update"), self.data(assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        self.own.refresh_from_db()
        self.assertEqual(self.own.assigned_to, self.other)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url("detail")).status_code, 200)
        self.assertEqual(self.client.post(self.url("update"), self.data(title="Новый ответственный изменил")).status_code, 302)

    def test_related_tasks_respect_scope(self):
        mismatch = Task.objects.create(request=self.own_request, title="Скрытая задача другого сотрудника", assigned_to=self.other)
        response = self.client.get(self.own_request.get_absolute_url())
        self.assertContains(response, self.own.title)
        self.assertNotContains(response, mismatch.title)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.own_request.get_absolute_url()), mismatch.title)
        empty_request = ClientRequest.objects.create(client=self.contact, title="Без задач", assigned_to=self.manager)
        self.assertContains(self.client.get(empty_request.get_absolute_url()), "Нет доступных задач заявки")

    def test_deleting_user_preserves_tasks_and_requests(self):
        self.other.delete()
        self.foreign.refresh_from_db()
        self.foreign_request.refresh_from_db()
        self.assertIsNone(self.foreign.assigned_to)
        self.assertIsNone(self.foreign_request.assigned_to)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url("detail", self.foreign)), "Не назначен")

    def test_no_automatic_tasks_on_request_creation_or_update(self):
        before = Task.objects.count()
        linked = ClientRequest.objects.create(client=self.contact, title="Не создаёт задачу", assigned_to=self.manager)
        linked.status = "completed"
        linked.save()
        self.assertEqual(Task.objects.count(), before)

    def test_put_cannot_modify_and_description_is_escaped(self):
        for url in (reverse("task_create"), self.url("update")):
            self.assertEqual(self.client.put(url, self.data()).status_code, 405)
        self.own.description = '<script>alert("test")</script>'
        self.own.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_partial_save_uses_persisted_status_when_status_not_saved(self):
        self.own.status = "completed"
        self.own.save(update_fields={"title"})
        self.own.refresh_from_db()
        self.assertEqual(self.own.status, "new")
        self.assertIsNone(self.own.completed_at)
        self.own.status = "completed"
        self.own.save()
        first = self.own.completed_at
        self.own.status = "new"
        self.own.save(update_fields={"title"})
        self.own.refresh_from_db()
        self.assertEqual(self.own.status, "completed")
        self.assertEqual(self.own.completed_at, first)
