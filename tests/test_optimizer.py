"""Optimization objectives verified against exhaustive tiny instances."""
from dataclasses import replace
from datetime import date
from itertools import product
from unittest.mock import patch
import pytest
from ortools.sat.python import cp_model
from src.metrics import schedule_metrics
from src.models import Appointment, Participant, Resource, Scenario, VisitRule
from src.optimizer import optimize_schedule, placement
from src.repair import repair_schedule, with_participant_availability, with_resource_outage
from src.scheduler import candidate_appointments, greedy_schedule
from src.storage import ScheduleStore, VersionConflict
from src.validator import validate_schedule
from test_core import interval


def tiny():
    hours = interval()
    return Scenario((Participant("P0", date(2026, 10, 12), (hours,)),
                     Participant("P1", date(2026, 10, 12), (interval(9, 10),))),
                    (VisitRule("V", 0, 0, 0, 60, (("nurse", 1),)),),
                    (Resource("N", "nurse", (hours,)),), (hours,))


def objective(scenario, appointments, existing):
    from src.models import ScheduleResult
    metrics = schedule_metrics(scenario, ScheduleResult(tuple(appointments), ()), existing)
    return -metrics["scheduled"], metrics["existing_changes"], metrics["target_deviation_days"]


def exhaustive(scenario, existing=()):
    groups = [(None,) + candidate_appointments(scenario, p, r)
              for p in scenario.participants for r in scenario.rules]
    scores = [objective(scenario, items, existing) for chosen in product(*groups)
              if not validate_schedule(scenario, items := tuple(a for a in chosen if a is not None))]
    return min(scores)


def test_beats_greedy_and_matches_exhaustive_count_and_changes():
    s = tiny()
    base = greedy_schedule(s).appointments
    assert len(base) == 1
    report = optimize_schedule(s, base)
    assert report.optimal
    assert len(report.result.appointments) == 2
    assert objective(s, report.result.appointments, base) == exhaustive(s, base)
    assert [stage.name for stage in report.stages] == ["scheduled_count", "existing_changes", "target_deviation"]
    assert not validate_schedule(s, report.result.appointments)
    assert report.result == optimize_schedule(replace(s, participants=tuple(reversed(s.participants))), base).result


def test_target_deviation_is_last_priority():
    s = tiny()
    next_day = interval(day="2026-10-13")
    s = replace(s, participants=(replace(s.participants[0], availability=s.clinic_hours + (next_day,)),),
                rules=(replace(s.rules[0], late_days=1),),
                clinic_hours=s.clinic_hours + (next_day,),
                resources=(replace(s.resources[0], working_hours=s.clinic_hours + (next_day,)),))
    old = Appointment("P0", "V", interval(9, 10, "2026-10-13"), ("N",))
    report = optimize_schedule(s, (old,))
    assert placement(report.result.appointments[0]) == placement(old)
    assert objective(s, report.result.appointments, (old,)) == exhaustive(s, (old,))
    fresh = optimize_schedule(s)
    assert objective(s, fresh.result.appointments, ()) == exhaustive(s)
    assert fresh.result.appointments[0].interval.start.date() == date(2026, 10, 12)


def test_precedence_matches_exhaustive_search():
    s = tiny()
    follow = VisitRule("follow", 0, 0, 0, 30, (), ("V",))
    s = replace(s, rules=s.rules + (follow,))
    report = optimize_schedule(s)
    assert report.optimal
    assert objective(s, report.result.appointments, ()) == exhaustive(s)
    assert not validate_schedule(s, report.result.appointments)


def test_pinned_appointment_can_reduce_count_and_conflicting_pin_is_infeasible():
    s = tiny()
    old = greedy_schedule(s).appointments
    report = optimize_schedule(s, old, (old[0].key,))
    assert len(report.result.appointments) == 1
    assert placement(report.result.appointments[0]) == placement(old[0])
    disrupted = with_resource_outage(s, "N", interval())
    report = optimize_schedule(disrupted, old, (old[0].key,))
    assert report.status == "INFEASIBLE"
    assert report.result is None


def test_no_candidates_is_a_valid_empty_optimum():
    s = with_resource_outage(tiny(), "N", interval())
    report = optimize_schedule(s)
    assert report.optimal
    assert report.result.appointments == ()
    assert all(u.reason == "no_candidate_slots" for u in report.result.unscheduled)


REAL_SOLVE = cp_model.CpSolver.solve


def time_out(self, model):
    self.parameters.max_time_in_seconds = 1e-9
    return REAL_SOLVE(self, model)


def test_unknown_uses_valid_fallback_and_does_not_claim_optimality():
    with patch.object(cp_model.CpSolver, "solve", time_out):
        report = optimize_schedule(tiny())
    assert report.status == "FEASIBLE"
    assert report.stages[0].status == "UNKNOWN"
    assert len(report.stages) == 1
    assert not report.optimal
    assert report.result.appointments == greedy_schedule(tiny()).appointments


