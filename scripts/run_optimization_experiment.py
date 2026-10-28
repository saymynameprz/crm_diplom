"""Reproducible synthetic M2 research; no Django, database, network or default files."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time
from types import MappingProxyType
from typing import Mapping

# Support direct execution from any directory without installing the CRM package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crm.optimization import (  # noqa: E402
    Employee, Problem, Request, Status, branch_and_bound, evaluate_assignment,
    exhaustive_oracle, greedy, hours_to_minutes,
)

SEEDS = tuple(range(20261028, 20261048))
GENERATOR_VERSION = "M2-v1"


@dataclass(frozen=True)
class GeneratedCase:
    problem: Problem
    witness: tuple[tuple[int, int], ...] | None
    fingerprint: str
    metadata: Mapping[str, object]


def _fraction(value) -> Fraction:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Exact int, Fraction or finite Decimal required")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("Finite number required")
    if not isinstance(value, (int, Fraction, Decimal)):
        raise ValueError("Exact int, Fraction or finite Decimal required")
    return Fraction(value)


def _ratio(value) -> str:
    number = _fraction(value)
    return f"{number.numerator}/{number.denominator}"


def canonical_problem(problem: Problem) -> dict:
    """Canonical mathematical input, independent of source tuple ordering."""
    return {
        "units": {"time": "integer_minutes", "rate": "RUB_per_hour"},
        "requests": [
            {"id": request.id, "efforts": [list(pair) for pair in sorted(request.efforts)]}
            for request in sorted(problem.requests, key=lambda item: item.id)
        ],
        "employees": [
            {"id": employee.id, "capacity_minutes": employee.capacity_minutes,
             "occupied_minutes": employee.occupied_minutes,
             "hourly_rate": _ratio(employee.hourly_rate)}
            for employee in sorted(problem.employees, key=lambda item: item.id)
        ],
        "alpha": _ratio(problem.alpha), "beta": _ratio(problem.beta),
        "require_deadline_guarantee": problem.require_deadline_guarantee,
    }


def problem_fingerprint(problem: Problem) -> str:
    encoded = json.dumps(canonical_problem(problem), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def generate_case(n: int, m: int, *, seed: int = SEEDS[0], categories: int = 2,
                  family: str = "feasible", rho: Fraction = Fraction(3, 4)) -> GeneratedCase:
    """Generate M1's fixed sequence, repairing only the named feasible family.

    All uniform choices use Random.choice/randrange on stated ordered sets;
    skill Bernoulli draws use integer randrange, never a float threshold.
    """
    for name, value, minimum in (("n", n, 0), ("m", m, 0), ("categories", categories, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if family not in ("feasible", "stress"):
        raise ValueError("family must be feasible or stress")
    rho = _fraction(rho)
    if not 0 <= rho <= 1:
        raise ValueError("rho must be in [0, 1]")
    if family == "feasible" and n and not m:
        raise ValueError("A nonempty feasible family requires employees")
    seed_text = f"{GENERATOR_VERSION}:{n}:{m}:{categories}:{family}:{_ratio(rho)}:{seed}"
    rng = random.Random(seed_text)
    request_categories, base_hours = [], []
    for _ in range(n):
        request_categories.append(rng.randrange(categories))
        base_hours.append(rng.choice((1, 2, 3, 4)))
    rates, capacities, loads, speeds = [], [], [], []
    for _ in range(m):
        rates.append(Fraction(rng.choice((100, 200, 400))))
        capacities.append(60 * rng.choice((8, 16, 40)))
        loads.append(rng.choice((Fraction(0), Fraction(1, 4), Fraction(1, 2), Fraction(3, 4))))
        speeds.append(tuple(rng.choice((Fraction(1, 2), Fraction(1), Fraction(3, 2)))
                            for _ in range(categories)))
    skills = [[rng.randrange(rho.denominator) < rho.numerator for _ in range(categories)]
              for _ in range(m)]
    witness = None
    if family == "feasible":
        owners = tuple(rng.randrange(m) for _ in range(n))
        work = [0] * m
        for i, owner in enumerate(owners):
            category = request_categories[i]
            skills[owner][category] = True
            work[owner] += hours_to_minutes(Fraction(base_hours[i]) / speeds[owner][category])
        for j in range(m):
            # Equivalent to 8*ceil(W_hours / (8*(1-load))) hours, exactly.
            required = 480 * math.ceil(Fraction(work[j], 480) / (1 - loads[j]))
            capacities[j] = max(capacities[j], required)
        witness = tuple((i + 1, owner + 1) for i, owner in enumerate(owners))
    employees = tuple(
        Employee(j + 1, capacities[j], int(capacities[j] * loads[j]), rates[j])
        for j in range(m)
    )
    requests = tuple(
        Request(i + 1, tuple((j + 1, hours_to_minutes(Fraction(base_hours[i]) /
                                                    speeds[j][request_categories[i]]))
                             for j in range(m) if skills[j][request_categories[i]]))
        for i in range(n)
    )
    problem = Problem(requests=requests, employees=employees)
    if witness is not None:
        evaluate_assignment(problem, witness)  # Verify construction; do not merely assume it.
    metadata = MappingProxyType({
        "generator": GENERATOR_VERSION, "seed_text": seed_text, "seed": seed,
        "family": family, "categories_count": categories, "rho": _ratio(rho),
        "request_categories": tuple(request_categories), "base_hours": tuple(base_hours),
        "speed_by_employee_category": tuple(tuple(_ratio(q) for q in row) for row in speeds),
        "occupied_fraction": tuple(_ratio(load) for load in loads),
        "skills": tuple(tuple(row) for row in skills),
        "witness_verified": witness is not None,
    })
    return GeneratedCase(problem, witness, problem_fingerprint(problem), metadata)


def control_cases() -> tuple[tuple[str, Problem], ...]:
    """Explicit controls and separately named preference/expert perturbations."""
    half = Fraction(1, 2)
    m1 = Problem((Request(1, ((1, 60), (2, 60))), Request(2, ((1, 120), (2, 135)))),
                 (Employee(1, 120, 0, Fraction(100)), Employee(2, 135, 0, Fraction(400))))
    trap = Problem((Request(1, ((1, 60), (2, 60))), Request(2, ((1, 60),))),
                   (Employee(1, 60, 0, Fraction(100)), Employee(2, 60, 0, Fraction(100))))
    packing = Problem(tuple(Request(i, ((1, 120), (2, 120))) for i in range(1, 4)),
                      (Employee(1, 180, 0, Fraction(100)), Employee(2, 180, 0, Fraction(100))))
    rare = Problem((Request(1, ((1, 60),)), Request(2, ((1, 60),)),
                    Request(3, ((1, 60), (2, 60))), Request(4, ((1, 60), (2, 60)))),
                   (Employee(1, 120, 0, Fraction(100)), Employee(2, 240, 0, Fraction(200))))
    imbalance = Problem(tuple(Request(i, ((1, 120), (2, 120))) for i in range(1, 4)),
                        (Employee(1, 480, 420, Fraction(100)), Employee(2, 480, 0, Fraction(200))))
    symmetric = Problem(tuple(Request(i, ((1, 60), (2, 60))) for i in range(1, 5)),
                        (Employee(1, 120, 0, Fraction(100)), Employee(2, 120, 0, Fraction(100))))
    zero = replace(symmetric, employees=tuple(replace(e, hourly_rate=Fraction(0))
                                              for e in symmetric.employees))
    saturated = Problem(tuple(Request(i, ((1, 60), (2, 60))) for i in range(1, 4)),
                        (Employee(1, 120, 120, Fraction(100)), Employee(2, 240, 0, Fraction(200))))
    tradeoff = Problem((Request(1, ((1, 60), (2, 60))),),
                      (Employee(1, 60, 0, Fraction(100)), Employee(2, 240, 0, Fraction(400))))
    cases = [("m1", m1), ("greedy_trap", trap), ("infeasible_packing", packing),
             ("rare_skills", rare), ("imbalanced_load", imbalance), ("symmetric", symmetric),
             ("zero_cost", zero), ("saturated_utilization", saturated)]
    for alpha in (Fraction(0), Fraction(1, 4), half, Fraction(3, 4), Fraction(1)):
        cases.append((f"weight_{alpha.numerator}_{alpha.denominator}",
                      replace(tradeoff, alpha=alpha, beta=1 - alpha)))
    cases.extend([
        ("expert_time_plus_15min", replace(m1, requests=(m1.requests[0],
            Request(2, ((1, 120), (2, 150)))))),
        ("expert_capacity_plus_60min", replace(m1, employees=(
            replace(m1.employees[0], capacity_minutes=180), m1.employees[1]))),
        ("expert_rate_plus_100", replace(m1, employees=(m1.employees[0],
            replace(m1.employees[1], hourly_rate=Fraction(500))))),
        ("expert_preload_plus_30min", replace(m1, employees=(
            replace(m1.employees[0], occupied_minutes=30), m1.employees[1]))),
        ("normalization_outlier", Problem(
            (Request(1, ((1, 60), (2, 60), (3, 60))),),
            (*tradeoff.employees, Employee(3, 30, 0, Fraction(10000))),
            alpha=Fraction(3, 4), beta=Fraction(1, 4))),
    ])
    cases.append(("infeasible_total_capacity", Problem(
        tuple(Request(i, ((1, 60), (2, 60))) for i in range(1, 4)),
        (Employee(1, 60, 0, Fraction(100)), Employee(2, 60, 0, Fraction(100))))))
    return tuple(cases)


def _number(value: Fraction | None) -> dict | None:
    if value is None:
        return None
    value = Fraction(value)
    with localcontext() as context:
        context.prec = 32
        display = format(Decimal(value.numerator) / Decimal(value.denominator), ".10f")
    return {"numerator": value.numerator, "denominator": value.denominator, "display": display}


def _result(result) -> dict:
    metrics = None if result.metrics is None else {
        name: _number(getattr(result.metrics, name))
        for name in ("cost", "utilization", "labor_hours", "objective", "cost_scale")
    }
    return {"status": result.status.value, "assignment": result.assignment, "metrics": metrics,
            "nodes_visited": result.nodes_visited, "termination_reason": result.termination_reason,
            "optimality_proven": result.optimality_proven,
            "infeasibility_proven": result.infeasibility_proven,
            "lower_bound": _number(result.lower_bound)}


def _timing(values: list[float]) -> dict:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def _measure(method, problem: Problem, repeats: int) -> dict:
    original = canonical_problem(problem)
    runs, walls, cpus = [], [], []
    for _ in range(repeats):
        wall, cpu = time.perf_counter_ns(), time.process_time_ns()
        result = method(problem)
        cpus.append((time.process_time_ns() - cpu) / 1_000_000_000)
        walls.append((time.perf_counter_ns() - wall) / 1_000_000_000)
        if result.metrics is not None:
            assert result.assignment is not None
            assert evaluate_assignment(problem, result.assignment) == result.metrics
        assert canonical_problem(problem) == original, "Solver mutated its input"
        runs.append(_result(result))
    match = all(run == runs[0] for run in runs)
    time_limited = any("time" in run["termination_reason"].lower() for run in runs)
    if not match and not time_limited:
        raise AssertionError("Nondeterministic solver result without a time cutoff")
    return {"result": runs[0], "wall_seconds": _timing(walls), "cpu_seconds": _timing(cpus),
            "repeats_match": match, "time_cutoff_observed": time_limited, "runs": runs}


def _gain(greedy_result: dict, bnb_result: dict) -> dict | None:
    g, b = greedy_result["metrics"], bnb_result["metrics"]
    if g is None or b is None:
        return None
    ga = Fraction(g["objective"]["numerator"], g["objective"]["denominator"])
    ba = Fraction(b["objective"]["numerator"], b["objective"]["denominator"])
    if ga == 0:
        return _number(Fraction(0)) if ba == 0 else None
    return _number((ga - ba) / ga)


def _specifications(preset: str, seed_count: int):
    specs = []
    if preset in ("smoke", "controls", "all"):
        for name, problem in control_cases():
            specs.append((name, "controls", lambda p=problem, n=name: GeneratedCase(
                p, None, problem_fingerprint(p), MappingProxyType({"family": "control", "name": n}))))
    dimensions = {
        "smoke": (((3, 2), (5, 3)), 2),
        "small": (tuple((n, m) for n in (3, 5) for m in (2, 3)), 2),
        "medium": (tuple((n, m) for n in (10, 20) for m in (3, 5)), 4),
        "large": (tuple((n, m) for n in (30, 50) for m in (5, 8)), 4),
    }
    groups = ("small", "medium", "large") if preset == "all" else (() if preset == "controls" else (preset,))
    for seed in SEEDS[:seed_count]:
        for group in groups:
            pairs, categories = dimensions[group]
            for n, m in pairs:
                for family in ("feasible", "stress"):
                    name = f"{group}-{n}x{m}-{family}-{seed}"
                    specs.append((name, group, lambda n=n, m=m, k=categories, seed=seed, family=family:
                                  generate_case(n, m, seed=seed, categories=k, family=family)))
    return specs


def _summarize(rows: list[dict]) -> dict:
    summary = {"cases": len(rows), "greedy_statuses": dict(Counter()), "bnb_statuses": dict(Counter())}
    greedy_statuses, bnb_statuses = Counter(), Counter()
    both, better, rescued, optimal, feasible_g, feasible_b = 0, 0, 0, 0, 0, 0
    for row in rows:
        g, b = row["greedy"]["result"], row["branch_and_bound"]["result"]
        greedy_statuses[g["status"]] += 1
        bnb_statuses[b["status"]] += 1
        feasible_g += g["metrics"] is not None
        feasible_b += b["metrics"] is not None
        optimal += b["optimality_proven"]
        rescued += g["metrics"] is None and b["metrics"] is not None
        if g["metrics"] is not None and b["metrics"] is not None:
            both += 1
            gm, bm = g["metrics"]["objective"], b["metrics"]["objective"]
            better += Fraction(bm["numerator"], bm["denominator"]) < Fraction(gm["numerator"], gm["denominator"])
    summary.update(greedy_statuses=dict(greedy_statuses), bnb_statuses=dict(bnb_statuses),
                   greedy_feasible=feasible_g, bnb_feasible=feasible_b, bnb_optimal=optimal,
                   comparable_full_plans=both, bnb_better=better, greedy_failed_bnb_found=rescued,
                   bnb_better_fraction_of_comparable=_number(Fraction(better, both)) if both else None,
                   rescued_fraction_of_all=_number(Fraction(rescued, len(rows))) if rows else None)
    return summary


def run_experiment(*, preset: str = "smoke", seed_count: int = 2, max_cases: int = 32,
                   node_limit: int = 200000, time_limit_seconds: float = 30,
                   repeats: int = 1, oracle_max_assignments: int = 1000000,
                   total_time_limit_seconds: float = 120) -> dict:
    if preset not in {"smoke", "controls", "small", "medium", "large", "all"}:
        raise ValueError("Unknown preset")
    for name, value, minimum in (("seed_count", seed_count, 1), ("max_cases", max_cases, 1),
                                  ("repeats", repeats, 1), ("node_limit", node_limit, 0),
                                  ("oracle_max_assignments", oracle_max_assignments, 0)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if seed_count > len(SEEDS):
        raise ValueError("Only the 20 fixed seeds are supported")
    for name, value in (("time_limit_seconds", time_limit_seconds),
                        ("total_time_limit_seconds", total_time_limit_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive timer value")
    specs = _specifications(preset, seed_count)
    started = time.perf_counter()
    # One deterministic warmup; excluded from every case's solver timings.
    warm = control_cases()[0][1]
    greedy(warm)
    branch_and_bound(warm, node_limit=node_limit, time_limit_seconds=time_limit_seconds)
    if oracle_max_assignments:
        exhaustive_oracle(warm, max_assignments=oracle_max_assignments)
    rows = []
    stop_reason = "completed"
    for name, group, factory in specs:
        if len(rows) >= max_cases:
            stop_reason = "case_cap"
            break
        if time.perf_counter() - started >= total_time_limit_seconds:
            stop_reason = "total_time_budget"
            break
        case = factory()  # Generation deliberately outside solver timers.
        g = _measure(greedy, case.problem, repeats)
        b = _measure(lambda p: branch_and_bound(p, node_limit=node_limit,
                                               time_limit_seconds=time_limit_seconds), case.problem, repeats)
        oracle = None
        if len(case.problem.requests) <= 5 and oracle_max_assignments:
            oracle = _measure(lambda p: exhaustive_oracle(p, max_assignments=oracle_max_assignments),
                              case.problem, repeats)
            o = oracle["result"]
            if b["result"]["optimality_proven"] and o["optimality_proven"]:
                assert b["result"]["assignment"] == o["assignment"]
                assert b["result"]["metrics"] == o["metrics"]
            if o["infeasibility_proven"]:
                assert b["result"]["metrics"] is None
            if b["result"]["infeasibility_proven"]:
                assert o["metrics"] is None
        rows.append({"name": name, "group": group, "n": len(case.problem.requests),
                     "m": len(case.problem.employees), "fingerprint": case.fingerprint,
                     "metadata": dict(case.metadata), "problem": canonical_problem(case.problem),
                     "witness": case.witness, "greedy": g, "branch_and_bound": b, "oracle": oracle,
                     "relative_gain_objective": _gain(g["result"], b["result"])})
    signature_rows = [
        {"name": row["name"], "fingerprint": row["fingerprint"],
         "greedy": row["greedy"]["result"], "branch_and_bound": row["branch_and_bound"]["result"],
         "oracle": row["oracle"]["result"] if row["oracle"] else None}
        for row in rows
    ]
    signature = hashlib.sha256(json.dumps(signature_rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": "M2-experiment-v1", "environment": {"python": sys.version,
            "platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count()},
        "parameters": {"preset": preset, "seed_count": seed_count, "seeds": list(SEEDS[:seed_count]),
            "max_cases": max_cases, "node_limit": node_limit, "time_limit_seconds": time_limit_seconds,
            "repeats": repeats, "oracle_max_assignments": oracle_max_assignments,
            "total_time_limit_seconds": total_time_limit_seconds},
        "coverage": {"requested_cases": len(specs), "completed_cases": len(rows),
                     "unrun_cases": len(specs) - len(rows), "stop_reason": stop_reason,
                     "completed_by_group": dict(Counter(row["group"] for row in rows)),
                     "completed_random_seeds": sorted({row["metadata"]["seed"] for row in rows
                                                        if "seed" in row["metadata"]}),
                     "completed_by_size_family": dict(Counter(
                         f"{row['group']}:{row['n']}x{row['m']}:{row['metadata']['family']}"
                         for row in rows))},
        "summary": _summarize(rows), "comparison_signature": signature,
        "elapsed_total_seconds": time.perf_counter() - started, "cases": rows,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _positive_seconds(value: str) -> float:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a decimal number of seconds") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    seconds = float(parsed)  # Timer control only; never used in model arithmetic.
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("seconds outside timer range")
    return seconds


def _safe_output(path: str) -> Path:
    target = Path(path).expanduser().resolve()
    if target == ROOT or ROOT in target.parents:
        raise ValueError("Experiment output must be outside the repository")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=("smoke", "controls", "small", "medium", "large", "all"), default="smoke")
    parser.add_argument("--seed-count", type=_positive_int, default=None, help="first 1..20 fixed seeds; default smoke=2, otherwise=20")
    parser.add_argument("--max-cases", type=_positive_int, default=32)
    parser.add_argument("--node-limit", type=_nonnegative_int, default=200000)
    parser.add_argument("--time-limit", type=_positive_seconds, default=30.0, help="B&B seconds per run; includes its greedy seed")
    parser.add_argument("--total-time-limit", type=_positive_seconds, default=120.0, help="budget checked between cases, includes warmup/generation")
    parser.add_argument("--repeats", type=_positive_int, default=1)
    parser.add_argument("--oracle-max-assignments", type=_nonnegative_int, default=1000000, help="0 disables the small oracle")
    parser.add_argument("--output", help="optional JSON path strictly outside the checkout")
    args = parser.parse_args(argv)
    if args.seed_count is None:
        args.seed_count = 2 if args.preset == "smoke" else 20
    if args.seed_count > 20:
        parser.error("--seed-count cannot exceed the 20 fixed seeds")
    try:
        output = _safe_output(args.output) if args.output else None
    except ValueError as error:
        parser.error(str(error))
    report = run_experiment(preset=args.preset, seed_count=args.seed_count, max_cases=args.max_cases,
                            node_limit=args.node_limit, time_limit_seconds=args.time_limit,
                            repeats=args.repeats, oracle_max_assignments=args.oracle_max_assignments,
                            total_time_limit_seconds=args.total_time_limit)
    print("Synthetic M2 experiment; no CRM data or database used.")
    print(f"Python {platform.python_version()}; node limit={args.node_limit}, time limit={args.time_limit}s, repeats={args.repeats}")
    for row in report["cases"]:
        g, b = row["greedy"]["result"], row["branch_and_bound"]["result"]
        gf = "-" if g["metrics"] is None else g["metrics"]["objective"]["display"]
        bf = "-" if b["metrics"] is None else b["metrics"]["objective"]["display"]
        gc = "-" if g["metrics"] is None else g["metrics"]["cost"]["display"]
        detail = "" if b["metrics"] is None else " C=" + b["metrics"]["cost"]["display"] + \
            " U=" + b["metrics"]["utilization"]["display"] + " T_hours=" + b["metrics"]["labor_hours"]["display"]
        print(f"{row['name']}: G={g['status']} F={gf} C={gc}; B&B={b['status']} F={bf}{detail}, "
              f"nodes={b['nodes_visited']}, wall median={row['branch_and_bound']['wall_seconds']['median']:.6f}s")
    print("Coverage:", json.dumps(report["coverage"], ensure_ascii=False, sort_keys=True))
    print("Summary:", json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print("Comparison signature (excludes timings):", report["comparison_signature"])
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("JSON:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
