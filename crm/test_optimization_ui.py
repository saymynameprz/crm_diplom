"""HTTP boundary, permissions, exact M2 delegation and read-only recommendations."""
from decimal import Decimal
from fractions import Fraction as Q
from html.parser import HTMLParser
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import Client as TestClient, RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import OptimizationForm, OptimizationHoursField
from .models import Client, ClientRequest, Task, Deal, ClientInteraction
from .optimization import Result, Status, branch_and_bound, greedy
from .optimization_presentation import STATUS_TEXT, compare_results, display_number, present_result
from .views import optimization


class OptimizationMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.labels, self.references, self.controls, self.forms = [], [], [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "label":
            self.labels.append(attrs.get("for"))
        for key in ("aria-describedby", "aria-labelledby"):
            self.references.extend(attrs.get(key, "").split())
        if tag in ("input", "select") and attrs.get("type") != "hidden":
            self.controls.append(attrs.get("id"))
        if tag == "form":
            self.forms.append(attrs)


class OptimizationDisplayTests(SimpleTestCase):
    def test_every_core_status_has_a_distinct_russian_description(self):
        self.assertEqual(set(STATUS_TEXT), set(Status))
        self.assertEqual(len(set(STATUS_TEXT.values())), len(Status))
        for status in Status:
            shown = present_result(Result(status), {}, {})
            self.assertEqual(shown["status"], status.value)
            self.assertFalse(shown["complete"])
            self.assertIsNone(shown["metrics"])

    def test_display_rounds_without_changing_exact_values(self):
        number = Q(19, 26)
        self.assertEqual(display_number(number, 6), "0,730769")
        self.assertEqual(display_number(Q(1, 200)), "0,01")
        self.assertEqual(number, Q(19, 26))

    def test_hour_field_accepts_minutes_and_comma_but_never_rounds(self):
        field = OptimizationHoursField(positive=True)
        for raw, minutes in (("0:01", 1), ("2:15", 135), ("2.25", 135), ("2,25", 135), ("1000", 60000)):
            self.assertEqual(field.clean(raw), minutes)
        from django.forms import ValidationError
        for raw in ("0.01", "NaN", "Infinity", "1e3", "1/60", "1:60", "-1", "0", "1001", "9" * 10000):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValidationError):
                field.clean(raw)


class OptimizationInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("opt-admin", is_superuser=True, is_staff=False)
        cls.manager = User.objects.create_user("opt-manager")
        cls.other = User.objects.create_user("opt-other")
        cls.staff = User.objects.create_user("opt-staff", is_staff=True)
        cls.inactive = User.objects.create_user("opt-inactive", is_active=False, is_superuser=True)
        cls.contact = Client.objects.create(full_name="Вымышленный клиент М3А")
        cls.request_record = ClientRequest.objects.create(client=cls.contact, title="Прежняя заявка", assigned_to=cls.manager)
        Task.objects.create(request=cls.request_record, title="Прежняя задача", assigned_to=cls.manager)
        Deal.objects.create(client=cls.contact, title="Прежняя сделка", amount=Decimal("12.34"), assigned_to=cls.other)
        ClientInteraction.objects.create(client=cls.contact, created_by=cls.other,
            interaction_type="call", subject="Прежняя история", occurred_at=timezone.now())

    def setUp(self):
        self.url = reverse("optimization")
        self.client.force_login(self.admin)

    def data(self):
        return {"request_count": "2", "employee_count": "2", "alpha": "0.50", "beta": "0.50",
                "employee_0": str(self.manager.pk), "capacity_0": "2", "occupied_0": "0", "rate_0": "100",
                "employee_1": str(self.other.pk), "capacity_1": "2:15", "occupied_1": "0", "rate_1": "400",
                "request_id_0": "1", "title_0": "Планируемая работа А", "effort_0_0": "1", "effort_0_1": "1",
                "request_id_1": "2", "title_1": "Планируемая работа Б", "effort_1_0": "2", "effort_1_1": "2:15"}

    def trap(self):
        return {**self.data(), "capacity_0": "1", "capacity_1": "1", "rate_0": "60", "rate_1": "120",
                "effort_1_0": "1", "effort_1_1": ""}

    def snapshot(self):
        return {model._meta.label: list(model.objects.order_by("pk").values())
                for model in (User, Client, ClientRequest, Task, Deal, ClientInteraction)}

    def assert_invalid(self, data, field):
        with patch("crm.views.greedy") as g, patch("crm.views.branch_and_bound") as b:
            if hasattr(data, "urlencode"):
                response = self.client.post(self.url, data.urlencode(), content_type="application/x-www-form-urlencoded")
            else:
                response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context["form"].errors)
        self.assertNotIn("results", response.context)
        g.assert_not_called()
        b.assert_not_called()
        return response

    def test_active_superuser_without_staff_opens_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оптимизация заявок")
        self.assertContains(response, 'href="/optimization/"')
        self.assertNotIn("results", response.context)

    def test_anonymous_get_and_post_redirect_to_login(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            response = method(self.url)
            self.assertRedirects(response, "/login/?next=/optimization/", fetch_redirect_response=False)

    def test_both_managers_and_staff_are_forbidden_for_get_and_post(self):
        for actor in (self.manager, self.other, self.staff):
            self.client.force_login(actor)
            for method in (self.client.get, self.client.post):
                with self.subTest(actor=actor.username, method=method.__name__):
                    self.assertEqual(method(self.url, self.data()).status_code, 403)

    def test_inactive_superuser_cannot_access_even_if_attached_to_request(self):
        self.client.force_login(self.inactive)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        for method in ("get", "post"):
            request = getattr(RequestFactory(), method)(self.url)
            request.user = self.inactive
            with self.assertRaises(PermissionDenied):
                optimization(request)

    def test_navigation_link_is_only_visible_to_active_superuser(self):
        for actor in (self.admin, self.manager, self.staff):
            self.client.force_login(actor)
            response = self.client.get(reverse("home"))
            if actor.is_superuser:
                self.assertContains(response, 'href="/optimization/"')
            else:
                self.assertNotContains(response, 'href="/optimization/"')

    def test_unsupported_http_methods_return_405(self):
        for method in (self.client.put, self.client.delete):
            self.assertEqual(method(self.url).status_code, 405)

    def test_csrf_is_enforced_and_valid_multipart_post_succeeds(self):
        browser = TestClient(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        with patch("crm.views.branch_and_bound") as b:
            self.assertEqual(browser.post(self.url, self.data()).status_code, 403)
            b.assert_not_called()
        page = browser.get(self.url)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())[1]
        response = browser.post(self.url, {**self.data(), "csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"][1].status, Status.OPTIMAL)

    def test_m1_exact_results_assignments_and_display(self):
        response = self.client.post(self.url, self.data())
        g, b = response.context["results"]
        self.assertEqual((g.metrics.objective, b.metrics.objective), (Q(23, 26), Q(19, 26)))
        self.assertEqual((g.metrics.cost, b.metrics.cost), (1000, 600))
        self.assertEqual((g.metrics.labor_hours, b.metrics.labor_hours), (Q(13, 4), Q(3)))
        self.assertEqual(b.assignment, ((1, self.other.pk), (2, self.manager.pk)))
        self.assertEqual((g.metrics.objective - b.metrics.objective) / g.metrics.objective, Q(4, 23))
        self.assertContains(response, "19/26")
        self.assertContains(response, "600,00 ₽")
        self.assertContains(response, "17,39%")
        self.assertContains(response, "3,000000 чел.-ч")
        self.assertContains(response, "100,00%")
        self.assertContains(response, "OPTIMAL")

    def test_solver_receives_only_exact_validated_values_and_fixed_limits(self):
        with patch("crm.views.greedy", wraps=greedy) as g, patch("crm.views.branch_and_bound", wraps=branch_and_bound) as b:
            response = self.client.post(self.url, {**self.data(), "node_limit": "999999999999", "time_limit_seconds": "99999"})
        self.assertEqual(response.status_code, 200)
        problem = g.call_args.args[0]
        self.assertIs(b.call_args.args[0], problem)
        self.assertEqual(b.call_args.kwargs, {"node_limit": 10000, "time_limit_seconds": 1})
        self.assertIs(type(problem.alpha), Q)
        self.assertTrue(all(type(e.hourly_rate) is Q and type(e.capacity_minutes) is int for e in problem.employees))
        self.assertTrue(all(type(t) is int for r in problem.requests for _, t in r.efforts))

    def test_active_superuser_is_also_a_permitted_employee(self):
        response = self.client.post(self.url, {**self.data(), "employee_0": str(self.admin.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.admin.pk, {j for _, j in response.context["results"][1].assignment})

    def test_employee_choice_rejects_nonexistent_inactive_and_forged_ids(self):
        for raw in ("999999", str(self.inactive.pk), "-1", "1 OR 1=1", "9" * 1000):
            with self.subTest(raw=raw[:20]):
                self.assert_invalid({**self.data(), "employee_0": raw}, "employee_0")

    def test_employee_deactivated_after_get_is_rejected_on_post(self):
        self.client.get(self.url)
        User.objects.filter(pk=self.manager.pk).update(is_active=False)
        self.assert_invalid(self.data(), "employee_0")

    def test_repeated_employee_and_local_ids_are_field_errors(self):
        self.assert_invalid({**self.data(), "employee_1": str(self.manager.pk)}, "employee_1")
        self.assert_invalid({**self.data(), "request_id_1": "01"}, "request_id_1")

    def test_invalid_local_ids_and_title_lengths_are_rejected(self):
        for raw in ("0", "-1", "1.5", "1000000", "abc"):
            self.assert_invalid({**self.data(), "request_id_0": raw}, "request_id_0")
        for raw in ("", " " * 5, "a" * 121):
            self.assert_invalid({**self.data(), "title_0": raw}, "title_0")

    def test_zero_negative_and_unrepresentable_efforts_are_rejected(self):
        for raw in ("0", "-1", "NaN", "Infinity", "0.01", "1e3", "1001", "9" * 2000):
            with self.subTest(raw=raw[:20]):
                self.assert_invalid({**self.data(), "effort_0_0": raw}, "effort_0_0")

    def test_capacity_and_occupied_time_are_validated(self):
        for field, raw in (("capacity_0", "0"), ("capacity_0", "1001"), ("occupied_0", "-1"), ("occupied_0", "3")):
            self.assert_invalid({**self.data(), field: raw}, field)

    def test_rates_are_bounded_finite_nonnegative_decimal_values(self):
        for raw in ("-1", "NaN", "Infinity", "1e9", "1000000.01", "1.001", "9" * 1000):
            self.assert_invalid({**self.data(), "rate_0": raw}, "rate_0")

    def test_invalid_weights_and_inexact_sum_are_rejected(self):
        for key, raw in (("alpha", "-0.1"), ("alpha", "NaN"), ("alpha", "1.01"), ("beta", "0.49"), ("beta", "0.001")):
            self.assert_invalid({**self.data(), key: raw}, key)

    def test_endpoint_weights_and_zero_objective_have_safe_comparison(self):
        for alpha, beta in (("0", "1"), ("1", "0")):
            response = self.client.post(self.url, {**self.data(), "alpha": alpha, "beta": beta, "rate_0": "0", "rate_1": "0"})
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "NaN")
            if alpha == "1":
                self.assertEqual(response.context["results"][1].metrics.objective, 0)
                self.assertEqual(response.context["comparison"]["gain"], "0,00")

    def test_server_dimensions_cannot_be_enlarged_or_omitted(self):
        for changes in ({"request_count": "7"}, {"employee_count": "5"}, {"request_count": "0"},
                        {"employee_count": "-1"}, {"request_count": "9" * 500}, {"request_count": ""}):
            with patch("crm.views.branch_and_bound") as b:
                response = self.client.post(self.url, {**self.data(), **changes})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("form", response.context)
            b.assert_not_called()

    def test_get_size_selection_is_bounded_without_running_solver(self):
        response = self.client.get(self.url, {"request_count": "6", "employee_count": "4"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["request_rows"]), 6)
        self.assertEqual(len(response.context["employee_rows"]), 4)
        self.assertEqual(self.client.get(self.url, {"request_count": "100"}).status_code, 400)

    def test_maximum_six_by_four_input_is_accepted(self):
        users = (self.manager, self.other, self.admin, self.staff)
        data = {"request_count": "6", "employee_count": "4", "alpha": "0.5", "beta": "0.5"}
        for j, user in enumerate(users):
            data.update({f"employee_{j}": str(user.pk), f"capacity_{j}": "1000", f"occupied_{j}": "0", f"rate_{j}": "1000000"})
        for i in range(6):
            data.update({f"request_id_{i}": str(i + 1), f"title_{i}": "Вымышленная работа" * 5})
            data.update({f"effort_{i}_{j}": "0:01" for j in range(4)})
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["results"][1].assignment)
        self.assertLessEqual(response.context["results"][1].nodes_visited, 10000)

    def test_oversized_request_is_rejected_without_computation(self):
        with patch("crm.views.greedy") as g:
            response = self.client.post(self.url, {**self.data(), "title_0": "x" * 20000})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "16 КиБ", status_code=400)
        g.assert_not_called()

    def test_repeated_scalar_keys_are_rejected(self):
        from django.http import QueryDict
        data = QueryDict(mutable=True)
        data.update(self.data())
        data.setlist("alpha", ["0.1", "0.5"])
        self.assert_invalid(data, "alpha")
        data.setlist("request_count", ["99", "2"])
        response = self.client.post(self.url, data.urlencode(), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 400)

    def test_blank_qualification_row_is_proven_infeasible_not_invalid_form(self):
        response = self.client.post(self.url, {**self.data(), "effort_1_0": "", "effort_1_1": ""})
        g, b = response.context["results"]
        self.assertEqual((g.status, b.status), (Status.HEURISTIC_FAILED, Status.INFEASIBLE))
        self.assertIsNone(b.assignment)
        self.assertIsNone(response.context["comparison"])
        self.assertContains(response, "нет квалифицированного сотрудника")

    def test_packing_infeasibility_has_no_final_assignment_or_metrics(self):
        data = {**self.data(), "request_count": "3", "capacity_0": "3", "capacity_1": "3"}
        for i in range(3):
            data.update({f"request_id_{i}": str(i + 1), f"title_{i}": f"Работа {i + 1}",
                         f"effort_{i}_0": "2", f"effort_{i}_1": "2"})
        response = self.client.post(self.url, data)
        b = response.context["results"][1]
        self.assertEqual(b.status, Status.INFEASIBLE)
        self.assertTrue(b.infeasibility_proven)
        self.assertTrue(all(not r["complete"] and not r["assignments"] for r in response.context["recommendations"]))

    def test_global_search_rescues_greedy_trap(self):
        response = self.client.post(self.url, self.trap())
        g, b = response.context["results"]
        self.assertEqual((g.status, b.status), (Status.HEURISTIC_FAILED, Status.OPTIMAL))
        self.assertEqual(b.assignment, ((1, self.other.pk), (2, self.manager.pk)))
        self.assertTrue(response.context["rescued"])
        self.assertContains(response, "Глобальный поиск нашёл полный допустимый план")
        self.assertIsNone(response.context["comparison"])

    def test_equal_optima_use_canonical_ids_not_greedy_tie_vector(self):
        data = {**self.data(), "request_count": "4", "capacity_1": "2", "rate_0": "60", "rate_1": "60"}
        for i in range(4):
            data.update({f"request_id_{i}": str(i + 1), f"title_{i}": f"Работа {i + 1}",
                         f"effort_{i}_0": "1", f"effort_{i}_1": "1"})
        response = self.client.post(self.url, data)
        g, b = response.context["results"]
        a, z = self.manager.pk, self.other.pk
        self.assertEqual(tuple(j for _, j in g.assignment), (a, z, a, z))
        self.assertEqual(tuple(j for _, j in b.assignment), (a, a, z, z))
        self.assertEqual(g.metrics.objective, b.metrics.objective)

    def test_reordering_employee_columns_keeps_the_exact_assignment(self):
        first = self.client.post(self.url, self.data()).context["results"][1]
        data = self.data()
        for name in ("employee", "capacity", "occupied", "rate", "effort_0", "effort_1"):
            data[f"{name}_0"], data[f"{name}_1"] = data[f"{name}_1"], data[f"{name}_0"]
        second = self.client.post(self.url, data).context["results"][1]
        self.assertEqual((first.assignment, first.metrics), (second.assignment, second.metrics))

    def test_limit_with_plan_is_not_presented_as_optimal(self):
        def limited(problem, **kwargs):
            return branch_and_bound(problem, node_limit=0, time_limit_seconds=None)
        with patch("crm.views.branch_and_bound", side_effect=limited):
            response = self.client.post(self.url, self.data())
        b = response.context["results"][1]
        self.assertEqual(b.status, Status.FEASIBLE_NOT_PROVEN)
        self.assertFalse(b.optimality_proven)
        self.assertContains(response, "B&amp;B не доказал оптимальность")
        self.assertContains(response, "серверный лимит")
        self.assertTrue(response.context["recommendations"][1]["complete"])

    def test_limit_without_plan_is_unknown_not_infeasible(self):
        with patch("crm.views.branch_and_bound", side_effect=lambda p, **kw: branch_and_bound(p, node_limit=0)):
            response = self.client.post(self.url, self.trap())
        self.assertEqual(response.context["results"][1].status, Status.UNKNOWN)
        self.assertFalse(response.context["recommendations"][1]["complete"])
        self.assertNotContains(response, "INFEASIBLE")

    def test_time_guard_reports_undemonstrated_optimality(self):
        with patch("crm.optimization.time.monotonic_ns", side_effect=(0, 2_000_000_000)):
            response = self.client.post(self.url, self.data())
        self.assertEqual(response.context["results"][1].status, Status.FEASIBLE_NOT_PROVEN)
        self.assertContains(response, "защитный лимит времени")
        self.assertContains(response, "оптимальность не доказана")

    def test_technical_failure_is_generic_and_traceback_is_logged(self):
        secret = "private /internal/path token-value"
        with patch("crm.views.branch_and_bound", side_effect=RuntimeError(secret)), self.assertLogs("crm.views", level="ERROR") as logs:
            response = self.client.post(self.url, self.data())
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "TECHNICAL_ERROR", status_code=503)
        self.assertNotContains(response, secret, status_code=503)
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertIn(secret, "\n".join(logs.output))
        self.assertNotIn("results", response.context)

    def test_core_failure_reason_is_not_exposed_as_arbitrary_text(self):
        with patch("crm.views.branch_and_bound", return_value=Result(Status.INPUT_INVALID, termination_reason="private-path")):
            response = self.client.post(self.url, self.data())
        self.assertContains(response, "INPUT_INVALID")
        self.assertNotContains(response, "private-path")

    def test_residual_and_initial_load_use_expert_hours(self):
        response = self.client.post(self.url, {**self.data(), "occupied_0": "0:30"})
        first = response.context["employee_rows"][0]
        self.assertEqual(first["residual"], "1,500000")
        self.assertEqual(first["initial_load"], "25,00")
        self.assertContains(response, "не вычисляются по задачам CRM")

    def test_invalid_form_preserves_values_and_marks_nearby_errors(self):
        response = self.assert_invalid({**self.data(), "title_0": "Исправляемая работа", "occupied_0": "3"}, "occupied_0")
        self.assertContains(response, 'value="Исправляемая работа"')
        self.assertContains(response, 'value="3"')
        self.assertContains(response, 'id="id_occupied_0_error"')
        self.assertContains(response, 'href="#id_occupied_0"')
        self.assertContains(response, 'aria-invalid="true"')

    def test_titles_and_usernames_are_html_escaped(self):
        User.objects.filter(pk=self.manager.pk).update(username="<b>сотрудник</b>")
        response = self.client.post(self.url, {**self.data(), "title_0": '<script>alert("x")</script>'})
        self.assertNotContains(response, '<script>')
        self.assertNotContains(response, '<b>сотрудник</b>')
        self.assertContains(response, '&lt;script&gt;')
        self.assertContains(response, '&lt;b&gt;сотрудник&lt;/b&gt;')

    def test_business_tables_and_users_remain_identical_for_all_outcomes(self):
        before = self.snapshot()
        for data in (self.data(), self.trap(), {**self.data(), "rate_0": "-1"},
                     {**self.data(), "effort_1_0": "", "effort_1_1": ""}):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.post(self.url, data)
            self.assertEqual(response.status_code, 200)
            writes = [q["sql"] for q in queries if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", q["sql"], re.I)]
            self.assertEqual(writes, [])
            self.assertEqual(self.snapshot(), before)

    def test_recommendations_are_not_stored_in_session_or_get(self):
        before = dict(self.client.session)
        self.client.post(self.url, self.data())
        self.assertEqual(dict(self.client.session), before)
        response = self.client.get(self.url, {"title_0": "private-expert-title", "rate_0": "999"})
        self.assertNotContains(response, "private-expert-title")
        self.assertNotIn("recommendations", response.context)
        self.assertFalse(response.context["form"].is_bound)

    def test_responses_cannot_be_shared_cached(self):
        for response in (self.client.get(self.url), self.client.post(self.url, self.data())):
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("private", response["Cache-Control"])

    def test_semantic_labels_references_and_no_javascript_dependency(self):
        response = self.client.post(self.url, {**self.data(), "occupied_0": "3"})
        body = response.content.decode()
        self.assertNotIn("<script", body)
        markup = OptimizationMarkup()
        markup.feed(body)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertTrue(set(markup.controls) <= set(markup.labels))
        self.assertTrue(set(markup.labels + markup.references) <= set(markup.ids))
        main = [f for f in markup.forms if f.get("id") == "optimization-form"][0]
        self.assertEqual(main["method"], "post")
        self.assertEqual(main["action"], self.url)
        self.assertContains(response, 'tabindex="0"')
        self.assertContains(response, 'scope="col"')
