"""Exact mathematical checks, independent enumeration and synthetic input controls."""
import copy
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import itertools
import random
import unittest
from unittest.mock import patch

from .optimization import (
    Employee, InputError, Problem, Request, Status, branch_and_bound,
    evaluate_assignment, exhaustive_oracle, greedy, hours_to_minutes, lower_bound,
)


Q = Fraction


def problem(matrix, capacities, *, loads=None, rates=None, alpha=Q(1, 2), beta=Q(1, 2)):
    """Rows/columns have stable IDs; None means no qualification edge."""
    loads = loads if loads is not None else [0] * len(capacities)
    rates = rates if rates is not None else [60] * len(capacities)
    return Problem(
        requests=tuple(Request(i + 1, tuple((j + 1, minutes)
            for j, minutes in enumerate(row) if minutes is not None))
            for i, row in enumerate(matrix)),
        employees=tuple(Employee(j + 1, capacity, loads[j], rates[j])
            for j, capacity in enumerate(capacities)),
        alpha=alpha, beta=beta,
    )


def manual_example():
    return problem(((60, 60), (120, 135)), (120, 135), rates=(100, 400))


def independent_plans(data, prefix=()):
    """Test oracle from definitions, without any production metric/bound helpers."""
    workers = sorted(data.employees, key=lambda e: e.id)
    jobs = sorted(data.requests, key=lambda r: r.id)
    ids = tuple(e.id for e in workers)
    staff = {e.id: e for e in workers}
    times = {r.id: dict(r.efforts) for r in jobs}
    if any(not r.efforts for r in jobs):
        return []
    scale = sum((max(Q(minutes, 60) * Q(staff[j].hourly_rate)
        for j, minutes in r.efforts) for r in jobs), Q(0)) or Q(1)
    plans = []
    for tail in itertools.product(ids, repeat=len(jobs) - len(prefix)):
        vector = tuple(prefix) + tail
        loads = {e.id: 0 for e in workers}
        cost = Q(0)
        labor = 0
        for job, employee_id in zip(jobs, vector):
            minutes = times[job.id].get(employee_id)
            if minutes is None:
                break
            loads[employee_id] += minutes
            labor += minutes
            cost += Q(minutes, 60) * Q(staff[employee_id].hourly_rate)
        else:
            if all(loads[e.id] + e.occupied_minutes <= e.capacity_minutes for e in workers):
                utilization = max((Q(loads[e.id] + e.occupied_minutes, e.capacity_minutes)
                    for e in workers), default=Q(0))
                objective = Q(data.alpha) * cost / scale + Q(data.beta) * utilization
                assignment = tuple((r.id, j) for r, j in zip(jobs, vector))
                plans.append((objective, vector, assignment, cost, utilization, Q(labor, 60), scale))
    return sorted(plans)


