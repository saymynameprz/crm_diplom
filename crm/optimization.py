"""Exact, in-memory assignment model M1; minutes in, rubles/Fraction out.

There is no Django, storage or scheduling dependency.  A request's missing
employee edge means that the employee is not qualified.  Unexpected execution
errors deliberately propagate; only the explicitly validated input contract is
converted to INPUT_INVALID.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from itertools import product
import math
import time


class InputError(ValueError):
    """The input cannot be interpreted under the documented mathematical units."""


class _AssignmentError(InputError):
    """A complete assignment violates the model (an expected oracle rejection)."""


class Status(str, Enum):
    OPTIMAL = "OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    FEASIBLE_NOT_PROVEN = "FEASIBLE_NOT_PROVEN"
    UNKNOWN = "UNKNOWN"
    HEURISTIC_FAILED = "HEURISTIC_FAILED"
    INPUT_INVALID = "INPUT_INVALID"
    UNSUPPORTED_DEADLINE = "UNSUPPORTED_DEADLINE"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


@dataclass(frozen=True)
class Employee:
    id: int
    capacity_minutes: int
    occupied_minutes: int
    hourly_rate: Fraction


@dataclass(frozen=True)
class Request:
    id: int
    efforts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Problem:
    requests: tuple[Request, ...]
    employees: tuple[Employee, ...]
    alpha: Fraction = Fraction(1, 2)
    beta: Fraction = Fraction(1, 2)
    require_deadline_guarantee: bool = False


@dataclass(frozen=True)
class Metrics:
    cost: Fraction
    utilization: Fraction
    labor_hours: Fraction
    objective: Fraction
    cost_scale: Fraction


@dataclass(frozen=True)
class Result:
    status: Status
    assignment: tuple[tuple[int, int], ...] | None = None
    metrics: Metrics | None = None
    nodes_visited: int = 0
    termination_reason: str = ""
    optimality_proven: bool = False
    infeasibility_proven: bool = False
    lower_bound: Fraction | None = None


@dataclass(frozen=True)
class _Job:
    id: int
    # employee position, minutes, rubles; positions follow sorted stable IDs.
    options: tuple[tuple[int, int, Fraction], ...]


@dataclass(frozen=True)
class _Prepared:
    jobs: tuple[_Job, ...]
    employees: tuple[Employee, ...]
    residual: tuple[int, ...]
    alpha: Fraction
    beta: Fraction
    cost_scale: Fraction | None
    require_deadline_guarantee: bool


def _fraction(value, name):
    if type(value) is int or type(value) is Fraction:
        return Fraction(value)
    if type(value) is Decimal and value.is_finite():
        return Fraction(value)
    raise InputError(f"{name} must be int, Fraction or finite Decimal, never float/bool")


def _integer(value, name, *, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        suffix = "" if minimum is None else f" >= {minimum}"
        raise InputError(f"{name} must be an integer{suffix}, not bool/float")
    return value


def _sequence(value, name):
    if type(value) not in (tuple, list):
        raise InputError(f"{name} must be a tuple or list")
    return tuple(value)


def hours_to_minutes(value):
    """Convert nonnegative exact hours; reject a fraction of a minute, no rounding."""
    hours = _fraction(value, "hours")
    if hours < 0:
        raise InputError("hours must be nonnegative")
    minutes = hours * 60
    if minutes.denominator != 1:
        raise InputError("hours are not exactly representable as integer minutes")
    return minutes.numerator


def _prepare(problem):
    if not isinstance(problem, Problem):
        raise InputError("problem must be a Problem")
    alpha = _fraction(problem.alpha, "alpha")
    beta = _fraction(problem.beta, "beta")
    if not (0 <= alpha <= 1 and 0 <= beta <= 1 and alpha + beta == 1):
        raise InputError("weights must be in [0, 1] and sum exactly to one")
    if type(problem.require_deadline_guarantee) is not bool:
        raise InputError("require_deadline_guarantee must be bool")
    employees = []
    seen = set()
    for employee in _sequence(problem.employees, "employees"):
        if not isinstance(employee, Employee):
            raise InputError("each employee must be an Employee")
        identifier = _integer(employee.id, "employee ID")
        if identifier in seen:
            raise InputError("employee IDs must be unique")
        seen.add(identifier)
        capacity = _integer(employee.capacity_minutes, "capacity_minutes", minimum=1)
        occupied = _integer(employee.occupied_minutes, "occupied_minutes", minimum=0)
        if occupied > capacity:
            raise InputError("occupied_minutes exceeds full capacity_minutes")
        rate = _fraction(employee.hourly_rate, "hourly_rate")
        if rate < 0:
            raise InputError("hourly_rate must be nonnegative rubles per hour")
        employees.append(Employee(identifier, capacity, occupied, rate))
    employees.sort(key=lambda employee: employee.id)
    positions = {employee.id: j for j, employee in enumerate(employees)}
    jobs = []
    seen = set()
    for request in _sequence(problem.requests, "requests"):
        if not isinstance(request, Request):
            raise InputError("each request must be a Request")
        identifier = _integer(request.id, "request ID")
        if identifier in seen:
            raise InputError("request IDs must be unique")
        seen.add(identifier)
        options = []
        used = set()
        for pair in _sequence(request.efforts, "efforts"):
            pair = _sequence(pair, "effort edge")
            if len(pair) != 2:
                raise InputError("an effort edge must contain employee ID and minutes")
            employee_id = _integer(pair[0], "effort employee ID")
            if employee_id not in positions:
                raise InputError("an effort references an unknown employee ID")
            if employee_id in used:
                raise InputError("effort employee IDs must be unique within a request")
            used.add(employee_id)
            minutes = _integer(pair[1], "effort minutes", minimum=1)
            j = positions[employee_id]
            cost = Fraction(minutes, 60) * employees[j].hourly_rate
            options.append((j, minutes, cost))
        options.sort(key=lambda option: option[0])
        jobs.append(_Job(identifier, tuple(options)))
    jobs.sort(key=lambda job: job.id)
    # A missing qualification row is a mathematical obstruction, not invalid
    # input.  Its undefined maximum must not be used to invent a normalization.
    if any(not job.options for job in jobs):
        scale = None
    else:
        scale = sum((max(option[2] for option in job.options) for job in jobs), Fraction(0))
        if not scale:
            scale = Fraction(1)  # One ruble, including the empty/all-zero case.
    return _Prepared(
        tuple(jobs), tuple(employees),
        tuple(employee.capacity_minutes - employee.occupied_minutes for employee in employees),
        alpha, beta, scale, problem.require_deadline_guarantee,
    )


def _invalid(error):
    return Result(Status.INPUT_INVALID, termination_reason=str(error))


def _unsupported():
    return Result(Status.UNSUPPORTED_DEADLINE,
                  termination_reason="individual_deadline_guarantee_unsupported")


def _initial_utilization(data):
    return max((Fraction(employee.occupied_minutes, employee.capacity_minutes)
                for employee in data.employees), default=Fraction(0))


def _direct_evaluate(data, assignment):
    """Rebuild every load and metric from the full vector, never a search state."""
    pairs = _sequence(assignment, "assignment")
    if len(pairs) != len(data.jobs):
        raise _AssignmentError("assignment must contain every request exactly once")
    by_request = {}
    for pair in pairs:
        if type(pair) not in (tuple, list) or len(pair) != 2:
            raise _AssignmentError("assignment items must be request/employee ID pairs")
        request_id, employee_id = pair
        if type(request_id) is not int or type(employee_id) is not int:
            raise _AssignmentError("assignment IDs must be integers, not bool")
        if request_id in by_request:
            raise _AssignmentError("assignment request IDs must be unique")
        by_request[request_id] = employee_id
    if set(by_request) != {job.id for job in data.jobs}:
        raise _AssignmentError("assignment contains unknown or missing request IDs")
    loads = [0] * len(data.employees)
    cost = Fraction(0)
    for job in data.jobs:
        employee_id = by_request[job.id]
        option = next((option for option in job.options
                       if data.employees[option[0]].id == employee_id), None)
        if option is None:
            raise _AssignmentError("assignment violates qualification")
        j, minutes, rubles = option
        loads[j] += minutes
        cost += rubles
    if any(load > data.residual[j] for j, load in enumerate(loads)):
        raise _AssignmentError("assignment exceeds residual capacity")
    utilization = max((Fraction(employee.occupied_minutes + loads[j], employee.capacity_minutes)
                       for j, employee in enumerate(data.employees)), default=Fraction(0))
    labor_hours = Fraction(sum(loads), 60)
    if data.cost_scale is None:
        # A complete qualified assignment above cannot exist with an empty row.
        raise RuntimeError("complete assignment has no cost normalization")
    objective = data.alpha * cost / data.cost_scale + data.beta * utilization
    return Metrics(cost, utilization, labor_hours, objective, data.cost_scale)


def evaluate_assignment(problem, assignment):
    """Validate a full plan and compute exact C/U/T/F directly; errors raise InputError."""
    data = _prepare(problem)
    if data.require_deadline_guarantee:
        raise InputError("individual deadline guarantees are unsupported")
    return _direct_evaluate(data, assignment)


def _full_result(status, data, assignment, nodes, reason):
    assignment = tuple(sorted(assignment))
    return Result(status, assignment, _direct_evaluate(data, assignment), nodes, reason,
                  optimality_proven=status is Status.OPTIMAL)


def _structural_obstruction(data):
    if not data.jobs:
        return None
    if not data.employees:
        return "no_employees"
    if any(not job.options for job in data.jobs):
        return "no_qualified_employee"
    minimum_minutes = 0
    for job in data.jobs:
        fitting = [minutes for j, minutes, _ in job.options if minutes <= data.residual[j]]
        if not fitting:
            return "individual_capacity"
        minimum_minutes += min(fitting)
    if minimum_minutes > sum(data.residual):
        return "total_capacity"
    return None


def greedy(problem):
    """ID-order, no-backtracking heuristic minimizing the exact increment of M1 F."""
    try:
        data = _prepare(problem)
    except InputError as error:
        return _invalid(error)
    if data.require_deadline_guarantee:
        return _unsupported()
    if not data.jobs:
        return _full_result(Status.OPTIMAL, data, (), 0, "empty_problem")
    if data.cost_scale is None:
        return Result(Status.HEURISTIC_FAILED, termination_reason="no_qualified_employee")
    loads = [0] * len(data.employees)
    utilization = _initial_utilization(data)
    assignment = []
    for visited, job in enumerate(data.jobs, 1):
        candidates = []
        for j, minutes, cost in job.options:
            if loads[j] + minutes <= data.residual[j]:
                employee = data.employees[j]
                new_utilization = max(utilization, Fraction(
                    employee.occupied_minutes + loads[j] + minutes,
                    employee.capacity_minutes,
                ))
                increment = (data.alpha * cost / data.cost_scale
                             + data.beta * (new_utilization - utilization))
                candidates.append((increment, employee.id, j, minutes, new_utilization))
        if not candidates:
            return Result(Status.HEURISTIC_FAILED, nodes_visited=visited,
                          termination_reason=f"no_fitting_employee_for_request:{job.id}")
        _, employee_id, j, minutes, utilization = min(candidates)
        loads[j] += minutes
        assignment.append((job.id, employee_id))
    return _full_result(Status.FEASIBLE_NOT_PROVEN, data, tuple(assignment), len(data.jobs),
                        "greedy_complete")


def _state_bound(data, depth, loads, cost, utilization):
    remaining_cost = Fraction(0)
    for job in data.jobs[depth:]:
        eligible_costs = [rubles for j, minutes, rubles in job.options
                          if loads[j] + minutes <= data.residual[j]]
        if not eligible_costs:
            return None
        remaining_cost += min(eligible_costs)
    if data.cost_scale is None:
        return None
    return (data.alpha * (cost + remaining_cost) / data.cost_scale
            + data.beta * utilization)


def lower_bound(problem, prefix_employee_ids):
    """M1 lower bound for a valid prefix in sorted request-ID order.

    None is a certificate that some remaining request individually cannot fit.
    The bound does not assert joint feasibility of all remaining requests.
    Invalid prefix assignments raise InputError, rather than being search nodes.
    """
    data = _prepare(problem)
    if data.require_deadline_guarantee:
        raise InputError("individual deadline guarantees are unsupported")
    prefix = _sequence(prefix_employee_ids, "prefix_employee_ids")
    if len(prefix) > len(data.jobs):
        raise InputError("prefix is longer than the request sequence")
    loads = [0] * len(data.employees)
    cost = Fraction(0)
    for job, employee_id in zip(data.jobs, prefix):
        employee_id = _integer(employee_id, "prefix employee ID")
        option = next((option for option in job.options
                       if data.employees[option[0]].id == employee_id), None)
        if option is None:
            raise InputError("prefix violates qualification")
        j, minutes, rubles = option
        loads[j] += minutes
        cost += rubles
    if any(load > data.residual[j] for j, load in enumerate(loads)):
        raise InputError("prefix exceeds residual capacity")
    utilization = max((Fraction(employee.occupied_minutes + loads[j], employee.capacity_minutes)
                       for j, employee in enumerate(data.employees)), default=Fraction(0))
    return _state_bound(data, len(prefix), loads, cost, utilization)


def _time_limit_ns(value):
    if value is None:
        return None
    # A float is allowed ONLY for a wall-clock guard, never a model coefficient.
    if type(value) is float:
        if not math.isfinite(value):
            raise InputError("time_limit_seconds must be finite")
        seconds = Fraction(Decimal(str(value)))
    else:
        seconds = _fraction(value, "time_limit_seconds")
    if seconds < 0:
        raise InputError("time_limit_seconds must be nonnegative or None")
    nanoseconds = seconds * 1_000_000_000
    return nanoseconds.numerator // nanoseconds.denominator


def branch_and_bound(problem, *, node_limit=200_000, time_limit_seconds=30):
    """Exact DFS with a greedy incumbent, strict bound pruning and canonical ties.

    A visited node is an entered prefix, including root and complete leaves.
    Limits are checked BEFORE entering the next prefix.  Finishing the actual
    tree exactly at a node limit is still a proof, not a fictitious timeout.
    The cooperative time guard starts before validation and greedy seeding;
    validation/a single node cannot be interrupted midway. None disables time.
    """
    started = time.monotonic_ns()
    try:
        limit = _integer(node_limit, "node_limit", minimum=0)
        budget_ns = _time_limit_ns(time_limit_seconds)
        data = _prepare(problem)
    except InputError as error:
        return _invalid(error)
    if data.require_deadline_guarantee:
        return _unsupported()
    if not data.jobs:
        return _full_result(Status.OPTIMAL, data, (), 0, "empty_problem")
    obstruction = _structural_obstruction(data)
    if obstruction is not None:
        return Result(Status.INFEASIBLE, termination_reason=obstruction,
                      infeasibility_proven=True)

    seed = greedy(problem)
    best_assignment = seed.assignment
    best_metrics = (_direct_evaluate(data, best_assignment)
                    if best_assignment is not None else None)
    best_vector = (tuple(employee_id for _, employee_id in best_assignment)
                   if best_assignment is not None else None)
    loads = [0] * len(data.employees)
    vector = []
    nodes = 0
    stopped = None

    def visit(depth, cost, utilization):
        nonlocal nodes, stopped, best_assignment, best_metrics, best_vector
        if nodes >= limit:
            stopped = "node_limit"
            return
        if budget_ns is not None and time.monotonic_ns() - started >= budget_ns:
            stopped = "time_limit"
            return
        nodes += 1
        if depth == len(data.jobs):
            assignment = tuple((job.id, data.employees[j].id)
                               for job, j in zip(data.jobs, vector))
            # A full vector is re-evaluated independently of incremental cost/U.
            metrics = _direct_evaluate(data, assignment)
            if metrics.cost != cost or metrics.utilization != utilization:
                raise RuntimeError("incremental search state disagrees with complete evaluation")
            ids = tuple(employee_id for _, employee_id in assignment)
            if best_metrics is None or (metrics.objective, ids) < (best_metrics.objective, best_vector):
                best_assignment, best_metrics, best_vector = assignment, metrics, ids
            return
        bound = _state_bound(data, depth, loads, cost, utilization)
        if bound is None or (best_metrics is not None and bound > best_metrics.objective):
            return
        job = data.jobs[depth]
        candidates = []
        for j, minutes, rubles in job.options:
            if loads[j] + minutes <= data.residual[j]:
                employee = data.employees[j]
                new_utilization = max(utilization, Fraction(
                    employee.occupied_minutes + loads[j] + minutes,
                    employee.capacity_minutes,
                ))
                increment = (data.alpha * rubles / data.cost_scale
                             + data.beta * (new_utilization - utilization))
                candidates.append((increment, employee.id, j, minutes, rubles, new_utilization))
        for _, _, j, minutes, rubles, new_utilization in sorted(candidates):
            loads[j] += minutes
            vector.append(j)
            try:
                visit(depth + 1, cost + rubles, new_utilization)
            finally:
                vector.pop()
                loads[j] -= minutes
            if stopped is not None:
                return

    visit(0, Fraction(0), _initial_utilization(data))
    if stopped is not None:
        return Result(Status.FEASIBLE_NOT_PROVEN if best_assignment is not None else Status.UNKNOWN,
                      best_assignment, best_metrics, nodes, stopped)
    if best_assignment is None:
        return Result(Status.INFEASIBLE, nodes_visited=nodes,
                      termination_reason="search_exhausted", infeasibility_proven=True)
    return Result(Status.OPTIMAL, best_assignment, best_metrics, nodes, "search_exhausted",
                  optimality_proven=True)


def exhaustive_oracle(problem, *, max_assignments=1_000_000):
    """Independent all-vector oracle: no greedy, bound or incremental search state.

    The guard counts m**n complete vectors (including unqualified vectors), not
    search prefixes.  A request above this small-task budget is UNKNOWN and is
    never mislabeled INFEASIBLE.  Every enumerated full vector is checked from
    scratch against the definitions used by evaluate_assignment.
    """
    try:
        maximum = _integer(max_assignments, "max_assignments", minimum=0)
        data = _prepare(problem)
    except InputError as error:
        return _invalid(error)
    if data.require_deadline_guarantee:
        return _unsupported()
    if not data.jobs:
        return _full_result(Status.OPTIMAL, data, (), 0, "empty_problem")
    # Unlike B&B, do not use even the structural capacity/qualification
    # certificates: all nonempty-domain constraints are tested on full vectors.
    # With no employees, product has no vectors and proves infeasibility itself.
    combinations = 1
    for _ in data.jobs:
        combinations *= len(data.employees)
        if combinations > maximum:
            return Result(Status.UNKNOWN, termination_reason="oracle_size_limit")
    best = None
    visited = 0
    employee_ids = tuple(employee.id for employee in data.employees)
    for ids in product(employee_ids, repeat=len(data.jobs)):
        visited += 1
        assignment = tuple((job.id, employee_id) for job, employee_id in zip(data.jobs, ids))
        try:
            metrics = _direct_evaluate(data, assignment)
        except _AssignmentError:
            continue
        key = (metrics.objective, ids)
        if best is None or key < best[0]:
            best = key, assignment, metrics
    if best is None:
        return Result(Status.INFEASIBLE, nodes_visited=visited,
                      termination_reason="oracle_exhausted", infeasibility_proven=True)
    return Result(Status.OPTIMAL, best[1], best[2], visited, "oracle_exhausted",
                  optimality_proven=True)
