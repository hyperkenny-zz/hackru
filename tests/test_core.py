from dataclasses import replace
from datetime import date, datetime
import unittest
from src.models import CLINIC_TZ, Appointment, Interval, Participant, Resource, Scenario, VisitRule
from src.scheduler import greedy_schedule
from src.validator import validate_schedule
from src.windows import visit_window


def interval(start=9, end=11, day="2026-10-12"):
    return Interval(datetime.fromisoformat(f"{day}T{start:02}:00:00").replace(tzinfo=CLINIC_TZ),
                    datetime.fromisoformat(f"{day}T{end:02}:00:00").replace(tzinfo=CLINIC_TZ))


def scenario(count=2):
    hours = interval()
    return Scenario(tuple(Participant(f"P{i}", date(2026, 10, 12), (hours,)) for i in range(count)),
                    (VisitRule("V1", 0, 0, 0, 60, (("nurse", 1),)),),
                    (Resource("N1", "nurse", (hours,)),), (hours,))


class CoreTests(unittest.TestCase):
    def test_windows_cross_leap_month_and_year(self):
        rule = VisitRule("V", 1, 1, 1, 30, ())
        p = Participant("P", date(2024, 2, 28), ())
        w = visit_window(p, rule)
        self.assertEqual((w.earliest, w.target, w.latest), (date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1)))
        self.assertEqual(visit_window(replace(p, reference_date=date(2026, 12, 31)), rule).target, date(2027, 1, 1))

    def test_adjacent_appointments_and_determinism(self):
        s = scenario()
        result = greedy_schedule(s)
        self.assertEqual(len(result.appointments), 2)
        self.assertEqual(result.appointments[0].interval.end, result.appointments[1].interval.start)
        self.assertEqual(validate_schedule(s, result.appointments), [])
        self.assertEqual(result, greedy_schedule(s))
        self.assertEqual(result, greedy_schedule(replace(s, participants=tuple(reversed(s.participants)))))

    def test_insufficient_capacity(self):
        result = greedy_schedule(scenario(3))
        self.assertEqual(len(result.appointments), 2)
        self.assertEqual(result.unscheduled[0].reason, "resource_competition_or_precedence")
        self.assertEqual(result.unscheduled[0].candidate_count, 3)

    def test_outage_and_no_candidates(self):
        s = scenario(1)
        resource = replace(s.resources[0], outages=(interval(),))
        result = greedy_schedule(replace(s, resources=(resource,)))
        self.assertEqual(result.appointments, ())
        self.assertEqual(result.unscheduled[0].reason, "no_candidate_slots")

    def test_precedence_and_missing_predecessor(self):
        s = scenario(1)
        follow = VisitRule("A_follow", 0, 0, 0, 60, (("nurse", 1),), ("V1",))
        s = replace(s, rules=(follow,) + s.rules)
        result = greedy_schedule(s)
        self.assertEqual([a.visit_id for a in result.appointments], ["V1", "A_follow"])
        self.assertTrue(validate_schedule(s, (result.appointments[1],)))
        s = replace(s, rules=(replace(s.rules[1], duration_minutes=120), follow))
        result = greedy_schedule(s)
        self.assertEqual(len(result.appointments), 1)
        self.assertEqual(result.unscheduled[0].visit_id, "A_follow")

    def test_independent_validator_rejects_invalid_appointments(self):
        s = scenario()
        a = greedy_schedule(s).appointments[0]
        self.assertTrue(validate_schedule(s, (a, a)))
        self.assertTrue(validate_schedule(s, (replace(a, resource_ids=()),)))
        self.assertTrue(validate_schedule(s, (replace(a, interval=interval(day="2026-10-13")),)))
        self.assertTrue(validate_schedule(s, (replace(a, participant_id="unknown"),)))
        unavailable = replace(s, resources=(replace(s.resources[0], outages=(interval(),)),))
        self.assertTrue(validate_schedule(unavailable, (a,)))

    def test_invalid_models(self):
        with self.assertRaises(ValueError):
            VisitRule("V", 0, 0, 0, 45, ())
        with self.assertRaises(ValueError):
            Interval(datetime(2026, 1, 1), datetime(2026, 1, 2))
        s = scenario()
        with self.assertRaises(ValueError):
            replace(s, participants=s.participants + s.participants)
        with self.assertRaises(ValueError):
            replace(s, rules=(replace(s.rules[0], predecessors=("missing",)),))
        with self.assertRaises(ValueError):
            replace(s, rules=(replace(s.rules[0], predecessors=("V1",)),))

    def test_dst_wall_times_rejected(self):
        for day, hour in (("2026-03-08", 2), ("2026-11-01", 1)):
            with self.assertRaises(ValueError):
                interval(hour, hour + 1, day)

    def test_inclusive_last_window_day(self):
        s = scenario(1)
        s = replace(s, rules=(replace(s.rules[0], target_day=-1, late_days=1),))
        self.assertEqual(len(greedy_schedule(s).appointments), 1)
        s = replace(s, rules=(replace(s.rules[0], late_days=0),))
        self.assertEqual(len(greedy_schedule(s).appointments), 0)

    def test_multiple_required_resources(self):
        s = scenario(1)
        s = replace(s, rules=(replace(s.rules[0], requirements=(("nurse", 2), ("equipment", 1))),),
                    resources=s.resources + (Resource("N2", "nurse", s.clinic_hours), Resource("E1", "equipment", s.clinic_hours)))
        result = greedy_schedule(s)
        self.assertEqual(result.appointments[0].resource_ids, ("E1", "N1", "N2"))
        self.assertFalse(validate_schedule(s, result.appointments))


if __name__ == "__main__":
    unittest.main()
