from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientRequestForm
from .models import Client, ClientRequest, Task, TaskQuerySet
from .services import create_request_with_task, ensure_automatic_task


class StabilizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("stabilization-admin", is_superuser=True)
        cls.manager = User.objects.create_user("stabilization-manager")
        cls.other = User.objects.create_user("stabilization-other")
        cls.contact = Client.objects.create(full_name="Вымышленный клиент стабилизации")

    def data(self, **extra):
        return {"client": self.contact.pk, "title": "Заявка 5.1", "assigned_to": self.manager.pk, **extra}

    def test_admin_reassignment_gives_new_manager_the_existing_automatic_task(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_create"), self.data(due_at="2020-01-01T12:00"))
        self.assertEqual(response.status_code, 302)
        linked = ClientRequest.objects.get()
        task = linked.tasks.get(is_automatic=True)
        update_url = reverse("request_update", args=[linked.pk])
        response = self.client.post(update_url, self.data(status="new", assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        linked.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(linked.assigned_to, self.other)

        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(linked.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(task.get_absolute_url()).status_code, 404)
        task_edit_url = reverse("task_update", args=[task.pk])
        self.assertEqual(self.client.post(task_edit_url, {"title": "Чужая правка", "status": "new"}).status_code, 404)

        self.client.force_login(self.other)
        self.assertIn(task, self.client.get(linked.get_absolute_url()).context["request_tasks"])
        self.assertEqual(self.client.get(task.get_absolute_url()).status_code, 200)
        self.assertEqual(task.assigned_to, self.other)
        response = self.client.post(task_edit_url, {
            "request": linked.pk, "title": task.title, "status": "in_progress",
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)

    def test_creation_ignores_all_supplied_statuses_for_both_roles(self):
        for actor in (self.admin, self.manager):
            self.client.force_login(actor)
            self.assertNotContains(self.client.get(reverse("request_create")), 'name="status"')
            for status in (None, "in_progress", "completed", "cancelled", "invalid"):
                with self.subTest(actor=actor.username, status=status):
                    data = self.data()
                    if status is not None:
                        data["status"] = status
                    response = self.client.post(reverse("request_create"), data)
                    self.assertEqual(response.status_code, 302)
                    linked = ClientRequest.objects.latest("pk")
                    self.assertEqual(linked.status, "new")
                    self.assertEqual(linked.tasks.count(), 1)
                    self.assertEqual(linked.tasks.get().status, "new")
                    self.assertTrue(linked.tasks.get().is_automatic)

    def test_form_and_service_override_preseeded_status_on_new_instance(self):
        form = ClientRequestForm(self.data(status="completed"), user=self.admin,
                                 instance=ClientRequest(status="cancelled"))
        linked = create_request_with_task(form)
        self.assertEqual(linked.status, "new")
        self.assertNotIn("status", form.fields)

    def test_update_keeps_all_statuses_and_invalid_status_validation(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("request_create"), self.data())
        linked = ClientRequest.objects.get()
        url = reverse("request_update", kwargs={"pk": linked.pk})
        self.assertContains(self.client.get(url), 'name="status"')
        for status in ClientRequest.Status.values:
            self.assertEqual(self.client.post(url, self.data(status=status)).status_code, 302)
            linked.refresh_from_db()
            self.assertEqual(linked.status, status)
        response = self.client.post(url, self.data(status="invalid"))
        self.assertIn("status", response.context["form"].errors)
        self.assertEqual(linked.tasks.count(), 1)

    def test_reassignment_notice_only_on_admin_edit(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Пояснение", assigned_to=self.manager)
        url = reverse("request_update", kwargs={"pk": linked.pk})
        self.client.force_login(self.admin)
        notice = "все её открытые задачи перейдут новому ответственному"
        self.assertContains(self.client.get(url), notice)
        self.assertNotContains(self.client.get(reverse("request_create")), notice)
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(url), notice)

    def test_distinct_posts_create_distinct_requests_with_one_task_each(self):
        self.client.force_login(self.manager)
        for title in ("Первый POST", "Второй POST"):
            self.assertEqual(self.client.post(reverse("request_create"), self.data(title=title)).status_code, 302)
        self.assertEqual(ClientRequest.objects.count(), 2)
        for linked in ClientRequest.objects.all():
            self.assertEqual(linked.tasks.filter(is_automatic=True).count(), 1)

    def test_admin_deletes_auto_task_and_service_can_restore_it(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("request_create"), self.data())
        linked = ClientRequest.objects.get()
        original = linked.tasks.get()
        self.assertEqual(self.client.post(reverse("task_delete", kwargs={"pk": original.pk})).status_code, 302)
        self.assertFalse(Task.objects.filter(pk=original.pk).exists())
        restored = ensure_automatic_task(linked)
        self.assertTrue(restored.is_automatic)
        self.assertEqual(ensure_automatic_task(linked).pk, restored.pk)
        self.assertEqual(linked.tasks.count(), 1)

    def test_reassignment_moves_list_and_overdue_counters_to_new_manager(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("request_create"), self.data(due_at="2020-01-01T12:00"))
        linked = ClientRequest.objects.get()
        task = linked.tasks.get()
        self.client.post(reverse("request_update", kwargs={"pk": linked.pk}), self.data(status="new", assigned_to=self.other.pk))
        for actor, count, expected in ((self.manager, 0, []), (self.other, 1, [task])):
            self.client.force_login(actor)
            listing = self.client.get(reverse("task_list"), {"overdue": "yes"})
            self.assertQuerySetEqual(listing.context["tasks"], expected)
            home = self.client.get(reverse("home"))
            self.assertEqual(home.context["task_count"], count)
            self.assertEqual(home.context["overdue_task_count"], count)

    def test_reassignment_transfers_open_manual_task_and_preserves_completed_task(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Заявка", assigned_to=self.manager)
        due_at = timezone.now() + timedelta(days=1)
        manual = Task.objects.create(request=linked, title="Ручная задача", description="Описание",
                                     status=Task.Status.IN_PROGRESS, assigned_to=self.manager, due_at=due_at)
        completed = ensure_automatic_task(linked)
        completed.status = Task.Status.COMPLETED
        completed.save()
        completed_before = Task.objects.values().get(pk=completed.pk)
        self.assertIsNotNone(completed_before["completed_at"])

        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_update", args=[linked.pk]),
                                    self.data(status="new", assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        manual.refresh_from_db()
        self.assertEqual(manual.assigned_to, self.other)
        self.assertEqual(manual.status, Task.Status.IN_PROGRESS)
        self.assertEqual(manual.due_at, due_at)
        self.assertEqual(manual.description, "Описание")
        self.assertFalse(manual.is_automatic)
        self.assertEqual(Task.objects.values().get(pk=completed.pk), completed_before)
        self.assertEqual(self.client.get(completed.get_absolute_url()).status_code, 200)
        for actor in (self.manager, self.other):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(completed.get_absolute_url()).status_code, 404)

    def test_reassignment_of_request_without_tasks_does_not_create_one(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Без задач", assigned_to=self.manager)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("request_update", args=[linked.pk]),
                                    self.data(status="new", assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302)
        linked.refresh_from_db()
        self.assertEqual(linked.assigned_to, self.other)
        self.assertFalse(linked.tasks.exists())

    def test_reassignment_rolls_back_request_and_tasks_after_task_update_error(self):
        linked = ClientRequest.objects.create(client=self.contact, title="До изменения", assigned_to=self.manager)
        task = ensure_automatic_task(linked)
        request_before = ClientRequest.objects.values().get(pk=linked.pk)
        tasks_before = list(linked.tasks.values())
        original_update = TaskQuerySet.update

        def update_then_fail(queryset, **kwargs):
            self.assertEqual(ClientRequest.objects.get(pk=linked.pk).assigned_to, self.other)
            self.assertEqual(original_update(queryset, **kwargs), 1)
            self.assertEqual(Task.objects.get(pk=task.pk).assigned_to, self.other)
            raise DatabaseError("Ошибка в испытании отката")

        self.client.force_login(self.admin)
        with self.assertLogs("crm.views", level="ERROR"):
            with patch.object(TaskQuerySet, "update", autospec=True, side_effect=update_then_fail):
                response = self.client.post(reverse("request_update", args=[linked.pk]),
                                            self.data(status="in_progress", assigned_to=self.other.pk))
        self.assertContains(response, "Данные не сохранены.", status_code=503)
        self.assertEqual(ClientRequest.objects.values().get(pk=linked.pk), request_before)
        self.assertEqual(list(linked.tasks.values()), tasks_before)

    def test_manager_cannot_transfer_tasks_or_take_a_foreign_request(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Своя заявка", assigned_to=self.manager)
        other_request = ClientRequest.objects.create(client=self.contact, title="Чужая заявка", assigned_to=self.other)
        task = ensure_automatic_task(linked)
        foreign_task = ensure_automatic_task(other_request)
        self.manager.is_staff = True
        self.manager.save()
        self.client.force_login(self.manager)
        response = self.client.post(reverse("request_update", args=[linked.pk]),
                                    self.data(status="new", assigned_to=self.other.pk, confirm_open_task_reassignment="on"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(reverse("request_update", args=[other_request.pk]),
                                         self.data(status="new")).status_code, 404)
        self.assertEqual(self.client.post(reverse("task_update", args=[foreign_task.pk]), {
            "request": linked.pk, "title": foreign_task.title, "status": "new",
        }).status_code, 404)
        linked.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(linked.assigned_to, self.manager)
        self.assertEqual(task.assigned_to, self.manager)

    def test_legacy_mismatch_needs_review_before_an_ownership_change(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Старая заявка", assigned_to=self.manager)
        historical_owner = User.objects.create_user("stabilization-historical-owner")
        task = Task.objects.create(request=linked, title="Задача с прежним ответственным", assigned_to=historical_owner)
        unassigned = Task.objects.create(request=linked, title="Задача без ответственного")
        update_url = reverse("request_update", args=[linked.pk])
        self.client.force_login(self.admin)
        response = self.client.get(update_url)
        self.assertContains(response, task.title)
        self.assertContains(response, unassigned.title)
        self.assertEqual(self.client.post(update_url, self.data(status="in_progress")).status_code, 302)
        before = list(linked.tasks.values())

        data = self.data(status="new", assigned_to=self.other.pk)
        response = self.client.post(update_url, data)
        self.assertContains(response, "Проверьте перечисленные задачи и подтвердите их перенос.")
        linked.refresh_from_db()
        self.assertEqual(linked.assigned_to, self.manager)
        self.assertEqual(linked.status, ClientRequest.Status.IN_PROGRESS)
        self.assertEqual(list(linked.tasks.values()), before)
        self.assertEqual({row["assigned_to_id"] for row in before}, {historical_owner.pk, None})

        response = self.client.post(update_url, {**data, "confirm_open_task_reassignment": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(linked.tasks.values_list("assigned_to_id", flat=True)), {self.other.pk})
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(task.get_absolute_url()).status_code, 200)

    def test_repeated_reassignments_keep_one_automatic_task(self):
        linked = ClientRequest.objects.create(client=self.contact, title="Заявка", assigned_to=self.manager)
        task = ensure_automatic_task(linked)
        self.client.force_login(self.admin)
        update_url = reverse("request_update", args=[linked.pk])
        for owner in (self.other, self.other, self.manager):
            with self.subTest(owner=owner.username):
                response = self.client.post(update_url, self.data(status="new", assigned_to=owner.pk))
                self.assertEqual(response.status_code, 302)
                linked.refresh_from_db()
                self.assertEqual(linked.assigned_to, owner)
                current = ensure_automatic_task(linked)
                self.assertEqual(current.pk, task.pk)
                self.assertEqual(current.assigned_to, owner)
                self.assertEqual(linked.tasks.count(), 1)