class OptimizationInputTests(unittest.TestCase):
    def assert_invalid(self, data):
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            with self.subTest(method=solve.__name__):
                result = solve(data)
                self.assertEqual(result.status, Status.INPUT_INVALID)
                self.assertIsNone(result.assignment)
                self.assertIsNone(result.metrics)
                self.assertFalse(result.optimality_proven)
                self.assertFalse(result.infeasibility_proven)
                self.assertTrue(result.termination_reason)

    def test_empty_packet_has_zero_work_and_existing_load(self):
        for data, expected_u in ((problem((), (120,), loads=(30,)), Q(1, 4)),
                                 (problem((), ()), Q(0))):
            for solve in (greedy, branch_and_bound, exhaustive_oracle):
                with self.subTest(method=solve.__name__, workers=len(data.employees)):
                    result = solve(data)
                    self.assertEqual(result.status, Status.OPTIMAL)
                    self.assertEqual(result.assignment, ())
                    self.assertEqual(result.metrics.cost, 0)
                    self.assertEqual(result.metrics.labor_hours, 0)
                    self.assertEqual(result.metrics.utilization, expected_u)
                    self.assertEqual(result.metrics.objective, expected_u / 2)
                    self.assertTrue(result.optimality_proven)

    def test_no_employees_is_proven_infeasible_only_for_exact_methods(self):
        data = Problem((Request(1, ()),), ())
        self.assertEqual(greedy(data).status, Status.HEURISTIC_FAILED)
        for solve in (branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.status, Status.INFEASIBLE)
            self.assertTrue(result.infeasibility_proven)
            self.assertIsNone(result.assignment)
            self.assertIsNone(result.metrics)

    def test_one_request_one_employee(self):
        data = problem(((60,),), (120,), loads=(30,), rates=(Decimal("12.50"),))
        result = branch_and_bound(data, time_limit_seconds=None)
        self.assertEqual(result.assignment, ((1, 1),))
        self.assertEqual(result.metrics.cost, Q(25, 2))
        self.assertEqual(result.metrics.utilization, Q(3, 4))
        self.assertEqual(result.metrics.labor_hours, 1)
        self.assertEqual(result.metrics.objective, Q(7, 8))
        for value in (result.metrics.cost, result.metrics.utilization, result.metrics.labor_hours,
                      result.metrics.objective, result.metrics.cost_scale):
            self.assertIsInstance(value, Fraction)

    def test_duplicate_request_ids(self):
        data = problem(((1,), (1,)), (5,))
        self.assert_invalid(replace(data, requests=(data.requests[0], data.requests[0])))

    def test_duplicate_employee_ids(self):
        data = problem(((1,),), (5,))
        self.assert_invalid(replace(data, employees=(data.employees[0], data.employees[0])))

    def test_duplicate_qualification_edges(self):
        data = problem(((1,),), (5,))
        self.assert_invalid(replace(data, requests=(Request(1, ((1, 1), (1, 2))),)))

    def test_unknown_employee_in_qualification_edges(self):
        data = problem(((1,),), (5,))
        self.assert_invalid(replace(data, requests=(Request(1, ((99, 1),)),)))

    def test_non_integer_and_boolean_ids(self):
        data = problem(((1,),), (5,))
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                self.assert_invalid(replace(data, employees=(replace(data.employees[0], id=value),)))
                self.assert_invalid(replace(data, requests=(replace(data.requests[0], id=value),)))

    def test_negative_rate(self):
        self.assert_invalid(problem(((1,),), (5,), rates=(-1,)))

    def test_zero_and_negative_effort(self):
        for minutes in (0, -1):
            with self.subTest(minutes=minutes):
                self.assert_invalid(problem(((minutes,),), (5,)))

    def test_non_integer_or_boolean_minutes_are_rejected(self):
        for minutes in (1.0, Q(1, 2), Decimal("1"), True, "1"):
            with self.subTest(minutes=minutes):
                self.assert_invalid(problem(((minutes,),), (5,)))

    def test_invalid_full_capacity(self):
        for capacity in (0, -1, 1.0, Q(3, 2), True):
            with self.subTest(capacity=capacity):
                self.assert_invalid(problem(((1,),), (capacity,)))

    def test_invalid_existing_load(self):
        for load in (-1, 6, 1.0, Q(1, 2), True):
            with self.subTest(load=load):
                self.assert_invalid(problem(((1,),), (5,), loads=(load,)))

    def test_invalid_weights_and_non_unit_sum(self):
        for alpha, beta in ((-1, 2), (2, -1), (Q(1, 2), Q(1, 3)), (0, 0),
                            (True, 0), (0.5, Q(1, 2))):
            with self.subTest(alpha=alpha, beta=beta):
                self.assert_invalid(problem(((1,),), (5,), alpha=alpha, beta=beta))

    def test_nonfinite_and_float_rates_weights_are_rejected(self):
        for value in (float("nan"), float("inf"), 1.0, True,
                      Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                self.assert_invalid(problem(((1,),), (5,), rates=(value,)))
                self.assert_invalid(problem(((1,),), (5,), alpha=value))

    def test_exact_decimal_and_fraction_weights(self):
        data = problem(((30,),), (60,), rates=(Q(1, 3),),
                       alpha=Decimal("0.25"), beta=Q(3, 4))
        result = branch_and_bound(data, time_limit_seconds=None)
        self.assertEqual(result.metrics.cost, Q(1, 6))
        self.assertEqual(result.metrics.objective, Q(5, 8))

    def test_hours_conversion_is_exact(self):
        for hours, minutes in ((0, 0), (1, 60), (Q(9, 4), 135),
                               (Decimal("0.05"), 3), (Q(1, 60), 1)):
            with self.subTest(hours=hours):
                self.assertEqual(hours_to_minutes(hours), minutes)

    def test_hours_conversion_rejects_unrepresentable_or_ambiguous_values(self):
        for hours in (Q(1, 7), Decimal("0.001"), 1.0, True, "1", -1,
                      Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(hours=str(hours)):
                with self.assertRaises(InputError):
                    hours_to_minutes(hours)

    def test_individual_deadline_guarantee_is_unsupported(self):
        data = replace(problem(((1,),), (5,)), require_deadline_guarantee=True)
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            with self.subTest(method=solve.__name__):
                result = solve(data)
                self.assertEqual(result.status, Status.UNSUPPORTED_DEADLINE)
                self.assertIsNone(result.assignment)
                self.assertIsNone(result.metrics)
                self.assertFalse(result.infeasibility_proven)

    def test_statuses_distinguish_uncertainty_from_errors(self):
        expected = {"OPTIMAL", "INFEASIBLE", "FEASIBLE_NOT_PROVEN", "UNKNOWN",
                    "HEURISTIC_FAILED", "INPUT_INVALID", "UNSUPPORTED_DEADLINE", "TECHNICAL_ERROR"}
        self.assertTrue(expected <= {item.name for item in Status})


class OptimizationMathematicsTests(unittest.TestCase):
    def test_hand_example_greedy_exact_cost_hours_and_objective(self):
        result = greedy(manual_example())
        self.assertEqual(result.status, Status.FEASIBLE_NOT_PROVEN)
        self.assertEqual(result.assignment, ((1, 1), (2, 2)))
        self.assertEqual(result.metrics.cost, 1000)
        self.assertEqual(result.metrics.cost_scale, 1300)
        self.assertEqual(result.metrics.utilization, 1)
        self.assertEqual(result.metrics.labor_hours, Q(13, 4))
        self.assertEqual(result.metrics.objective, Q(23, 26))
        self.assertFalse(result.optimality_proven)

    def test_hand_example_exact_optimum_and_improvement(self):
        data = manual_example()
        for result in (branch_and_bound(data, time_limit_seconds=None), exhaustive_oracle(data)):
            self.assertEqual(result.status, Status.OPTIMAL)
            self.assertEqual(result.assignment, ((1, 2), (2, 1)))
            self.assertEqual(result.metrics.cost, 600)
            self.assertEqual(result.metrics.cost_scale, 1300)
            self.assertEqual(result.metrics.labor_hours, 3)
            self.assertEqual(result.metrics.utilization, 1)
            self.assertEqual(result.metrics.objective, Q(19, 26))
            self.assertTrue(result.optimality_proven)
        self.assertEqual((Q(23, 26) - Q(19, 26)) / Q(23, 26), Q(4, 23))

    def test_all_four_hand_vectors_and_independent_feasibility(self):
        rows = independent_plans(manual_example())
        self.assertEqual([(r[1], r[0]) for r in rows],
                         [((2, 1), Q(19, 26)), ((1, 2), Q(23, 26))])
        for vector in ((1, 1), (2, 2)):
            with self.assertRaises(InputError):
                evaluate_assignment(manual_example(), tuple(enumerate(vector, 1)))

    def test_existing_load_is_not_counted_twice(self):
        data = problem(((300,),), (480,), loads=(180,), rates=(100,))
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.assignment, ((1, 1),))
            self.assertEqual(result.metrics.labor_hours, 5)
            self.assertEqual(result.metrics.cost, 500)
            self.assertEqual(result.metrics.utilization, 1)

    def test_greedy_dead_end_does_not_prevent_exact_solution(self):
        data = problem(((60, 60), (60, None)), (60, 60))
        result = greedy(data)
        self.assertEqual(result.status, Status.HEURISTIC_FAILED)
        self.assertIsNone(result.assignment)
        self.assertIsNone(result.metrics)
        self.assertFalse(result.infeasibility_proven)
        for solve in (branch_and_bound, exhaustive_oracle):
            exact = solve(data)
            self.assertEqual(exact.status, Status.OPTIMAL)
            self.assertEqual(exact.assignment, ((1, 2), (2, 1)))

    def test_aggregate_capacity_is_not_sufficient(self):
        data = problem(((120, 120),) * 3, (180, 180))
        self.assertEqual(sum(min(dict(r.efforts).values()) for r in data.requests),
                         sum(e.capacity_minutes for e in data.employees))
        for solve in (branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.status, Status.INFEASIBLE)
            self.assertTrue(result.infeasibility_proven)
            self.assertIsNone(result.assignment)
            self.assertIsNone(result.metrics)

    def test_qualification_shortage_is_not_ignored(self):
        data = problem(((60, None), (60, None)), (60, 600))
        self.assertEqual(branch_and_bound(data).status, Status.INFEASIBLE)
        missing = problem(((None, None),), (60, 60))
        self.assertEqual(branch_and_bound(missing).status, Status.INFEASIBLE)
        self.assertEqual(exhaustive_oracle(missing).status, Status.INFEASIBLE)
        self.assertEqual(greedy(missing).status, Status.HEURISTIC_FAILED)

    def test_whole_request_cannot_be_split_across_workers(self):
        data = problem(((120, 120),), (60, 60))
        self.assertEqual(branch_and_bound(data).status, Status.INFEASIBLE)
        self.assertEqual(exhaustive_oracle(data).status, Status.INFEASIBLE)

    def test_zero_cost_uses_fixed_positive_scale(self):
        data = problem(((60, 60),), (60, 120), rates=(0, 0))
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.assignment, ((1, 2),))
            self.assertEqual(result.metrics.cost, 0)
            self.assertEqual(result.metrics.cost_scale, 1)
            self.assertEqual(result.metrics.utilization, Q(1, 2))
            self.assertEqual(result.metrics.objective, Q(1, 4))

    def test_canonical_tie_uses_stable_employee_ids(self):
        data = problem(((60, 60), (60, 60)), (60, 60))
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.assignment, ((1, 1), (2, 2)))

    def test_equal_bound_branch_can_improve_lexicographic_tie(self):
        data = problem(((2, 1), (3, 2)), (3, 2), alpha=1, beta=0)
        self.assertEqual(greedy(data).assignment, ((1, 2), (2, 1)))
        self.assertEqual(lower_bound(data, (1,)), Q(4, 5))
        for solve in (branch_and_bound, exhaustive_oracle):
            result = solve(data)
            self.assertEqual(result.assignment, ((1, 1), (2, 2)))
            self.assertEqual(result.metrics.objective, Q(4, 5))

    def test_weight_endpoints_change_the_selected_assignment(self):
        data = problem(((1, 1),), (1, 100), rates=(60, 120))
        cost_only = branch_and_bound(replace(data, alpha=1, beta=0))
        load_only = branch_and_bound(replace(data, alpha=0, beta=1))
        self.assertEqual(cost_only.assignment, ((1, 1),))
        self.assertEqual(cost_only.metrics.objective, Q(1, 2))
        self.assertEqual(load_only.assignment, ((1, 2),))
        self.assertEqual(load_only.metrics.objective, Q(1, 100))

    def test_reweighting_recomputes_objective_from_exact_metrics(self):
        data = manual_example()
        assignment = ((1, 2), (2, 1))
        for alpha in (Q(0), Q(1, 4), Q(1, 2), Q(3, 4), Q(1)):
            weighted = replace(data, alpha=alpha, beta=1 - alpha)
            metric = evaluate_assignment(weighted, assignment)
            self.assertEqual(metric.objective, alpha * Q(600, 1300) + (1 - alpha))

    def test_fully_loaded_worker_makes_maximum_utilization_constant(self):
        data = problem(((60, 60, 60),), (60, 120, 120), loads=(60, 0, 0), rates=(0, 100, 200))
        plans = independent_plans(data)
        self.assertEqual({r[4] for r in plans}, {Q(1)})
        result = branch_and_bound(data)
        self.assertEqual(result.assignment, ((1, 2),))
        self.assertEqual(result.metrics.utilization, 1)
        self.assertEqual(branch_and_bound(problem(((1,),), (60,), loads=(60,))).status,
                         Status.INFEASIBLE)

    def test_unused_expensive_qualified_alternative_changes_fixed_normalization(self):
        base = problem(((1, 1),), (1, 100), rates=(60, 120), alpha=Q(4, 5), beta=Q(1, 5))
        expanded = problem(((1, 1, 100),), (1, 100, 1), rates=(60, 120, 600),
                           alpha=Q(4, 5), beta=Q(1, 5))
        first = branch_and_bound(base)
        second = branch_and_bound(expanded)
        self.assertEqual(first.assignment, ((1, 1),))
        self.assertEqual(first.metrics.cost_scale, 2)
        self.assertEqual(first.metrics.objective, Q(3, 5))
        self.assertEqual(second.assignment, ((1, 2),))
        self.assertEqual(second.metrics.cost_scale, 1000)
        self.assertEqual(second.metrics.objective, Q(9, 2500))

    def test_input_order_is_not_a_hidden_preference(self):
        data = manual_example()
        permuted = replace(data, requests=tuple(replace(r, efforts=tuple(reversed(r.efforts)))
            for r in reversed(data.requests)), employees=tuple(reversed(data.employees)))
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            self.assertEqual(solve(data), solve(permuted))

    def test_calls_do_not_mutate_any_input(self):
        data = manual_example()
        original = copy.deepcopy(data)
        for solve in (greedy, branch_and_bound, exhaustive_oracle):
            solve(data)
            self.assertEqual(data, original)
        evaluate_assignment(data, ((1, 2), (2, 1)))
        lower_bound(data, (1,))
        self.assertEqual(data, original)

    def test_assignment_evaluator_rejects_missing_duplicate_and_foreign_jobs(self):
        data = manual_example()
        for assignment in ((), ((1, 1),), ((1, 1), (1, 2)), ((1, 1), (99, 2)),
                           ((1, 99), (2, 1)), ((1, 1), (2, 1))):
            with self.subTest(assignment=assignment):
                with self.assertRaises(InputError):
                    evaluate_assignment(data, assignment)


