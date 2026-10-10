"""Shared, immutable domain objects for the scheduler and application."""
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

CLINIC_TZ = ZoneInfo("America/New_York")
SLOT_MINUTES = 30


def local_time(value: datetime) -> datetime:
    """Normalize an aware instant and reject ambiguous or nonexistent wall times."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Times must be timezone-aware")
    local = value.astimezone(CLINIC_TZ)
    wall = local.replace(tzinfo=None)
    first = wall.replace(tzinfo=CLINIC_TZ, fold=0)
    second = wall.replace(tzinfo=CLINIC_TZ, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Ambiguous or nonexistent clinic time")
    if value.replace(tzinfo=None) == wall and value.utcoffset() != local.utcoffset():
        raise ValueError("Invalid clinic time")
    return local


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, "start", local_time(self.start))
        object.__setattr__(self, "end", local_time(self.end))
        if self.end <= self.start:
            raise ValueError("Interval end must follow start")

    def contains(self, other: "Interval") -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class VisitRule:
    id: str
    target_day: int
    early_days: int
    late_days: int
    duration_minutes: int
    requirements: tuple[tuple[str, int], ...]
    predecessors: tuple[str, ...] = ()
    version: str = "1"

    def __post_init__(self):
        if not self.id or min(self.early_days, self.late_days) < 0:
            raise ValueError("Invalid visit rule")
        if self.duration_minutes <= 0 or self.duration_minutes % SLOT_MINUTES:
            raise ValueError("Duration must be a positive multiple of 30 minutes")
        kinds = [kind for kind, _ in self.requirements]
        if len(kinds) != len(set(kinds)) or any(k not in {"nurse", "equipment"} or n <= 0 for k, n in self.requirements):
            raise ValueError("Invalid resource requirements")


@dataclass(frozen=True)
class Participant:
    id: str
    reference_date: date
    availability: tuple[Interval, ...]


@dataclass(frozen=True)
class Resource:
    id: str
    kind: str
    working_hours: tuple[Interval, ...]
    outages: tuple[Interval, ...] = ()


@dataclass(frozen=True)
class Scenario:
    participants: tuple[Participant, ...]
    rules: tuple[VisitRule, ...]
    resources: tuple[Resource, ...]
    clinic_hours: tuple[Interval, ...]
    seed: int = 0

    def __post_init__(self):
        for items in (self.participants, self.rules, self.resources):
            ids = [item.id for item in items]
            if any(not id for id in ids) or len(ids) != len(set(ids)):
                raise ValueError("IDs must be nonempty and unique")
        if any(r.kind not in {"nurse", "equipment"} for r in self.resources):
            raise ValueError("Unknown resource kind")
        order = {r.id: r.predecessors for r in self.rules}
        visited, active = set(), set()
        def visit(key):
            if key not in order or key in active:
                raise ValueError("Unknown predecessor or precedence cycle")
            if key in visited:
                return
            active.add(key)
            for predecessor in order[key]:
                visit(predecessor)
            active.remove(key)
            visited.add(key)
        for key in order:
            visit(key)


@dataclass(frozen=True)
class Appointment:
    participant_id: str
    visit_id: str
    interval: Interval
    resource_ids: tuple[str, ...]
    schedule_version: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.participant_id, self.visit_id


@dataclass(frozen=True)
class UnscheduledVisit:
    participant_id: str
    visit_id: str
    reason: str
    candidate_count: int


@dataclass(frozen=True)
class ScheduleResult:
    appointments: tuple[Appointment, ...]
    unscheduled: tuple[UnscheduledVisit, ...]
    status: str = "GREEDY"