def test_unknown_with_invalid_base_and_pins_has_no_confirmable_result():
    s = tiny()
    old = (Appointment("P0", "V", interval(10, 11), ("N",)),
           Appointment("P1", "V", interval(10, 11), ("N",)))
    with patch.object(cp_model.CpSolver, "solve", time_out):
        report = optimize_schedule(s, old, (old[0].key,))
    assert report.result is None
    assert report.status == "UNKNOWN"


def test_later_unknown_retains_previous_stage_incumbent():
    original = cp_model.CpSolver.solve
    calls = []
    def solve(self, model):
        calls.append(1)
        return original(self, model) if len(calls) == 1 else time_out(self, model)
    with patch.object(cp_model.CpSolver, "solve", solve):
        report = optimize_schedule(tiny())
    assert len(report.result.appointments) == 2
    assert report.status == "FEASIBLE"
    assert [s.status for s in report.stages] == ["OPTIMAL", "OPTIMAL", "UNKNOWN"]


def test_repair_outage_preserves_feasible_appointments_and_persistence(tmp_path):
    s = replace(tiny(), participants=(tiny().participants[0],),
                resources=tiny().resources + (Resource("N2", "nurse", tiny().clinic_hours),))
    with ScheduleStore(tmp_path / "state.sqlite3") as store:
        saved = store.confirm("tiny", 0, greedy_schedule(s).appointments)
        old = saved.appointments
        disrupted = with_resource_outage(s, old[0].resource_ids[0], old[0].interval)
        proposal = repair_schedule(disrupted, old, saved.version)
        assert proposal.optimization.result is not None
        assert [event[0] for event in proposal.changes] == ["move"]
        assert store.current_version("tiny") == 1
        assert not validate_schedule(disrupted, proposal.optimization.result.appointments)
        committed = store.confirm("tiny", proposal.base_version, proposal.optimization.result.appointments)
        assert committed.version == 2
        with pytest.raises(VersionConflict):
            store.confirm("tiny", proposal.base_version, proposal.optimization.result.appointments)
    with ScheduleStore(tmp_path / "state.sqlite3") as store:
        assert not validate_schedule(disrupted, store.load("tiny").appointments)
    assert s.resources[0].outages == ()


def test_availability_rescheduling_and_no_change_repair():
    s = replace(tiny(), participants=(tiny().participants[0],))
    old = greedy_schedule(s).appointments
    assert repair_schedule(s, old, 0).changes == ()
    changed = with_participant_availability(s, "P0", (interval(10, 11),))
    report = repair_schedule(changed, old, 0)
    assert report.optimization.result.appointments[0].interval.start.hour == 10
    assert report.changes[0][0] == "move"
    assert s.participants[0].availability != changed.participants[0].availability


def test_invalid_requests():
    s = tiny()
    old = greedy_schedule(s).appointments
    for limit in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            optimize_schedule(s, time_limit_seconds=limit)
    with pytest.raises(ValueError):
        optimize_schedule(s, old + old)
    with pytest.raises(ValueError):
        optimize_schedule(s, pinned=(("missing", "V"),))
    with pytest.raises(ValueError):
        repair_schedule(s, old, 2)
    with pytest.raises(ValueError):
        with_resource_outage(s, "missing", interval())
    with pytest.raises(ValueError):
        with_participant_availability(s, "missing", ())


def test_outage_can_cascade_to_a_previously_feasible_successor():
    hours = interval(9, 12)
    s = Scenario((Participant("P", date(2026, 10, 12), (hours,)),),
                 (VisitRule("A", 0, 0, 0, 60, (("nurse", 1),)),
                  VisitRule("B", 0, 0, 0, 60, (("nurse", 1),), ("A",))),
                 (Resource("N", "nurse", (hours,)),), (hours,))
    old = greedy_schedule(s).appointments
    disrupted = with_resource_outage(s, "N", interval(9, 10))
    proposal = repair_schedule(disrupted, old, 0)
    assert len(proposal.optimization.result.appointments) == 2
    assert [kind for kind, _, _ in proposal.changes] == ["move", "move"]
    assert not validate_schedule(disrupted, proposal.optimization.result.appointments)
    assert optimize_schedule(disrupted, old, (old[1].key,)).result is None


def test_multiple_resources_and_participant_overlap_constraints():
    s = tiny()
    s = replace(s, resources=s.resources + (Resource("N2", "nurse", s.clinic_hours),
                                           Resource("E", "equipment", s.clinic_hours)),
                rules=(replace(s.rules[0], requirements=(("nurse", 2), ("equipment", 1))),))
    report = optimize_schedule(s)
    assert len(report.result.appointments) == 2
    assert all(a.resource_ids == ("E", "N", "N2") for a in report.result.appointments)
    assert not validate_schedule(s, report.result.appointments)


def test_empty_scenario():
    s = Scenario((), (), (), ())
    report = optimize_schedule(s)
    assert report.optimal and report.result.appointments == ()
