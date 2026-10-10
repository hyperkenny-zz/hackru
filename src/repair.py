"""Immutable disruption inputs and reviewable repair proposals."""
from dataclasses import dataclass, replace
from src.models import Appointment, Interval, Scenario
from src.optimizer import OptimizationResult, optimize_schedule
from src.storage import change_events


@dataclass(frozen=True)
class RepairProposal:
    base_version: int
    optimization: OptimizationResult
    changes: tuple[tuple[str, Appointment | None, Appointment | None], ...]


def with_resource_outage(scenario: Scenario, resource_id: str, interval: Interval) -> Scenario:
    if resource_id not in {r.id for r in scenario.resources}:
        raise ValueError("Unknown resource")
    return replace(scenario, resources=tuple(replace(r, outages=r.outages + (interval,))
                                             if r.id == resource_id else r for r in scenario.resources))


def with_participant_availability(scenario: Scenario, participant_id: str,
                                  availability: tuple[Interval, ...]) -> Scenario:
    if participant_id not in {p.id for p in scenario.participants}:
        raise ValueError("Unknown participant")
    return replace(scenario, participants=tuple(replace(p, availability=availability)
                                                if p.id == participant_id else p for p in scenario.participants))


def repair_schedule(scenario: Scenario, existing: tuple[Appointment, ...], base_version: int,
                    pinned: tuple[tuple[str, str], ...] = (), time_limit_seconds: float = 5.0) -> RepairProposal:
    if base_version < 0 or any(a.schedule_version != base_version for a in existing):
        raise ValueError("Existing appointments must belong to the proposal's base version")
    optimization = optimize_schedule(scenario, existing, pinned, time_limit_seconds)
    changes = change_events(existing, optimization.result.appointments) if optimization.result is not None else ()
    return RepairProposal(base_version, optimization, changes)
