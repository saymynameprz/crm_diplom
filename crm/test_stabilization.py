from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import ClientRequestForm
from .models import Client, ClientRequest, Task
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
        self.assertContains(self.client.get(url), "существующие задачи требуют ручного переназначения")
        self.assertNotContains(self.client.get(reverse("request_create")), "существующие задачи требуют ручного переназначения")
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(url), "существующие задачи требуют ручного переназначения")

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

    def test_reassignment_filters_counters_and_manual_repair(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("request_create"), self.data(due_at="2020-01-01T12:00"))
        linked = ClientRequest.objects.get()
        task = linked.tasks.get()
        self.client.post(reverse("request_update", kwargs={"pk": linked.pk}), self.data(status="new", assigned_to=self.other.pk))
        for actor in (self.manager, self.other):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(task.get_absolute_url()).status_code, 404)
            self.assertQuerySetEqual(self.client.get(reverse("task_list"), {"overdue": "yes"}).context["tasks"], [])
            home = self.client.get(reverse("home"))
            self.assertEqual(home.context["task_count"], 0)
            self.assertEqual(home.context["overdue_task_count"], 0)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("task_update", kwargs={"pk": task.pk}), {
            "request": linked.pk, "title": task.title, "status": "new", "assigned_to": self.other.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(task.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(reverse("home")).context["task_count"], 1)
