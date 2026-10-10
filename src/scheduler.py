"""Deterministic greedy baseline; no optimality claims."""
from datetime import timedelta
from itertools import combinations, product
from src.models import Appointment, Interval, Scenario, ScheduleResult, UnscheduledVisit
from src.windows import visit_window
from src.validator import validate_schedule


def candidate_appointments(scenario, participant, rule):
    window = visit_window(participant, rule)
    choices = [list(combinations(sorted((r.id for r in scenario.resources if r.kind == kind)), count))
               for kind, count in sorted(rule.requirements)]
    resources = {r.id: r for r in scenario.resources}
    candidates = set()
    for hours in scenario.clinic_hours:
        start = hours.start.replace(second=0, microsecond=0)
        start += timedelta(minutes=(-start.minute) % 30)
        if start < hours.start:
            start += timedelta(minutes=30)
        while start + timedelta(minutes=rule.duration_minutes) <= hours.end:
            interval = Interval(start, start + timedelta(minutes=rule.duration_minutes))
            if window.earliest <= start.date() and (interval.end - timedelta(microseconds=1)).date() <= window.latest and any(i.contains(interval) for i in participant.availability):
                for groups in product(*choices):
                    ids = tuple(sorted(id for group in groups for id in group))
                    if all(any(i.contains(interval) for i in resources[id].working_hours) and not any(i.overlaps(interval) for i in resources[id].outages) for id in ids):
                        candidates.add(Appointment(participant.id, rule.id, interval, ids))
            start += timedelta(minutes=30)
    return tuple(sorted(candidates, key=lambda a: (abs((a.interval.start.date() - window.target).days), a.interval.start, a.resource_ids)))


def greedy_schedule(scenario: Scenario) -> ScheduleResult:
    appointments, unscheduled = [], []
    remaining = {r.id: r for r in scenario.rules}
    ordered = []
    while remaining:
        ready = sorted((r for r in remaining.values() if all(p not in remaining for p in r.predecessors)), key=lambda r: r.id)
        for rule in ready:
            ordered.append(rule)
            del remaining[rule.id]
    for rule in ordered:
        for participant in sorted(scenario.participants, key=lambda p: (visit_window(p, rule).latest, p.id)):
            candidates = candidate_appointments(scenario, participant, rule)
            chosen = next((a for a in candidates if not validate_schedule(scenario, tuple(appointments) + (a,))), None)
            if chosen:
                appointments.append(chosen)
            else:
                reason = "no_candidate_slots" if not candidates else "resource_competition_or_precedence"
                unscheduled.append(UnscheduledVisit(participant.id, rule.id, reason, len(candidates)))
    result = ScheduleResult(tuple(appointments), tuple(unscheduled))
    errors = validate_schedule(scenario, result.appointments)
    if errors:
        raise RuntimeError(f"Generated invalid schedule: {errors}")
    return result