class BranchAndBoundProofTests(unittest.TestCase):
    def test_node_limit_with_incumbent_does_not_claim_optimality(self):
        for limit in (0, 1):
            with self.subTest(limit=limit):
                result = branch_and_bound(manual_example(), node_limit=limit, time_limit_seconds=None)
                self.assertEqual(result.status, Status.FEASIBLE_NOT_PROVEN)
                self.assertEqual(result.nodes_visited, limit)
                self.assertEqual(result.metrics.objective, Q(23, 26))
                self.assertFalse(result.optimality_proven)
                self.assertFalse(result.infeasibility_proven)
                self.assertIsNone(result.lower_bound)

    def test_node_limit_without_incumbent_is_unknown(self):
        data = problem(((60, 60), (60, None)), (60, 60))
        result = branch_and_bound(data, node_limit=0, time_limit_seconds=None)
        self.assertEqual(result.status, Status.UNKNOWN)
        self.assertEqual(result.nodes_visited, 0)
        self.assertIsNone(result.assignment)
        self.assertIsNone(result.metrics)
        self.assertFalse(result.infeasibility_proven)

    def test_finite_node_count_includes_root_and_leaf_without_false_limit(self):
        data = problem(((1,),), (1,))
        exact = branch_and_bound(data, node_limit=2, time_limit_seconds=None)
        self.assertEqual(exact.status, Status.OPTIMAL)
        self.assertEqual(exact.nodes_visited, 2)
        limited = branch_and_bound(data, node_limit=1, time_limit_seconds=None)
        self.assertEqual(limited.status, Status.FEASIBLE_NOT_PROVEN)
        self.assertEqual(limited.nodes_visited, 1)

    def test_zero_time_budget_never_claims_optimality(self):
        seeded = branch_and_bound(manual_example(), time_limit_seconds=0)
        self.assertEqual(seeded.status, Status.FEASIBLE_NOT_PROVEN)
        self.assertEqual(seeded.nodes_visited, 0)
        trap = problem(((60, 60), (60, None)), (60, 60))
        unseeded = branch_and_bound(trap, time_limit_seconds=0)
        self.assertEqual(unseeded.status, Status.UNKNOWN)
        self.assertIsNone(unseeded.assignment)

    def test_time_limit_uses_monotonic_clock_and_includes_initial_greedy(self):
        with patch('crm.optimization.time.monotonic_ns', side_effect=[0] + [1_000_000_000] * 30) as clock:
            result = branch_and_bound(manual_example(), time_limit_seconds=1)
        self.assertEqual(result.status, Status.FEASIBLE_NOT_PROVEN)
        self.assertEqual(result.nodes_visited, 0)
        self.assertEqual(result.metrics.cost, 1000)
        self.assertGreaterEqual(clock.call_count, 2)

    def test_invalid_search_limits_are_input_errors(self):
        for limit in (-1, True, Q(1, 2), 1.0):
            with self.subTest(limit=limit):
                result = branch_and_bound(manual_example(), node_limit=limit)
                self.assertEqual(result.status, Status.INPUT_INVALID)
        for limit in (-1, float('nan'), float('inf'), True):
            with self.subTest(seconds=limit):
                result = branch_and_bound(manual_example(), time_limit_seconds=limit)
                self.assertEqual(result.status, Status.INPUT_INVALID)

    def test_unexpected_runtime_exception_is_not_disguised_as_normal_status(self):
        with patch('crm.optimization.time.monotonic_ns', side_effect=RuntimeError('clock failure')):
            with self.assertRaisesRegex(RuntimeError, 'clock failure'):
                branch_and_bound(manual_example())

    def test_repeated_node_limited_runs_are_deterministic_and_keep_valid_incumbent(self):
        data = manual_example()
        first = branch_and_bound(data, node_limit=3, time_limit_seconds=None)
        for _ in range(3):
            self.assertEqual(branch_and_bound(data, node_limit=3, time_limit_seconds=None), first)
        if first.assignment is not None:
            metric = evaluate_assignment(data, first.assignment)
            self.assertEqual(metric, first.metrics)

    def test_oracle_size_guard_is_unknown_and_never_a_false_infeasibility(self):
        data = problem(((1, 1), (1, 1)), (2, 2))
        guarded = exhaustive_oracle(data, max_assignments=1)
        self.assertEqual(guarded.status, Status.UNKNOWN)
        self.assertIsNone(guarded.assignment)
        self.assertIsNone(guarded.metrics)
        self.assertEqual(guarded.nodes_visited, 0)
        self.assertFalse(guarded.infeasibility_proven)

    def test_bound_rejects_invalid_or_over_capacity_prefix(self):
        for prefix in ((99,), (1, 1), (1, 2, 1)):
            with self.subTest(prefix=prefix):
                with self.assertRaises(InputError):
                    lower_bound(manual_example(), prefix)

    def test_bound_can_certify_a_dead_branch_but_not_the_whole_problem(self):
        data = problem(((60, 60), (60, None)), (60, 60))
        self.assertIsNone(lower_bound(data, (1,)))
        self.assertIsNotNone(lower_bound(data, (2,)))
        self.assertEqual(branch_and_bound(data).status, Status.OPTIMAL)

    def test_completed_prefix_bound_is_the_full_objective(self):
        self.assertEqual(lower_bound(manual_example(), (2, 1)), Q(19, 26))

    def test_many_deterministic_small_problems_match_independent_enumeration(self):
        rng = random.Random(20261028)
        for case in range(120):
            n, m = rng.randint(1, 4), rng.randint(1, 3)
            capacities = tuple(rng.randint(2, 8) for _ in range(m))
            loads = tuple(rng.randint(0, capacity) for capacity in capacities)
            rates = tuple(rng.choice((0, 1, 3, 60)) for _ in range(m))
            matrix = tuple(tuple(rng.randint(1, 4) if rng.randrange(4) else None
                                 for _ in range(m)) for _ in range(n))
            alpha = rng.choice((Q(0), Q(1, 4), Q(1, 2), Q(1)))
            data = problem(matrix, capacities, loads=loads, rates=rates, alpha=alpha, beta=1 - alpha)
            expected = independent_plans(data)
            with self.subTest(case=case, n=n, m=m):
                actual = branch_and_bound(data, time_limit_seconds=None)
                oracle = exhaustive_oracle(data)
                if expected:
                    best = expected[0]
                    for result in (actual, oracle):
                        self.assertEqual(result.status, Status.OPTIMAL)
                        self.assertEqual(result.assignment, best[2])
                        self.assertEqual((result.metrics.objective, result.metrics.cost,
                            result.metrics.utilization, result.metrics.labor_hours,
                            result.metrics.cost_scale), (best[0], best[3], best[4], best[5], best[6]))
                else:
                    self.assertEqual(actual.status, Status.INFEASIBLE)
                    self.assertEqual(oracle.status, Status.INFEASIBLE)

    def test_bounds_on_many_small_prefixes_never_exceed_best_completion(self):
        rng = random.Random(20261029)
        checked = 0
        for _ in range(30):
            n, m = 3, 2
            capacities = tuple(rng.randint(3, 9) for _ in range(m))
            matrix = tuple(tuple(rng.randint(1, 4) for _ in range(m)) for _ in range(n))
            alpha = rng.choice((Q(0), Q(1, 2), Q(1)))
            data = problem(matrix, capacities, rates=(30, 120), alpha=alpha, beta=1 - alpha)
            for depth in range(n + 1):
                for prefix in itertools.product(range(1, m + 1), repeat=depth):
                    # Prefix feasibility is computed directly, without the production bound.
                    used = [0] * m
                    for i, j in enumerate(prefix):
                        used[j - 1] += matrix[i][j - 1]
                    if any(used[j] > capacities[j] for j in range(m)):
                        continue
                    completions = independent_plans(data, prefix)
                    bound = lower_bound(data, prefix)
                    if completions:
                        self.assertIsNotNone(bound)
                        self.assertLessEqual(bound, completions[0][0])
                        checked += 1
        self.assertGreater(checked, 100)


