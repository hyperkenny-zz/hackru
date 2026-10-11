"""Pure presentation data for clinic-time timelines and disruption review."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from src.models import Appointment, CLINIC_TZ, Scenario
from src.windows import visit_window

IMPACT_COLUMNS = ("participant_id", "visit_id", "start", "resources", "impact", "reason")


@dataclass(frozen=True)
class VisitImpact:
    key: tuple[str, str]
    category: str
    reasons: tuple[str, ...]


def disruption_impacts(scenario: Scenario, appointments: tuple[Appointment, ...]) -> tuple[VisitImpact, ...]:
    """Identify directly invalid placements and successors requiring review.

    Successors are a conservative review list, not a claim that they must move.
    Full schedule validity remains the responsibility of the independent validator.
    """
    participants = {p.id: p for p in scenario.participants}
    resources = {r.id: r for r in scenario.resources}
    rules = {r.id: r for r in scenario.rules}
    by_key = {a.key: a for a in appointments}
    direct = {}
    for a in appointments:
        reasons = []
        p, rule = participants.get(a.participant_id), rules.get(a.visit_id)
        if p is None or rule is None:
            reasons.append("Participant or visit rule is no longer present")
        else:
            window = visit_window(p, rule)
            if not (window.earliest <= a.interval.start.date() and
                    (a.interval.end - timedelta(microseconds=1)).date() <= window.latest):
                reasons.append("Appointment falls outside the visit window")
            if not any(i.contains(a.interval) for i in p.availability):
                reasons.append("Participant is unavailable at the appointment time")
            for predecessor in rule.predecessors:
                prior = by_key.get((p.id, predecessor))
                if prior is None or prior.interval.end > a.interval.start:
                    reasons.append(f"Predecessor {predecessor} is missing or finishes too late")
        if not any(i.contains(a.interval) for i in scenario.clinic_hours):
            reasons.append("Appointment falls outside clinic hours")
        for id in sorted(a.resource_ids):
            resource = resources.get(id)
            if resource is None:
                reasons.append(f"Resource {id} is no longer present")
            elif any(i.overlaps(a.interval) for i in resource.outages):
                reasons.append(f"Resource {id} has an overlapping outage")
            elif not any(i.contains(a.interval) for i in resource.working_hours):
                reasons.append(f"Resource {id} is outside working hours")
        if reasons:
            direct[a.key] = VisitImpact(a.key, "Directly affected", tuple(reasons))
    impacts = dict(direct)
    while True:
        added = {}
        for a in appointments:
            if a.key in impacts or a.visit_id not in rules:
                continue
            predecessors = [id for id in rules[a.visit_id].predecessors if (a.participant_id, id) in impacts]
            if predecessors:
                added[a.key] = VisitImpact(a.key, "Successor to review",
                                          (f"Affected predecessor: {', '.join(sorted(predecessors))}. This visit may remain unchanged.",))
        if not added:
            break
        impacts.update(added)
    return tuple(impacts[key] for key in sorted(impacts))


def impact_rows(appointments: tuple[Appointment, ...], impacts: tuple[VisitImpact, ...]) -> list[dict]:
    by_key = {a.key: a for a in appointments}
    return [{"participant_id": item.key[0], "visit_id": item.key[1],
             "start": by_key[item.key].interval.start.strftime("%Y-%m-%d %H:%M"),
             "resources": " ".join(by_key[item.key].resource_ids),
             "impact": item.category, "reason": "; ".join(item.reasons)} for item in impacts]


def timeline_rows(scenario: Scenario, appointments: tuple[Appointment, ...], day: date,
                  group_by: str = "Resource", label: str = "Schedule",
                  statuses: dict[tuple[str, str], str] | None = None) -> list[dict]:
    """Clip bars to a local calendar day; numeric minutes avoid browser timezone shifts."""
    if group_by not in {"Resource", "Participant"}:
        raise ValueError("Unknown timeline grouping")
    start = datetime.combine(day, time.min, CLINIC_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, CLINIC_TZ)
    statuses = statuses or {}
    participants = {p.id: p for p in scenario.participants}
    rules = {r.id: r for r in scenario.rules}
    rows = []

    def bounds(interval):
        left, right = max(start, interval.start), min(end, interval.end)
        if left >= right:
            return None
        def minutes(moment):
            return (moment.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds() / 60
        return minutes(left), minutes(right)

    for a in sorted(appointments, key=lambda a: (a.interval.start, a.key)):
        clipped = bounds(a.interval)
        if clipped is None:
            continue
        window = visit_window(participants[a.participant_id], rules[a.visit_id])
        lanes = (a.resource_ids or ("No resources",)) if group_by == "Resource" else (a.participant_id,)
        for lane in lanes:
            rows.append({"lane": f"{label} · {lane}", "start_minute": clipped[0], "end_minute": clipped[1],
                         "participant": a.participant_id, "visit": a.visit_id, "resources": ", ".join(a.resource_ids),
                         "start": a.interval.start.strftime("%Y-%m-%d %H:%M"),
                         "end": a.interval.end.strftime("%Y-%m-%d %H:%M"),
                         "window": f"{window.earliest} to {window.latest}", "target": str(window.target),
                         "status": statuses.get(a.key, "Scheduled"), "kind": "Appointment"})
    if group_by == "Resource":
        for resource in sorted(scenario.resources, key=lambda r: r.id):
            for outage in resource.outages:
                clipped = bounds(outage)
                if clipped is not None:
                    rows.append({"lane": f"{label} · {resource.id}", "start_minute": clipped[0], "end_minute": clipped[1],
                                 "participant": "", "visit": "Resource outage", "resources": resource.id,
                                 "start": outage.start.strftime("%Y-%m-%d %H:%M"),
                                 "end": outage.end.strftime("%Y-%m-%d %H:%M"), "window": "", "target": "",
                                 "status": "Outage", "kind": "Outage"})
    return rows
