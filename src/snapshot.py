"""JSON encoding of scenarios, so a confirmed version can restore its own inputs.

A committed schedule is only interpretable against the scenario it was validated
against: once an outage or a rescheduling request has been confirmed, reloading
the built-in specification would describe a clinic that no longer matches the
appointments. The encoding is explicit rather than derived from the dataclasses,
so a model change surfaces as a decoding error instead of silent data loss.
"""
import json
from datetime import date, datetime
from src.models import Interval, Participant, Resource, Scenario, VisitRule

SNAPSHOT_FORMAT = 1


def _interval(interval: Interval) -> dict:
    return {"start": interval.start.isoformat(), "end": interval.end.isoformat()}


def _intervals(intervals: tuple[Interval, ...]) -> list[dict]:
    return [_interval(interval) for interval in intervals]


def _read_intervals(payload: list[dict]) -> tuple[Interval, ...]:
    return tuple(Interval(datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]))
                 for item in payload)


def encode_scenario(scenario: Scenario) -> dict:
    """A JSON-serializable snapshot of every scheduler input."""
    return {
        "format": SNAPSHOT_FORMAT,
        "seed": scenario.seed,
        "clinic_hours": _intervals(scenario.clinic_hours),
        "participants": [{"id": p.id, "reference_date": p.reference_date.isoformat(),
                          "availability": _intervals(p.availability)}
                         for p in scenario.participants],
        "rules": [{"id": r.id, "target_day": r.target_day, "early_days": r.early_days,
                   "late_days": r.late_days, "duration_minutes": r.duration_minutes,
                   "requirements": [[kind, count] for kind, count in r.requirements],
                   "predecessors": list(r.predecessors), "version": r.version}
                  for r in scenario.rules],
        "resources": [{"id": r.id, "kind": r.kind, "working_hours": _intervals(r.working_hours),
                       "outages": _intervals(r.outages)} for r in scenario.resources],
    }


def decode_scenario(payload: dict) -> Scenario:
    """Rebuild a scenario from a snapshot, rejecting an unknown snapshot format."""
    if payload.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"Unsupported scenario snapshot format {payload.get('format')!r}")
    try:
        return Scenario(
            tuple(Participant(item["id"], date.fromisoformat(item["reference_date"]),
                              _read_intervals(item["availability"]))
                  for item in payload["participants"]),
            tuple(VisitRule(item["id"], item["target_day"], item["early_days"], item["late_days"],
                            item["duration_minutes"],
                            tuple((kind, count) for kind, count in item["requirements"]),
                            tuple(item["predecessors"]), item["version"])
                  for item in payload["rules"]),
            tuple(Resource(item["id"], item["kind"], _read_intervals(item["working_hours"]),
                           _read_intervals(item["outages"])) for item in payload["resources"]),
            _read_intervals(payload["clinic_hours"]), payload["seed"])
    except (KeyError, TypeError) as error:
        raise ValueError(f"Malformed scenario snapshot: {error}") from error


def dumps(scenario: Scenario) -> str:
    return json.dumps(encode_scenario(scenario), sort_keys=True)


def loads(text: str) -> Scenario:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Scenario snapshot is not valid JSON: {error}") from error
    return decode_scenario(payload)
