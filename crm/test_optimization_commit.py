"""Signed two-step confirmation, atomic CRM writes and real SQLite concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
import threading
import time
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.db import OperationalError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .forms import OptimizationForm, OptimizationDetailsForm
from .models import Client, ClientRequest, Task, Deal, ClientInteraction, OptimizationCommit
from .optimization import branch_and_bound, Status
from .optimization_commit import (CommitError, SALT, SNAPSHOT_TTL, issue_snapshot, read_snapshot,
                                  prepare_snapshot, commit_snapshot, sign_snapshot, decode_snapshot)
from .services import create_request_with_task


def source_data(workers, count=2):
    data = {"request_count": str(count), "employee_count": "2", "alpha": "0.5", "beta": "0.5",
        "employee_0": str(workers[0].pk), "capacity_0": "2", "occupied_0": "0", "rate_0": "100",
        "employee_1": str(workers[1].pk), "capacity_1": "2:15", "occupied_1": "0", "rate_1": "400"}
    for i in range(count):
        data.update({f"request_id_{i}": str(i+1), f"title_{i}": f"Вымышленная работа {i+1}",
                     f"effort_{i}_0": "1" if i == 0 else "2", f"effort_{i}_1": "1" if i == 0 else "2:15"})
    if count > 2:
        data.update(capacity_0="10", capacity_1="10")
    return data


def calculation(actor, workers, *, count=2, unproven=False, changes=None):
    data = {**source_data(workers, count), **(changes or {})}
    form = OptimizationForm(data, request_count=int(data["request_count"]), employee_count=int(data["employee_count"]),
                            users=list(User.objects.filter(is_active=True)))
    if not form.is_valid():
        raise AssertionError(form.errors)
    result = branch_and_bound(form.to_problem(), node_limit=0 if unproven else 10000, time_limit_seconds=None)
    return issue_snapshot(form, result, actor)


def details_data(raw, contacts, count=2):
    data = {"snapshot": raw, "horizon_start": timezone.now().isoformat(),
            "due_at": (timezone.now()+timedelta(days=2)).replace(second=0, microsecond=0).isoformat(), "estimates_confirmed": "on"}
    for i in range(count):
        data.update({f"client_{i+1}": str(contacts[i % len(contacts)].pk), f"description_{i+1}": f"Учебное описание {i+1}"})
    return data


def prepared(actor, workers, contacts, *, count=2, unproven=False, changes=None):
    raw = calculation(actor, workers, count=count, unproven=unproven, changes=changes)
    verified = read_snapshot(raw, actor)
    data = details_data(raw, contacts, count)
    form = OptimizationDetailsForm(data, local_ids=[i for i, _ in verified.result.assignment])
    if not form.is_valid():
        raise AssertionError(form.errors)
    return prepare_snapshot(verified, form, actor)


class OptimizationCommitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("commit-admin", is_superuser=True)
        cls.other_admin = User.objects.create_user("commit-other-admin", is_superuser=True)
        cls.manager = User.objects.create_user("commit-manager")
        cls.other = User.objects.create_user("commit-other", is_staff=True)
        cls.inactive = User.objects.create_user("commit-inactive", is_active=False, is_superuser=True)
        cls.contacts = [Client.objects.create(full_name=f"Вымышленный клиент {i}") for i in (1,2)]
        cls.old = ClientRequest.objects.create(client=cls.contacts[0], title="Прежняя заявка", assigned_to=cls.manager)
        Task.objects.create(request=cls.old, title="Прежняя задача", assigned_to=cls.manager)
        Deal.objects.create(client=cls.contacts[0], title="Прежняя сделка", amount=Decimal("12.34"), assigned_to=cls.other)
        ClientInteraction.objects.create(client=cls.contacts[0], created_by=cls.manager,
            interaction_type="call", subject="Прежнее событие", occurred_at=timezone.now())

    def setUp(self):
        self.client.force_login(self.admin)
        self.workers = (self.manager, self.other)
        self.url = reverse("optimization_confirm")

    def token(self, **kwargs):
        return prepared(self.admin, self.workers, self.contacts, **kwargs)

    def counts(self):
        return (ClientRequest.objects.count(), Task.objects.count(), OptimizationCommit.objects.count())

    def post(self, token, **extra):
        return self.client.post(self.url, {"snapshot": token, "confirm_create": "on", **extra})

    def test_full_two_step_http_flow_creates_nothing_before_final_confirmation(self):
        before = self.counts()
        page = self.client.post(reverse("optimization"), source_data(self.workers))
        raw = page.context["snapshot"]
        self.assertEqual(self.counts(), before)
        choose = self.client.post(reverse("optimization_prepare"), {"snapshot": raw})
        self.assertEqual(choose.status_code, 200)
        self.assertEqual(self.counts(), before)
        preview = self.client.post(reverse("optimization_preview"), details_data(raw, self.contacts))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "19/26")
        self.assertContains(preview, "Подтвердить и создать заявки")
        self.assertEqual(self.counts(), before)
        token = preview.context["final_form"].initial["snapshot"]
        response = self.post(token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.counts(), (before[0]+2, before[1]+2, 1))
        result = self.client.get(response.url)
        self.assertContains(result, "Весь пакет успешно подтверждён")
        for row in OptimizationCommit.objects.get().created_ids:
            self.assertContains(result, reverse("request_detail", kwargs={"pk": row["request_id"]}))
            self.assertContains(result, reverse("task_detail", kwargs={"pk": row["task_id"]}))

    def test_single_request_is_supported(self):
        record = commit_snapshot(self.token(count=1), self.admin)
        self.assertEqual(len(record.created_ids), 1)

    def test_m1_real_records_have_exact_assignment_status_clients_and_due_date(self):
        token = self.token()
        payload = decode_snapshot(token, self.admin, stage="prepared")
        record = commit_snapshot(token, self.admin)
        self.assertEqual(read_snapshot(token, self.admin, stage="prepared").result.metrics.objective, Fraction(19,26))
        self.assertEqual([r["employee_id"] for r in record.created_ids], [self.other.pk, self.manager.pk])
        for row, contact in zip(record.created_ids, self.contacts):
            request = ClientRequest.objects.get(pk=row["request_id"])
            task = Task.objects.get(pk=row["task_id"])
            self.assertEqual(request.client_id, contact.pk)
            self.assertEqual((request.status, task.status), ("new", "new"))
            self.assertEqual(request.assigned_to_id, task.assigned_to_id)
            self.assertEqual(request.assigned_to_id, row["employee_id"])
            self.assertEqual(request.due_at.isoformat(), payload["details"]["due_at"])
            self.assertEqual(task.due_at, request.due_at)
            self.assertEqual(request.tasks.filter(is_automatic=True).count(), 1)

    def test_existing_requests_tasks_deals_history_users_and_clients_are_unchanged(self):
        models = (User, Client, ClientRequest, Task, Deal, ClientInteraction)
        before = {m: list(m.objects.order_by("pk").values()) for m in models}
        commit_snapshot(self.token(), self.admin)
        for model, rows in before.items():
            self.assertEqual(list(model.objects.filter(pk__in=[r["id"] for r in rows]).order_by("pk").values()), rows)

    def test_unproven_plan_requires_explicit_additional_consent(self):
        token = self.token(unproven=True)
        before = self.counts()
        response = self.post(token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(), before)
        with self.assertRaises(CommitError):
            commit_snapshot(token, self.admin)
        self.assertEqual(self.post(token, accept_unproven="on").status_code, 302)

    def test_no_complete_bnb_result_has_no_continue_button(self):
        for changes in ({"effort_1_0":"", "effort_1_1":""}, {"capacity_0":"0:01","capacity_1":"0:01"}):
            response = self.client.post(reverse("optimization"), {**source_data(self.workers), **changes})
            self.assertIsNone(response.context["snapshot"])
            self.assertNotContains(response, "Подготовить пакет заявок")

    def test_signed_invalid_status_and_partial_assignment_are_rejected(self):
        raw = calculation(self.admin, self.workers)
        for status in ("UNKNOWN", "INFEASIBLE", "HEURISTIC_FAILED", "TECHNICAL_ERROR"):
            payload = signing.loads(raw, salt=SALT);payload["plan"]["status"] = status
            with self.assertRaises(CommitError):
                read_snapshot(sign_snapshot(payload), self.admin)
        payload = signing.loads(raw, salt=SALT);payload["plan"]["assignment"].pop()
        with self.assertRaises(CommitError):
            read_snapshot(sign_snapshot(payload), self.admin)

    def test_tampered_signature_and_mathematical_fields_are_rejected(self):
        raw = self.token();before=self.counts()
        for section,key,value in (("plan","assignment",[[1,self.manager.pk],[2,self.manager.pk]]),
                                  ("plan","metrics",{"cost":"0"}),("source","rate_0","0")):
            payload=signing.loads(raw,salt=SALT)
            self.assertNotEqual(payload[section][key],value)
            payload[section][key]=value
            changed=sign_snapshot(payload)
            bad=changed.rsplit(":",1)[0]+":"+raw.rsplit(":",1)[1]
            self.assertEqual(self.post(bad).status_code,400)
            self.assertEqual(self.counts(),before)

    def test_unproven_status_upgrade_with_original_signature_is_rejected(self):
        raw = self.token(unproven=True)
        payload = signing.loads(raw, salt=SALT)
        self.assertEqual(payload["plan"]["status"], Status.FEASIBLE_NOT_PROVEN.value)
        before = self.counts()
        # The real limited solver's plan still needs the administrator's extra consent.
        self.assertEqual(self.post(raw).status_code, 400)
        original_status = payload["plan"]["status"]
        payload["plan"]["status"] = Status.OPTIMAL.value
        self.assertNotEqual(payload["plan"]["status"], original_status)
        # Preserve both the original timestamp and HMAC; only the signed content changes.
        changed_content = sign_snapshot(payload).split(":", 1)[0]
        bad = changed_content + ":" + raw.split(":", 1)[1]
        with self.assertRaises(signing.BadSignature):
            signing.loads(bad, salt=SALT)
        for consent in ({}, {"accept_unproven": "on"}):
            self.assertEqual(self.post(bad, **consent).status_code, 400)
            self.assertEqual(self.counts(), before)
        self.assertEqual(self.post(raw).status_code, 400)
        self.assertEqual(self.counts(), before)

    def test_even_signed_plan_is_revalidated_for_qualification_and_exact_metrics(self):
        raw=self.token()
        for mutate in (lambda p:p["plan"]["metrics"].update(cost="0"),
                       lambda p:p["plan"].update(assignment=[[1,self.manager.pk],[2,self.manager.pk]]),
                       lambda p:p["source"].update(effort_0_1="")):
            payload=signing.loads(raw,salt=SALT);mutate(payload)
            with self.assertRaises(CommitError):
                commit_snapshot(sign_snapshot(payload),self.admin)
        self.assertEqual(OptimizationCommit.objects.count(),0)

    def test_unsigned_override_fields_are_rejected(self):
        raw=self.token();before=self.counts()
        for extra in ({"assigned_to":str(self.admin.pk)},{"objective":"0"},{"status":"new"},{"title":"Changed"}):
            self.assertEqual(self.post(raw,**extra).status_code,400)
            self.assertEqual(self.counts(),before)

    def test_duplicate_rows_employees_and_local_ids_in_signed_snapshot_are_rejected(self):
        raw=self.token();before=self.counts()
        for mutate in (lambda p:p["details"]["rows"].append(p["details"]["rows"][0]),
                       lambda p:p["source"].update(employee_1=p["source"]["employee_0"]),
                       lambda p:p["source"].update(request_id_1="1")):
            payload=signing.loads(raw,salt=SALT);mutate(payload)
            with self.assertRaises(CommitError):commit_snapshot(sign_snapshot(payload),self.admin)
            self.assertEqual(self.counts(),before)

    def test_expired_uncommitted_snapshot_creates_nothing(self):
        raw=self.token();before=self.counts()
        with patch("crm.optimization_commit.time.time",return_value=time.time()+SNAPSHOT_TTL+2):
            self.assertEqual(self.post(raw).status_code,410)
        self.assertEqual(self.counts(),before)

    def test_preview_does_not_extend_original_calculation_lifetime(self):
        raw=calculation(self.admin,self.workers)
        with patch("crm.optimization_commit.time.time",return_value=time.time()+SNAPSHOT_TTL-1):
            verified=read_snapshot(raw,self.admin)
            form=OptimizationDetailsForm(details_data(raw,self.contacts),local_ids=[1,2])
            newer=prepare_snapshot(verified,form,self.admin)
        with patch("crm.optimization_commit.time.time",return_value=time.time()+SNAPSHOT_TTL+2):
            with self.assertRaises(CommitError):commit_snapshot(newer,self.admin)

    def test_snapshot_cannot_be_used_by_another_admin(self):
        raw=self.token();self.client.force_login(self.other_admin)
        self.assertEqual(self.post(raw).status_code,403)
        self.assertEqual(OptimizationCommit.objects.count(),0)

    def test_manager_staff_anonymous_and_inactive_cannot_prepare_or_confirm(self):
        raw=self.token()
        for actor in (self.manager,self.other,self.inactive,None):
            if actor:self.client.force_login(actor)
            else:self.client.logout()
            for name in ("optimization_prepare","optimization_preview","optimization_confirm"):
                response=self.client.post(reverse(name),{"snapshot":raw,"confirm_create":"on"})
                self.assertEqual(response.status_code,403 if actor in (self.manager,self.other) else 302)
        self.assertEqual(OptimizationCommit.objects.count(),0)

    def test_direct_service_rechecks_admin_flags_in_database(self):
        raw=self.token();User.objects.filter(pk=self.admin.pk).update(is_superuser=False)
        with self.assertRaises(CommitError) as error:commit_snapshot(raw,self.admin)
        self.assertEqual(error.exception.status,403)

    def test_post_only_endpoints_and_result_author_ownership(self):
        for name in ("optimization_prepare","optimization_preview","optimization_confirm"):
            self.assertEqual(self.client.get(reverse(name)).status_code,405)
        response=self.post(self.token());url=response.url
        self.client.force_login(self.other_admin);self.assertEqual(self.client.get(url).status_code,404)
        self.client.force_login(self.manager);self.assertEqual(self.client.get(url).status_code,403)

    def test_csrf_enforced_for_each_write_stage(self):
        browser=HttpClient(enforce_csrf_checks=True);browser.force_login(self.admin)
        for name in ("optimization_prepare","optimization_preview","optimization_confirm"):
            self.assertEqual(browser.post(reverse(name),{"snapshot":self.token(),"confirm_create":"on"}).status_code,403)
        page=browser.get(reverse("optimization"));token=browser.cookies["csrftoken"].value
        response=browser.post(self.url,{"snapshot":self.token(),"confirm_create":"on","csrfmiddlewaretoken":token})
        self.assertEqual(response.status_code,302)

    def test_deleted_client_and_changed_client_are_stale(self):
        for change in ("update","delete"):
            contact=Client.objects.create(full_name="Временный вымышленный клиент")
            raw=prepared(self.admin,self.workers,[contact])
            if change=="update":Client.objects.filter(pk=contact.pk).update(phone="123")
            else:contact.delete()
            before=self.counts()
            self.assertEqual(self.post(raw).status_code,409)
            self.assertEqual(self.counts(),before)

    def test_deactivated_or_renamed_employee_invalidates_snapshot(self):
        for changes in ({"is_active":False},{"username":"changed-worker"}):
            raw=self.token();before=self.counts()
            User.objects.filter(pk=self.other.pk).update(**changes)
            self.assertEqual(self.post(raw).status_code,409)
            self.assertEqual(self.counts(),before)
            User.objects.filter(pk=self.other.pk).update(is_active=True,username=self.other.username)

    def test_invalid_dates_horizon_acknowledgement_and_description(self):
        raw=calculation(self.admin,self.workers);base=details_data(raw,self.contacts)
        for changes in ({"due_at":"bad"},{"due_at":(timezone.now()-timedelta(days=1)).isoformat()},
                        {"horizon_start":(timezone.now()+timedelta(days=3)).isoformat()},
                        {"estimates_confirmed":""},{"description_1":"x"*1001},{"client_1":"999999"}):
            response=self.client.post(reverse("optimization_preview"),{**base,**changes})
            self.assertEqual(response.status_code,400)
            self.assertEqual(OptimizationCommit.objects.count(),0)

    def test_due_date_is_revalidated_at_final_post(self):
        raw=self.token();payload=signing.loads(raw,salt=SALT)
        payload["details"]["due_at"]=(timezone.now()-timedelta(seconds=1)).isoformat()
        before=self.counts();self.assertEqual(self.post(sign_snapshot(payload)).status_code,409)
        self.assertEqual(self.counts(),before)

    def test_late_request_failure_rolls_back_every_request_task_and_receipt(self):
        raw=self.token(count=3);before=self.counts();calls=[]
        def fail_last(form):
            result=create_request_with_task(form);calls.append(result.pk)
            if len(calls)==3:raise RuntimeError("late synthetic failure")
            return result
        with patch("crm.optimization_commit.create_request_with_task",side_effect=fail_last):
            with self.assertRaises(RuntimeError):commit_snapshot(raw,self.admin)
        self.assertEqual(len(calls),3)
        self.assertEqual(self.counts(),before)
        self.assertEqual(len(commit_snapshot(raw,self.admin).created_ids),3)

    def test_automatic_task_failure_rolls_back_entire_package(self):
        from .services import ensure_automatic_task
        raw=self.token();before=self.counts();calls=[]
        def fail_task(request):
            task=ensure_automatic_task(request);calls.append(task.pk)
            if len(calls)==2:raise RuntimeError("synthetic task failure")
            return task
        with patch("crm.services.ensure_automatic_task",side_effect=fail_task):
            with self.assertRaises(RuntimeError):commit_snapshot(raw,self.admin)
        self.assertEqual(self.counts(),before)

    def test_current_request_form_invalid_in_last_row_rolls_back(self):
        from .optimization_commit import request_form
        raw=self.token();before=self.counts();calls=[]
        def invalid_last(*args):
            form=request_form(*args);calls.append(form)
            if len(calls)==2:form.is_valid=lambda:False
            return form
        with patch("crm.optimization_commit.request_form",side_effect=invalid_last):
            with self.assertRaises(CommitError):commit_snapshot(raw,self.admin)
        self.assertEqual(self.counts(),before)

    def test_database_busy_is_controlled_retry_and_no_partial_receipt(self):
        raw=self.token();before=self.counts()
        with patch("crm.optimization_commit.OptimizationCommit.objects.create",side_effect=OperationalError("locked")),self.assertLogs("crm.views",level="ERROR"):
            response=self.post(raw)
        self.assertEqual(response.status_code,503)
        self.assertContains(response,"Повторить это же подтверждение",status_code=503)
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.post(raw).status_code,302)

    def test_unexpected_failure_is_logged_but_not_exposed(self):
        raw=self.token();before=self.counts()
        with patch("crm.optimization_commit.create_request_with_task",side_effect=RuntimeError("secret /internal/path")),self.assertLogs("crm.views",level="ERROR") as logs:
            response=self.post(raw)
        self.assertEqual(response.status_code,503)
        self.assertNotContains(response,"secret /internal/path",status_code=503)
        self.assertIn("Traceback","\n".join(logs.output))
        self.assertEqual(self.counts(),before)

    def test_identical_posts_return_same_ids(self):
        raw=self.token();first=self.post(raw);record=OptimizationCommit.objects.get();ids=record.created_ids
        after=self.counts()
        for _ in range(3):
            response=self.post(raw)
            self.assertEqual(response.url,first.url)
            self.assertEqual(self.counts(),after)
            self.assertEqual(OptimizationCommit.objects.get().created_ids,ids)

    def test_replay_of_completed_token_after_expiry_returns_receipt_without_writes(self):
        raw=self.token();first=commit_snapshot(raw,self.admin);after=self.counts()
        with patch("crm.optimization_commit.time.time",return_value=time.time()+SNAPSHOT_TTL+2):
            second=commit_snapshot(raw,self.admin)
        self.assertEqual(first.pk,second.pk);self.assertEqual(self.counts(),after)

    def test_same_token_with_different_details_is_rejected(self):
        source=calculation(self.admin,self.workers);verified=read_snapshot(source,self.admin)
        first_data=details_data(source,self.contacts)
        first_form=OptimizationDetailsForm(first_data,local_ids=[1,2])
        first=prepare_snapshot(verified,first_form,self.admin)
        second_form=OptimizationDetailsForm({**first_data,"description_2":"Другое описание"},local_ids=[1,2])
        second=prepare_snapshot(verified,second_form,self.admin)
        commit_snapshot(first,self.admin);after=self.counts()
        with self.assertRaises(CommitError):commit_snapshot(second,self.admin)
        self.assertEqual(self.counts(),after)

    def test_new_independent_token_is_a_new_admin_action(self):
        first=commit_snapshot(self.token(),self.admin)
        second=commit_snapshot(self.token(),self.admin)
        self.assertNotEqual(first.pk,second.pk)
        self.assertEqual(OptimizationCommit.objects.count(),2)

    def test_receipt_stores_only_digest_author_time_and_created_ids(self):
        record=commit_snapshot(self.token(),self.admin)
        self.assertEqual(set(f.name for f in OptimizationCommit._meta.fields),{"token","created_by","digest","created_at","created_ids"})
        self.assertRegex(record.digest,r"^[0-9a-f]{64}$")
        self.assertEqual(set(record.created_ids[0]),{"local_id","request_id","task_id","employee_id"})

    def test_xss_cache_headers_and_no_javascript(self):
        raw=calculation(self.admin,self.workers,changes={"title_0":"<script>bad()</script>"})
        response=self.client.post(reverse("optimization_preview"),{**details_data(raw,self.contacts),"description_1":"<img src=x onerror=bad()>"})
        self.assertNotContains(response,"<script>");self.assertNotContains(response,"<img src=x")
        self.assertContains(response,"&lt;script&gt;")
        self.assertIn("no-store",response["Cache-Control"])
        self.assertNotIn("<script",response.content.decode())

    def test_repeated_keys_and_oversized_confirmation_are_rejected(self):
        from django.http import QueryDict
        raw=self.token();data=QueryDict(mutable=True);data.update(snapshot=raw,confirm_create="on")
        data.setlist("confirm_create",["on","on"])
        self.assertEqual(self.client.post(self.url,data.urlencode(),content_type="application/x-www-form-urlencoded").status_code,400)
        self.assertEqual(self.post(raw,unexpected="x"*270000).status_code,400)
        self.assertEqual(OptimizationCommit.objects.count(),0)


class OptimizationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin=User.objects.create_user("parallel-admin",is_superuser=True)
        self.workers=(User.objects.create_user("parallel-a"),User.objects.create_user("parallel-b"))
        self.contacts=[Client.objects.create(full_name="Вымышленный клиент параллельности")]
        self.raw=prepared(self.admin,self.workers,self.contacts)

    def test_response_failure_after_commit_replays_complete_package_without_duplicates(self):
        self.client.force_login(self.admin)
        payload = decode_snapshot(self.raw, self.admin, stage="prepared")
        data = {"snapshot": self.raw, "confirm_create": "on"}
        url = reverse("optimization_confirm")
        before = (ClientRequest.objects.count(), Task.objects.count())
        committed_ids = []

        def lose_response(name, *, token):
            self.assertEqual(name, "optimization_result")
            self.assertFalse(connection.in_atomic_block)
            record = OptimizationCommit.objects.get(pk=token)
            committed_ids.extend(record.created_ids)
            raise RuntimeError("synthetic response failure AFTER COMMIT /internal/path")

        with patch("crm.views.redirect", side_effect=lose_response) as redirect, self.assertLogs("crm.views", level="ERROR") as logs:
            response = self.client.post(url, data)
        redirect.assert_called_once()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(OptimizationCommit.objects.count(), 1)
        record = OptimizationCommit.objects.get()
        self.assertEqual(str(record.token), payload["token"])
        self.assertEqual(record.created_by_id, self.admin.pk)
        self.assertEqual(len(committed_ids), 2)
        self.assertEqual(record.created_ids, committed_ids)
        after = (ClientRequest.objects.count(), Task.objects.count())
        self.assertEqual(after, (before[0]+2, before[1]+2))
        assignment = dict(payload["plan"]["assignment"])
        details = {row["local_id"]: row for row in payload["details"]["rows"]}
        for row in committed_ids:
            request = ClientRequest.objects.get(pk=row["request_id"])
            task = Task.objects.get(pk=row["task_id"])
            self.assertEqual(request.client_id, details[row["local_id"]]["client"])
            self.assertEqual(request.assigned_to_id, assignment[row["local_id"]])
            self.assertEqual(task.assigned_to_id, request.assigned_to_id)
            self.assertEqual(task.request_id, request.pk)
            self.assertEqual((request.status, task.status), ("new", "new"))
            self.assertEqual(request.due_at.isoformat(), payload["details"]["due_at"])
            self.assertEqual(task.due_at, request.due_at)
            self.assertEqual(request.tasks.count(), 1)
            self.assertTrue(task.is_automatic)
        self.assertContains(response, "Пакет мог быть создан", status_code=503)
        self.assertNotContains(response, "Пакет не подтверждён", status_code=503)
        self.assertNotContains(response, "Данные не созданы", status_code=503)
        self.assertNotContains(response, "/internal/path", status_code=503)
        self.assertNotContains(response, "Traceback", status_code=503)
        self.assertContains(response, "Повторить это же подтверждение", status_code=503)
        self.assertEqual(response.context["retry_form"]["snapshot"].value(), self.raw)
        self.assertIn("AFTER COMMIT", "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))
        replay = self.client.post(url, {"snapshot": response.context["retry_form"]["snapshot"].value(),
                                      "confirm_create": "on"})
        self.assertRedirects(replay, reverse("optimization_result", kwargs={"token": record.token}))
        self.assertEqual((ClientRequest.objects.count(), Task.objects.count()), after)
        self.assertEqual(OptimizationCommit.objects.count(), 1)
        self.assertEqual(OptimizationCommit.objects.get().created_ids, committed_ids)

    def test_two_real_connections_start_confirmation_concurrently(self):
        barrier=threading.Barrier(2)
        def worker():
            close_old_connections()
            try:
                actor=User.objects.get(pk=self.admin.pk)
                connection.cursor().execute("PRAGMA busy_timeout=100")
                barrier.wait(timeout=5)
                try:return ("ok",str(commit_snapshot(self.raw,actor).pk))
                except CommitError as error:
                    if error.status!=503 or not error.retry:raise
                    return ("busy",None)
            finally:connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:worker(),range(2)))
        self.assertIn("ok",[o[0] for o in outcomes])
        self.assertEqual(OptimizationCommit.objects.count(),1)
        self.assertEqual((ClientRequest.objects.count(),Task.objects.count()),(2,2))
        receipt=commit_snapshot(self.raw,self.admin)
        self.assertTrue(all(o[1]==str(receipt.pk) for o in outcomes if o[0]=="ok"))
        self.assertEqual((ClientRequest.objects.count(),Task.objects.count()),(2,2))

    def test_real_sqlite_writer_lock_is_retryable_then_succeeds_once(self):
        def worker():
            close_old_connections()
            try:
                connection.cursor().execute("PRAGMA busy_timeout=50")
                try:commit_snapshot(self.raw,self.admin)
                except CommitError as error:return error.status,error.retry
                return 200,False
            finally:connection.close()
        with transaction.atomic():
            OptimizationCommit.objects.create(token=uuid.uuid4(),created_by=self.admin,digest="0"*64)
            with ThreadPoolExecutor(max_workers=1) as pool:outcome=pool.submit(worker).result(timeout=5)
            transaction.set_rollback(True)
        self.assertEqual(outcome,(503,True))
        self.assertEqual(OptimizationCommit.objects.count(),0)
        receipt=commit_snapshot(self.raw,self.admin)
        self.assertEqual(len(receipt.created_ids),2)
        self.assertEqual(OptimizationCommit.objects.count(),1)


class OptimizationMigrationTests(TransactionTestCase):
    def test_0007_preserves_filled_0006_data_and_supports_idempotent_creation(self):
        executor=MigrationExecutor(connection)
        try:
            executor.migrate([("crm","0006_clientinteraction")])
            admin=User.objects.create_user("migration-admin",is_superuser=True)
            workers=(User.objects.create_user("migration-a"),User.objects.create_user("migration-b"))
            contacts=[Client.objects.create(full_name="Вымышленный клиент миграции")]
            old=ClientRequest.objects.create(client=contacts[0],title="Прежняя заявка миграции",assigned_to=workers[0])
            Task.objects.create(request=old,title="Прежняя задача миграции",assigned_to=workers[0])
            models=(User,Client,ClientRequest,Task,Deal,ClientInteraction)
            before={m:list(m.objects.order_by("pk").values()) for m in models}
            executor=MigrationExecutor(connection);executor.migrate([("crm","0007_optimizationcommit")])
            for model,rows in before.items():self.assertEqual(list(model.objects.order_by("pk").values()),rows)
            raw=prepared(admin,workers,contacts)
            first=commit_snapshot(raw,admin);second=commit_snapshot(raw,admin)
            self.assertEqual(first.pk,second.pk)
            self.assertEqual((ClientRequest.objects.count(),Task.objects.count()),(3,3))
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
