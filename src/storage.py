"""SQLite committed schedule state, version allocation, and audit history.

SQLite is the only source of committed state. Pending proposals live in the
application's session state and never reach this module, so discarding a
proposal cannot modify the database. :meth:`ScheduleStore.confirm` allocates the
next version for a scenario, rejects a stale base version, and writes the
schedule together with its change events in one transaction.
"""
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from src.models import CLINIC_TZ, Appointment, Interval

REPO_DB_PATH = Path(__file__).resolve().parent.parent / "scheduler.sqlite3"
DB_PATH_VARIABLE = "SCHEDULER_DB_PATH"
SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_versions (
    scenario_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    confirmed_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scenario_id, version)
);
CREATE TABLE IF NOT EXISTS appointments (
    scenario_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    participant_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    resource_ids TEXT NOT NULL,
    PRIMARY KEY (scenario_id, version, participant_id, visit_id),
    FOREIGN KEY (scenario_id, version) REFERENCES schedule_versions (scenario_id, version)
);
CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('add', 'move', 'cancel')),
    participant_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    previous TEXT,
    current TEXT,
    reason TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL,
    FOREIGN KEY (scenario_id, version) REFERENCES schedule_versions (scenario_id, version)
);
"""


def default_db_path() -> Path:
    """The database path, overridable with the SCHEDULER_DB_PATH environment variable."""
    return Path(os.environ.get(DB_PATH_VARIABLE) or REPO_DB_PATH)


class VersionConflict(RuntimeError):
    """Raised when a proposal is confirmed against a version that is no longer current."""

    def __init__(self, scenario_id: str, expected: int, actual: int):
        super().__init__(f"Scenario {scenario_id} is at version {actual}, not the expected {expected}")
        self.scenario_id, self.expected, self.actual = scenario_id, expected, actual


@dataclass(frozen=True)
class CommittedSchedule:
    scenario_id: str
    version: int
    appointments: tuple[Appointment, ...]
    confirmed_at: datetime | None = None
    reason: str = ""


@dataclass(frozen=True)
class ChangeEvent:
    scenario_id: str
    version: int
    event_type: str
    participant_id: str
    visit_id: str
    previous: Appointment | None
    current: Appointment | None
    reason: str
    confirmed_at: datetime


def _appointment_payload(appointment: Appointment) -> str:
    return json.dumps({"start": appointment.interval.start.isoformat(),
                       "end": appointment.interval.end.isoformat(),
                       "resource_ids": list(appointment.resource_ids)}, sort_keys=True)


def _appointment(participant_id: str, visit_id: str, start: str, end: str,
                 resource_ids, version: int) -> Appointment:
    return Appointment(participant_id, visit_id,
                       Interval(datetime.fromisoformat(start), datetime.fromisoformat(end)),
                       tuple(resource_ids), version)


def _placement(appointment: Appointment) -> tuple:
    return appointment.interval.start, appointment.interval.end, appointment.resource_ids


def change_events(base: tuple[Appointment, ...], proposed: tuple[Appointment, ...]) -> tuple[tuple[str, Appointment | None, Appointment | None], ...]:
    """Additions, moves, and cancellations between a committed schedule and a proposal."""
    before = {a.key: a for a in base}
    after = {a.key: a for a in proposed}
    events = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old is None:
            events.append(("add", None, new))
        elif new is None:
            events.append(("cancel", old, None))
        elif _placement(old) != _placement(new):
            events.append(("move", old, new))
    return tuple(events)


class ScheduleStore:
    """Committed schedules, versions, and audit events for one SQLite database."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA)

    def __enter__(self) -> "ScheduleStore":
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def current_version(self, scenario_id: str) -> int:
        """The latest committed version, or zero when nothing is committed yet."""
        row = self._connection.execute(
            "SELECT MAX(version) AS version FROM schedule_versions WHERE scenario_id = ?", (scenario_id,)).fetchone()
        return row["version"] or 0

    def load(self, scenario_id: str, version: int | None = None) -> CommittedSchedule:
        """A committed schedule; version zero with no appointments when none exists."""
        version = self.current_version(scenario_id) if version is None else version
        if version == 0:
            return CommittedSchedule(scenario_id, 0, ())
        header = self._connection.execute(
            "SELECT confirmed_at, reason FROM schedule_versions WHERE scenario_id = ? AND version = ?",
            (scenario_id, version)).fetchone()
        if header is None:
            raise KeyError(f"Scenario {scenario_id} has no version {version}")
        rows = self._connection.execute(
            "SELECT participant_id, visit_id, start, end, resource_ids FROM appointments"
            " WHERE scenario_id = ? AND version = ? ORDER BY start, participant_id, visit_id",
            (scenario_id, version)).fetchall()
        appointments = tuple(_appointment(row["participant_id"], row["visit_id"], row["start"], row["end"],
                                          json.loads(row["resource_ids"]), version) for row in rows)
        return CommittedSchedule(scenario_id, version, appointments,
                                 datetime.fromisoformat(header["confirmed_at"]), header["reason"])

    def confirm(self, scenario_id: str, base_version: int, appointments: tuple[Appointment, ...],
                reason: str = "") -> CommittedSchedule:
        """Save a proposal as the next version, atomically, if the base version still holds."""
        keys = [a.key for a in appointments]
        if len(keys) != len(set(keys)):
            raise ValueError("Proposal contains duplicate participant/visit appointments")
        confirmed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            actual = self.current_version(scenario_id)
            if actual != base_version:
                raise VersionConflict(scenario_id, base_version, actual)
            base = self.load(scenario_id, base_version).appointments
            version = base_version + 1
            stored = tuple(Appointment(a.participant_id, a.visit_id, a.interval, a.resource_ids, version)
                           for a in appointments)
            connection.execute("INSERT INTO schedule_versions (scenario_id, version, confirmed_at, reason)"
                               " VALUES (?, ?, ?, ?)", (scenario_id, version, confirmed_at, reason))
            connection.executemany(
                "INSERT INTO appointments (scenario_id, version, participant_id, visit_id, start, end, resource_ids)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(scenario_id, version, a.participant_id, a.visit_id, a.interval.start.isoformat(),
                  a.interval.end.isoformat(), json.dumps(list(a.resource_ids))) for a in stored])
            connection.executemany(
                "INSERT INTO change_events (scenario_id, version, event_type, participant_id, visit_id,"
                " previous, current, reason, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(scenario_id, version, event_type,
                  (old or new).participant_id, (old or new).visit_id,
                  _appointment_payload(old) if old else None, _appointment_payload(new) if new else None,
                  reason, confirmed_at) for event_type, old, new in change_events(base, stored)])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return CommittedSchedule(scenario_id, version, stored, datetime.fromisoformat(confirmed_at), reason)

    def versions(self, scenario_id: str) -> tuple[CommittedSchedule, ...]:
        """Version headers, newest first, without loading their appointments."""
        rows = self._connection.execute(
            "SELECT v.version, v.confirmed_at, v.reason, COUNT(a.participant_id) AS count"
            " FROM schedule_versions v LEFT JOIN appointments a"
            " ON a.scenario_id = v.scenario_id AND a.version = v.version"
            " WHERE v.scenario_id = ? GROUP BY v.version ORDER BY v.version DESC", (scenario_id,)).fetchall()
        return tuple(CommittedSchedule(scenario_id, row["version"], (),
                                       datetime.fromisoformat(row["confirmed_at"]), row["reason"])
                     for row in rows)

    def appointment_counts(self, scenario_id: str) -> dict[int, int]:
        rows = self._connection.execute(
            "SELECT version, COUNT(*) AS count FROM appointments WHERE scenario_id = ? GROUP BY version",
            (scenario_id,)).fetchall()
        return {row["version"]: row["count"] for row in rows}

    def history(self, scenario_id: str, limit: int | None = None) -> tuple[ChangeEvent, ...]:
        """Change events, newest first."""
        query = ("SELECT * FROM change_events WHERE scenario_id = ?"
                 " ORDER BY version DESC, id DESC" + (" LIMIT ?" if limit else ""))
        rows = self._connection.execute(query, (scenario_id, limit) if limit else (scenario_id,)).fetchall()
        def restore(payload: str | None, row: sqlite3.Row) -> Appointment | None:
            if payload is None:
                return None
            fields = json.loads(payload)
            return _appointment(row["participant_id"], row["visit_id"], fields["start"], fields["end"],
                                fields["resource_ids"], row["version"])

        return tuple(ChangeEvent(scenario_id, row["version"], row["event_type"], row["participant_id"],
                                 row["visit_id"], restore(row["previous"], row), restore(row["current"], row),
                                 row["reason"], datetime.fromisoformat(row["confirmed_at"])) for row in rows)


def local(value: datetime) -> datetime:
    """Render a stored UTC audit timestamp in clinic time."""
    return value.astimezone(CLINIC_TZ)
