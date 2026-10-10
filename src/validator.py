"""Independent checks of appointments, without scheduler candidate construction."""
from collections import Counter
from datetime import timedelta
from src.models import Appointment, Scenario


def validate_schedule(scenario: Scenario, appointments: tuple[Appointment, ...]) -> list[str]:
    errors = []
    participants = {p.id: p for p in scenario.participants}
    rules = {r.id: r for r in scenario.rules}
    resources = {r.id: r for r in scenario.resources}
    by_key = {a.key: a for a in appointments}
    if len(by_key) != len(appointments):
        errors.append("Duplicate participant/visit appointment")
    for a in appointments:
        label = f"{a.participant_id}/{a.visit_id}"
        p, r = participants.get(a.participant_id), rules.get(a.visit_id)
        if p is None or r is None:
            errors.append(f"{label}: unknown participant or visit")
            continue
        start, end = a.interval.start, a.interval.end
        target = p.reference_date + timedelta(days=r.target_day)
        if not (target - timedelta(days=r.early_days) <= start.date() and
                (end - timedelta(microseconds=1)).date() <= target + timedelta(days=r.late_days)):
            errors.append(f"{label}: outside visit window")
        if end - start != timedelta(minutes=r.duration_minutes):
            errors.append(f"{label}: incorrect duration")
        if start.minute % 30 or start.second or start.microsecond:
            errors.append(f"{label}: off slot grid")
        for name, intervals in (("participant availability", p.availability), ("clinic hours", scenario.clinic_hours)):
            if not any(i.contains(a.interval) for i in intervals):
                errors.append(f"{label}: outside {name}")
        assigned = Counter()
        if len(a.resource_ids) != len(set(a.resource_ids)):
            errors.append(f"{label}: duplicate resource")
        for id in a.resource_ids:
            resource = resources.get(id)
            if resource is None:
                errors.append(f"{label}: unknown resource {id}")
                continue
            assigned[resource.kind] += 1
            if not any(i.contains(a.interval) for i in resource.working_hours) or any(i.overlaps(a.interval) for i in resource.outages):
                errors.append(f"{label}: unavailable resource {id}")
        if assigned != Counter(dict(r.requirements)):
            errors.append(f"{label}: incorrect resource requirements")
        for predecessor in r.predecessors:
            prior = by_key.get((p.id, predecessor))
            if prior is None or prior.interval.end > start:
                errors.append(f"{label}: precedence violation")
    for index, a in enumerate(appointments):
        for b in appointments[index + 1:]:
            if a.interval.overlaps(b.interval) and (a.participant_id == b.participant_id or set(a.resource_ids) & set(b.resource_ids)):
                errors.append(f"Overlap: {a.key} and {b.key}")
    return errors
