"""Synthetic scenario specifications stored in data/, converted to Scenario objects.

A specification is a small JSON document describing a clinic horizon, participant
and resource counts, visit rules, and optional resource outages. Expanding a
specification is deterministic for a fixed seed, so a scenario identifier always
produces identical inputs for the scheduler.
"""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from random import Random
from src.models import CLINIC_TZ, Interval, Participant, Resource, Scenario, VisitRule
from src.windows import visit_window

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESOURCE_KINDS = ("nurse", "equipment")


def _interval(day: date, start: time, end: time) -> Interval:
    return Interval(datetime.combine(day, start, CLINIC_TZ), datetime.combine(day, end, CLINIC_TZ))


def _clinic_days(spec: dict) -> tuple[date, ...]:
    """Calendar days inside the horizon, excluding weekends unless requested."""
    start = date.fromisoformat(spec["start_date"])
    days = tuple(day for offset in range(int(spec["days"]))
                 if not (spec.get("skip_weekends", True) and (day := start + timedelta(days=offset)).weekday() >= 5))
    if not days:
        raise ValueError("Scenario horizon contains no clinic days")
    return days


def _availability(pattern: str, rng: Random, day: date, open_time: time, close_time: time) -> tuple[Interval, ...]:
    """Daily availability blocks; every block fits inside one clinic day."""
    if pattern == "wide":
        return (_interval(day, open_time, close_time),)
    if pattern == "mixed":
        middle = time(hour=(open_time.hour + close_time.hour) // 2)
        choice = rng.choice(("full", "morning", "afternoon"))
        if choice == "morning":
            return (_interval(day, open_time, middle),)
        if choice == "afternoon":
            return (_interval(day, middle, close_time),)
        return (_interval(day, open_time, close_time),)
    raise ValueError(f"Unknown availability pattern {pattern!r}")


def _participants(spec: dict, days: tuple[date, ...], open_time: time, close_time: time) -> tuple[Participant, ...]:
    block = spec["participants"]
    count, pattern = int(block["count"]), block.get("availability", "wide")
    prefix, width = block.get("id_prefix", "P"), len(str(int(block["count"])))
    spread = max(1, int(block.get("reference_spread_days", 1)))
    if spread > len(days):
        raise ValueError("Reference dates spread beyond the clinic horizon")
    participants = []
    for index in range(count):
        rng = Random(f"{spec['seed']}-{index}")
        availability = tuple(i for day in days for i in _availability(pattern, rng, day, open_time, close_time))
        participants.append(Participant(f"{prefix}{index + 1:0{width}}", days[index % spread], availability))
    return tuple(participants)


def _outages(spec: dict) -> dict[str, list[Interval]]:
    outages: dict[str, list[Interval]] = {}
    for entry in spec.get("outages", ()):
        interval = _interval(date.fromisoformat(entry["date"]),
                             time.fromisoformat(entry["from"]), time.fromisoformat(entry["to"]))
        outages.setdefault(entry["resource_id"], []).append(interval)
    return outages


def _resources(spec: dict, days: tuple[date, ...], open_time: time, close_time: time) -> tuple[Resource, ...]:
    outages, resources = _outages(spec), []
    for kind in RESOURCE_KINDS:
        block = spec["resources"].get(kind, {})
        prefix = block.get("id_prefix", kind[0].upper())
        shift_start, shift_end = (time.fromisoformat(value) for value in block.get("shift", (open_time.isoformat(timespec="minutes"), close_time.isoformat(timespec="minutes"))))
        if shift_start < open_time or close_time < shift_end or shift_end <= shift_start:
            raise ValueError(f"Shift for {kind} resources falls outside clinic opening hours")
        hours = tuple(_interval(day, shift_start, shift_end) for day in days)
        for index in range(1, int(block.get("count", 0)) + 1):
            id = f"{prefix}{index}"
            resources.append(Resource(id, kind, hours, tuple(sorted(outages.pop(id, []), key=lambda i: i.start))))
    if outages:
        raise ValueError(f"Outages reference unknown resources: {sorted(outages)}")
    return tuple(resources)


def _rules(spec: dict) -> tuple[VisitRule, ...]:
    return tuple(VisitRule(entry["id"], int(entry["target_day"]), int(entry["early_days"]), int(entry["late_days"]),
                           int(entry["duration_minutes"]),
                           tuple(sorted((kind, int(count)) for kind, count in entry.get("requirements", {}).items())),
                           tuple(entry.get("predecessors", ())), str(entry.get("version", "1")))
                 for entry in spec["rules"])


def _check_feasible_inputs(scenario: Scenario, days: tuple[date, ...]) -> None:
    """Reject authoring mistakes that make a scenario trivially unschedulable."""
    supply = {kind: sum(1 for r in scenario.resources if r.kind == kind) for kind in RESOURCE_KINDS}
    for rule in scenario.rules:
        for kind, count in rule.requirements:
            if supply[kind] < count:
                raise ValueError(f"Rule {rule.id} needs {count} {kind} resources but the scenario declares {supply[kind]}")
    clinic_days = set(days)
    for participant in scenario.participants:
        for rule in scenario.rules:
            window = visit_window(participant, rule)
            if not any(window.earliest <= day <= window.latest for day in clinic_days):
                raise ValueError(f"Horizon misses the window of {participant.id}/{rule.id}")


def build_scenario(spec: dict) -> Scenario:
    """Expand a specification into the shared immutable Scenario object."""
    days = _clinic_days(spec)
    open_time, close_time = time.fromisoformat(spec["clinic_open"]), time.fromisoformat(spec["clinic_close"])
    if close_time <= open_time:
        raise ValueError("Clinic must close after it opens")
    scenario = Scenario(_participants(spec, days, open_time, close_time), _rules(spec),
                        _resources(spec, days, open_time, close_time),
                        tuple(_interval(day, open_time, close_time) for day in days), int(spec["seed"]))
    _check_feasible_inputs(scenario, days)
    return scenario


def load_specs(directory: Path = DATA_DIR) -> dict[str, dict]:
    """Specifications by identifier, ordered by the ``order`` field then identifier."""
    specs = {}
    for path in sorted(directory.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        id = spec.get("id") or path.stem
        if id in specs:
            raise ValueError(f"Duplicate scenario identifier {id}")
        specs[id] = spec | {"id": id}
    return dict(sorted(specs.items(), key=lambda item: (item[1].get("order", 0), item[0])))


def load_scenario(id: str, directory: Path = DATA_DIR) -> Scenario:
    specs = load_specs(directory)
    if id not in specs:
        raise KeyError(f"Unknown scenario {id!r}")
    return build_scenario(specs[id])
