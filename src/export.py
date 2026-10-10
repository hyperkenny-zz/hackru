"""Tabular appointment views and CSV export for the application."""
import csv
import io
from collections.abc import Iterable, Sequence
from src.models import Appointment, Scenario, UnscheduledVisit
from src.windows import visit_window

APPOINTMENT_COLUMNS = ("participant_id", "visit_id", "date", "start", "end", "duration_minutes",
                       "resources", "window_earliest", "window_target", "window_latest",
                       "days_from_target", "schedule_version")
UNSCHEDULED_COLUMNS = ("participant_id", "visit_id", "reason", "candidate_count",
                       "window_earliest", "window_target", "window_latest")


def _index(scenario: Scenario):
    return {p.id: p for p in scenario.participants}, {r.id: r for r in scenario.rules}


def appointment_rows(scenario: Scenario, appointments: Iterable[Appointment]) -> list[dict]:
    """One row per appointment, ordered by start time, with its protocol window."""
    participants, rules = _index(scenario)
    rows = []
    for appointment in appointments:
        window = visit_window(participants[appointment.participant_id], rules[appointment.visit_id])
        start, end = appointment.interval.start, appointment.interval.end
        rows.append({"participant_id": appointment.participant_id, "visit_id": appointment.visit_id,
                     "date": start.date().isoformat(), "start": start.strftime("%H:%M"),
                     "end": end.strftime("%H:%M"),
                     "duration_minutes": int((end - start).total_seconds() // 60),
                     "resources": " ".join(appointment.resource_ids),
                     "window_earliest": window.earliest.isoformat(),
                     "window_target": window.target.isoformat(),
                     "window_latest": window.latest.isoformat(),
                     "days_from_target": (start.date() - window.target).days,
                     "schedule_version": appointment.schedule_version})
    return sorted(rows, key=lambda row: (row["date"], row["start"], row["participant_id"], row["visit_id"]))


def unscheduled_rows(scenario: Scenario, unscheduled: Iterable[UnscheduledVisit]) -> list[dict]:
    """One row per unscheduled visit with its diagnostic category and candidate count."""
    participants, rules = _index(scenario)
    rows = []
    for visit in unscheduled:
        window = visit_window(participants[visit.participant_id], rules[visit.visit_id])
        rows.append({"participant_id": visit.participant_id, "visit_id": visit.visit_id,
                     "reason": visit.reason, "candidate_count": visit.candidate_count,
                     "window_earliest": window.earliest.isoformat(),
                     "window_target": window.target.isoformat(),
                     "window_latest": window.latest.isoformat()})
    return sorted(rows, key=lambda row: (row["participant_id"], row["visit_id"]))


def to_csv(rows: Sequence[dict], columns: Sequence[str]) -> str:
    """Render rows as CSV text with a fixed header and Unix line endings."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def appointments_csv(scenario: Scenario, appointments: Iterable[Appointment]) -> str:
    return to_csv(appointment_rows(scenario, appointments), APPOINTMENT_COLUMNS)


def unscheduled_csv(scenario: Scenario, unscheduled: Iterable[UnscheduledVisit]) -> str:
    return to_csv(unscheduled_rows(scenario, unscheduled), UNSCHEDULED_COLUMNS)
