"""Lexicographic CP-SAT scheduling and repair; never writes committed state."""
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from ortools.sat.python import cp_model
from src.models import Appointment, Scenario, ScheduleResult, UnscheduledVisit
from src.scheduler import candidate_appointments, greedy_schedule
from src.validator import validate_schedule
from src.windows import visit_window


@dataclass(frozen=True)
class SolverStage:
    name: str
    status: str
    runtime_seconds: float
    objective: float | None
    best_bound: float | None


@dataclass(frozen=True)
class OptimizationResult:
    result: ScheduleResult | None
    status: str
    stages: tuple[SolverStage, ...]
    runtime_seconds: float
    candidate_count: int
    optimal: bool


def placement(a: Appointment) -> tuple:
    """Version-independent placement, with resource order normalized."""
    return a.interval, tuple(sorted(a.resource_ids))


def optimize_schedule(scenario: Scenario, existing: tuple[Appointment, ...] = (),
                      pinned: tuple[tuple[str, str], ...] = (),
                      time_limit_seconds: float = 5.0) -> OptimizationResult:
    """Maximize count, minimize existing changes, then target-day deviation.

    The total CP-SAT search budget excludes candidate/model construction, which
    is included in runtime_seconds. UNKNOWN without a valid incumbent returns
    result=None, never an empty proposal that silently drops pinned visits.
    Pins refer to existing appointment keys. Old placements may violate newly
    introduced outages; only pinned placements must still be individually feasible.
    """
    if not isfinite(time_limit_seconds) or time_limit_seconds <= 0:
        raise ValueError("Search budget must be positive and finite")
    started = perf_counter()
    before = {a.key: a for a in existing}
    keys = {(p.id, r.id) for p in scenario.participants for r in scenario.rules}
    if len(before) != len(existing) or not before.keys() <= keys:
        raise ValueError("Existing appointments have duplicate or unknown visit keys")
    if not set(pinned) <= before.keys():
        raise ValueError("Pinned keys must refer to existing appointments")
    model = cp_model.CpModel()
    candidates, variables, present = {}, {}, {}
    occupancy = defaultdict(list)
    starts, ends = {}, {}
    origin = min((i.start.timestamp() for i in scenario.clinic_hours), default=0)
    for participant in sorted(scenario.participants, key=lambda p: p.id):
        for rule in sorted(scenario.rules, key=lambda r: r.id):
            key = participant.id, rule.id
            options = candidate_appointments(scenario, participant, rule)
            candidates[key] = options
            xs = [model.new_bool_var(f"candidate_{key}_{i}") for i in range(len(options))]
            variables[key] = xs
            selected = model.new_bool_var(f"present_{key}")
            present[key] = selected
            model.add(sum(xs) == selected)
            offsets = [int((a.interval.start.timestamp() - origin) // 60) for a in options]
            starts[key] = sum(t * x for t, x in zip(offsets, xs))
            ends[key] = starts[key] + rule.duration_minutes * selected
            for i, (a, x, offset) in enumerate(zip(options, xs, offsets)):
                item = model.new_optional_fixed_size_interval_var(offset, rule.duration_minutes, x, f"interval_{key}_{i}")
                occupancy[("participant", participant.id)].append(item)
                for resource in a.resource_ids:
                    occupancy[("resource", resource)].append(item)
            if key in pinned:
                matches = [x for a, x in zip(options, xs) if placement(a) == placement(before[key])]
                model.add(sum(matches) == 1)
    for intervals in occupancy.values():
        model.add_no_overlap(intervals)
    for p in scenario.participants:
        for r in scenario.rules:
            for predecessor in r.predecessors:
                key, prior = (p.id, r.id), (p.id, predecessor)
                model.add(present[key] <= present[prior])
                model.add(ends[prior] <= starts[key]).only_enforce_if(present[key])

    count = sum(present.values())
    preserved = sum(x for key, old in before.items()
                    for a, x in zip(candidates[key], variables[key]) if placement(a) == placement(old))
    changes = len(before) - preserved
    participant_map = {p.id: p for p in scenario.participants}
    rule_map = {r.id: r for r in scenario.rules}
    deviation = sum(abs((a.interval.start.date() - visit_window(participant_map[key[0]], rule_map[key[1]]).target).days) * x
                    for key in sorted(keys) for a, x in zip(candidates[key], variables[key]))

    def score(items):
        mapped = {a.key: a for a in items}
        return (-len(items), sum(k not in mapped or placement(mapped[k]) != placement(a) for k, a in before.items()),
                sum(abs((a.interval.start.date() - visit_window(participant_map[a.participant_id], rule_map[a.visit_id]).target).days) for a in items))

    # A validated fallback is retained if a search stage runs out of time.
    incumbent = None
    if not validate_schedule(scenario, existing) and all(key in before for key in pinned):
        incumbent = existing
    if not pinned:
        greedy = greedy_schedule(scenario).appointments
        if incumbent is None or score(greedy) < score(incumbent):
            incumbent = greedy
    if incumbent is not None:
        model.add(count >= len(incumbent))
        initial = {a.key: placement(a) for a in incumbent}
        for key in sorted(keys):
            for a, x in zip(candidates[key], variables[key]):
                model.add_hint(x, int(initial.get(key) == placement(a)))

    stages = []
    search_started = perf_counter()
    final_status = "UNKNOWN"
    for name, expression, maximize in (("scheduled_count", count, True), ("existing_changes", changes, False),
                                        ("target_deviation", deviation, False)):
        remaining = time_limit_seconds - (perf_counter() - search_started)
        if remaining <= 0:
            stages.append(SolverStage(name, "NOT_RUN", 0.0, None, None))
            break
        if isinstance(expression, int) and incumbent is not None:
            stages.append(SolverStage(name, "OPTIMAL", 0.0, float(expression), float(expression)))
            continue
        if maximize:
            model.maximize(expression)
        else:
            model.minimize(expression)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = scenario.seed
        stage_started = perf_counter()
        status = solver.solve(model)
        solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        stages.append(SolverStage(name, solver.status_name(status), perf_counter() - stage_started,
                                  solver.objective_value if solved else None,
                                  solver.best_objective_bound if status != cp_model.MODEL_INVALID else None))
        if status == cp_model.MODEL_INVALID:
            raise RuntimeError(model.validate())
        if status == cp_model.INFEASIBLE:
            incumbent = None
            final_status = "INFEASIBLE"
            break
        if solved:
            solution = tuple(a for key in sorted(keys) for a, x in zip(candidates[key], variables[key]) if solver.value(x))
            if incumbent is None or score(solution) < score(incumbent):
                incumbent = solution
        if status != cp_model.OPTIMAL:
            break
        model.add(expression == round(solver.objective_value))
        model.clear_hints()
        for key in sorted(keys):
            for x in variables[key]:
                model.add_hint(x, solver.value(x))
    optimal = len(stages) == 3 and all(s.status == "OPTIMAL" for s in stages)
    result = None
    if incumbent is not None:
        errors = validate_schedule(scenario, incumbent)
        if errors:
            raise RuntimeError(f"Optimizer produced an invalid proposal: {errors}")
        mapped = {a.key: a for a in incumbent}
        if any(key not in mapped or placement(mapped[key]) != placement(before[key]) for key in pinned):
            raise RuntimeError("Optimizer failed to preserve a pinned appointment")
        final_status = "OPTIMAL" if optimal else "FEASIBLE"
        unscheduled = tuple(UnscheduledVisit(*key, "no_candidate_slots" if not candidates[key] else
                                             "resource_competition_or_precedence", len(candidates[key]))
                            for key in sorted(keys - mapped.keys()))
        result = ScheduleResult(tuple(sorted(incumbent, key=lambda a: a.key)), unscheduled, final_status)
    return OptimizationResult(result, final_status, tuple(stages), perf_counter() - started,
                              sum(map(len, candidates.values())), optimal)