class OracleIndependenceTests(unittest.TestCase):
    def test_oracle_does_not_use_greedy_or_branch_bound(self):
        with patch('crm.optimization.greedy', side_effect=AssertionError('greedy called')), \
             patch('crm.optimization._state_bound', side_effect=AssertionError('bound called')):
            result = exhaustive_oracle(manual_example())
        self.assertEqual(result.status, Status.OPTIMAL)
        self.assertEqual(result.assignment, ((1, 2), (2, 1)))
        self.assertEqual(result.nodes_visited, 4)

    def test_oracle_unexpected_evaluation_error_is_visible(self):
        with patch('crm.optimization._direct_evaluate', side_effect=RuntimeError('unexpected defect')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected defect'):
                exhaustive_oracle(manual_example())


class SyntheticGeneratorTests(unittest.TestCase):
    def test_fixed_initial_seed_content_and_fingerprints(self):
        from scripts.run_optimization_experiment import generate_case

        first = generate_case(3, 2, seed=20261028)
        expected = problem(((480, 240), (360, 180), (120, 60)), (2400, 2400),
                           loads=(1200, 0), rates=(400, 400))
        self.assertEqual(first.problem, expected)
        self.assertEqual(first.witness, ((1, 2), (2, 2), (3, 2)))
        self.assertEqual(first.metadata['seed_text'], 'M2-v1:3:2:2:feasible:3/4:20261028')
        self.assertEqual(first.fingerprint,
                         '4aef2ef55c7830cedfa4305192500a992b398de5bf2988c0e26919828fdbe466')
        self.assertEqual(generate_case(3, 2, seed=20261029).fingerprint,
                         '834633c473888ee30f264ceee40f3bf54f1ab6dd5cdf7cf5eca6107c39d4b662')
        self.assertEqual(generate_case(3, 2, seed=20261028, family='stress').fingerprint,
                         'c56be660be0389f90be7e1ab74d47ae48069d477d3a87a3a8d42b864873291da')

    def test_all_twenty_fixed_seeds_have_independently_valid_feasible_witnesses(self):
        from scripts.run_optimization_experiment import SEEDS, generate_case

        self.assertEqual(SEEDS, tuple(range(20261028, 20261048)))
        for seed in SEEDS:
            with self.subTest(seed=seed):
                case = generate_case(3, 2, seed=seed, rho=Q(1, 4))
                plans = independent_plans(case.problem)
                by_assignment = {row[2]: row for row in plans}
                self.assertIn(case.witness, by_assignment)
                self.assertTrue(case.metadata['witness_verified'])
                for worker in case.problem.employees:
                    self.assertIs(type(worker.capacity_minutes), int)
                    self.assertIs(type(worker.occupied_minutes), int)
                self.assertTrue(all(type(minutes) is int for request in case.problem.requests
                                    for _, minutes in request.efforts))

    def test_generator_repeats_exactly_and_does_not_use_global_random_state(self):
        from scripts.run_optimization_experiment import generate_case

        global_state = random.getstate()
        first = generate_case(5, 3, seed=20261028, categories=4, family='stress')
        second = generate_case(5, 3, seed=20261028, categories=4, family='stress')
        self.assertEqual(first, second)
        self.assertEqual(random.getstate(), global_state)
        with self.assertRaises(TypeError):
            first.metadata['seed'] = 0

    def test_stress_family_never_repairs_missing_qualification(self):
        from scripts.run_optimization_experiment import generate_case

        stress = generate_case(3, 2, rho=Q(0), family='stress')
        self.assertIsNone(stress.witness)
        self.assertFalse(stress.metadata['witness_verified'])
        self.assertTrue(all(request.efforts == () for request in stress.problem.requests))
        self.assertEqual(branch_and_bound(stress.problem).status, Status.INFEASIBLE)
        controlled = generate_case(3, 2, rho=Q(0), family='feasible')
        self.assertIsNotNone(controlled.witness)
        self.assertIn(controlled.witness, {row[2] for row in independent_plans(controlled.problem)})

    def test_generator_rejects_ambiguous_or_invalid_arguments(self):
        from scripts.run_optimization_experiment import generate_case

        for kwargs in ({'n': -1}, {'m': -1}, {'categories': 0}, {'seed': True},
                       {'family': 'unknown'}, {'rho': 0.75}, {'rho': Decimal('NaN')},
                       {'rho': Decimal('Infinity')}, {'rho': -1}, {'rho': 2},
                       {'n': True}, {'m': Q(2)}, {'categories': 1.0}):
            with self.subTest(kwargs=kwargs):
                args = {'n': 3, 'm': 2}
                args.update(kwargs)
                with self.assertRaises(ValueError):
                    generate_case(**args)
        with self.assertRaises(ValueError):
            generate_case(1, 0, family='feasible')
        self.assertEqual(generate_case(0, 0).witness, ())

    def test_canonical_serialization_is_stable_under_input_permutations(self):
        from scripts.run_optimization_experiment import canonical_problem, problem_fingerprint

        data = manual_example()
        permuted = replace(data, requests=tuple(replace(request,
            efforts=tuple(reversed(request.efforts))) for request in reversed(data.requests)),
            employees=tuple(reversed(data.employees)))
        self.assertEqual(canonical_problem(data), canonical_problem(permuted))
        self.assertEqual(problem_fingerprint(data), problem_fingerprint(permuted))
        changed = replace(data, employees=(replace(data.employees[0], occupied_minutes=1),
                                           data.employees[1]))
        self.assertNotEqual(problem_fingerprint(data), problem_fingerprint(changed))

    def test_control_case_catalog_preserves_required_research_cases(self):
        from scripts.run_optimization_experiment import control_cases

        controls = dict(control_cases())
        self.assertTrue({'m1', 'greedy_trap', 'infeasible_packing', 'rare_skills',
                         'imbalanced_load', 'symmetric', 'zero_cost', 'saturated_utilization',
                         'normalization_outlier'} <= set(controls))
        self.assertEqual(controls['m1'], manual_example())
        self.assertEqual(branch_and_bound(controls['infeasible_packing']).status, Status.INFEASIBLE)
        self.assertEqual(greedy(controls['greedy_trap']).status, Status.HEURISTIC_FAILED)
        self.assertEqual(branch_and_bound(controls['greedy_trap']).status, Status.OPTIMAL)


class ExperimentInterfaceTests(unittest.TestCase):
    def test_small_repeated_experiment_has_same_exact_signature_and_oracle(self):
        from scripts.run_optimization_experiment import run_experiment

        options = dict(preset='smoke', seed_count=1, max_cases=2, node_limit=100,
                       time_limit_seconds=2, repeats=2, total_time_limit_seconds=5)
        first = run_experiment(**options)
        second = run_experiment(**options)
        self.assertEqual(first['comparison_signature'], second['comparison_signature'])
        self.assertEqual(first['coverage']['completed_cases'], 2)
        self.assertEqual(first['coverage']['stop_reason'], 'case_cap')
        m1 = first['cases'][0]
        self.assertEqual(m1['name'], 'm1')
        for method in ('greedy', 'branch_and_bound', 'oracle'):
            self.assertTrue(m1[method]['repeats_match'])
            self.assertEqual(len(m1[method]['runs']), 2)
            timing = m1[method]['wall_seconds']
            self.assertLessEqual(timing['min'], timing['median'])
            self.assertLessEqual(timing['median'], timing['max'])
        self.assertEqual(m1['branch_and_bound']['result']['assignment'],
                         m1['oracle']['result']['assignment'])
        self.assertEqual(m1['relative_gain_objective']['numerator'], 4)
        self.assertEqual(m1['relative_gain_objective']['denominator'], 23)
        self.assertEqual(first['summary']['greedy_failed_bnb_found'], 1)

    def test_relative_gain_handles_missing_plans_and_zero_denominators(self):
        from scripts.run_optimization_experiment import _gain

        def result(number):
            return {'metrics': None if number is None else {'objective': {
                'numerator': number.numerator, 'denominator': number.denominator}}}
        self.assertIsNone(_gain(result(None), result(Q(1))))
        self.assertIsNone(_gain(result(Q(1)), result(None)))
        self.assertIsNone(_gain(result(Q(0)), result(Q(1))))
        zero = _gain(result(Q(0)), result(Q(0)))
        self.assertEqual((zero['numerator'], zero['denominator']), (0, 1))
        worse = _gain(result(Q(1)), result(Q(2)))
        self.assertEqual((worse['numerator'], worse['denominator']), (-1, 1))

    def test_output_guard_rejects_checkout_and_symlink_into_checkout(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts.run_optimization_experiment import ROOT, _safe_output

        for target in (ROOT, ROOT / 'artifacts' / 'report.json', ROOT / 'docs' / 'report.json'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                _safe_output(str(target))
        with TemporaryDirectory(prefix='crm-optimization-test-') as directory:
            root = Path(directory)
            self.assertEqual(_safe_output(str(root / 'result.json')), root / 'result.json')
            (root / 'checkout-link').symlink_to(ROOT, target_is_directory=True)
            with self.assertRaises(ValueError):
                _safe_output(str(root / 'checkout-link' / 'result.json'))

    def test_cli_rejects_unsafe_parameters_without_running_an_experiment(self):
        import contextlib
        import io
        from scripts.run_optimization_experiment import ROOT, main

        cases = (['--seed-count', '21'], ['--max-cases', '0'], ['--node-limit', '-1'],
                 ['--time-limit', 'nan'], ['--time-limit', 'not-a-number'],
                 ['--time-limit', '0'], ['--output', str(ROOT / 'result.json')])
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                 patch('scripts.run_optimization_experiment.run_experiment') as runner:
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
                runner.assert_not_called()

    def test_cli_can_write_one_optional_report_only_outside_checkout(self):
        import contextlib
        import io
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts.run_optimization_experiment import main

        with TemporaryDirectory(prefix='crm-optimization-cli-test-') as directory:
            target = Path(directory) / 'report.json'
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--preset', 'smoke', '--seed-count', '1', '--max-cases', '1',
                             '--node-limit', '100', '--time-limit', '2', '--total-time-limit', '5',
                             '--output', str(target)])
            self.assertEqual(code, 0)
            report = json.loads(target.read_text())
            self.assertEqual(report['schema'], 'M2-experiment-v1')
            self.assertEqual(report['coverage']['completed_cases'], 1)
            self.assertEqual(report['cases'][0]['name'], 'm1')
