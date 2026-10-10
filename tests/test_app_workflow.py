"""Synthetic scenarios, CSV export, SQLite state, and the Streamlit workflow."""
import importlib.util
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta, time as dt_time
from pathlib import Path
from src.export import (APPOINTMENT_COLUMNS, CHANGE_COLUMNS, UNSCHEDULED_COLUMNS, appointment_rows,
                        appointments_csv, change_rows, changes_csv, unscheduled_csv, unscheduled_rows)
from src.scenarios import DATA_DIR, build_scenario, load_scenario, load_specs
from src.scheduler import greedy_schedule
from src.storage import ScheduleStore, VersionConflict, change_events, default_db_path
from src.validator import validate_schedule

DISRUPTIONS, REPAIR = "disruptions", "repair_proposal"
HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None and importlib.util.find_spec("pandas") is not None


def spec(id="small_feasible"):
    return load_specs()[id]


class ScenarioTests(unittest.TestCase):
    def test_every_specification_builds_a_valid_scenario(self):
        specs = load_specs()
        self.assertEqual(list(specs), ["small_feasible", "baseline_30", "tight_capacity", "equipment_outage"])
        for id, item in specs.items():
            with self.subTest(scenario=id):
                scenario = build_scenario(item)
                self.assertEqual(len(scenario.participants), item["participants"]["count"])
                self.assertTrue(scenario.clinic_hours and scenario.rules and scenario.resources)
                self.assertTrue(all(i.start.weekday() < 5 for i in scenario.clinic_hours))
                self.assertEqual(scenario.seed, item["seed"])

    def test_expansion_is_deterministic_and_ignores_file_order(self):
        self.assertEqual(build_scenario(spec()), load_scenario("small_feasible"))
        shuffled = dict(reversed(list(spec().items())))
        self.assertEqual(build_scenario(shuffled), build_scenario(spec()))

    def test_availability_blocks_stay_inside_one_clinic_day(self):
        scenario = load_scenario("baseline_30")
        days = {i.start.date(): i for i in scenario.clinic_hours}
        for participant in scenario.participants:
            for block in participant.availability:
                self.assertEqual(block.start.date(), (block.end - timedelta(microseconds=1)).date())
                self.assertTrue(days[block.start.date()].contains(block))

    def test_shifts_and_outages_reach_the_resources(self):
        scenario = load_scenario("tight_capacity")
        nurse = next(r for r in scenario.resources if r.kind == "nurse")
        self.assertEqual((nurse.working_hours[0].start.hour, nurse.working_hours[0].end.hour), (9, 13))
        outage_dates = {i.start.date().isoformat() for r in load_scenario("equipment_outage").resources for i in r.outages}
        self.assertEqual(outage_dates, {entry["date"] for entry in spec("equipment_outage")["outages"]})

    def test_weekend_days_are_kept_when_requested(self):
        weekdays_only, every_day = (build_scenario(spec() | {"skip_weekends": value}) for value in (True, False))
        self.assertEqual(len(every_day.clinic_hours), spec()["days"])
        self.assertGreater(len(every_day.clinic_hours), len(weekdays_only.clinic_hours))
        self.assertEqual({i.start.weekday() for i in every_day.clinic_hours}, set(range(7)))
        self.assertTrue(all(i.start.weekday() < 5 for i in weekdays_only.clinic_hours))
        with self.assertRaises(ValueError) as caught:
            build_scenario(spec() | {"skip_weekends": True, "start_date": "2026-10-17", "days": 2})
        self.assertIn("no clinic days", str(caught.exception))

    def test_specifications_reject_authoring_mistakes(self):
        for change, message in ((lambda s: s | {"days": 3}, "Horizon misses"),
                                (lambda s: s | {"clinic_open": "17:00"}, "must close after"),
                                (lambda s: s | {"outages": [{"resource_id": "N9", "date": "2026-10-12",
                                                             "from": "09:00", "to": "10:00"}]}, "unknown resources"),
                                (lambda s: s | {"participants": s["participants"] | {"availability": "hourly"}}, "availability pattern"),
                                (lambda s: s | {"resources": {"nurse": {"count": 1, "shift": ["08:00", "17:00"]},
                                                              "equipment": {"count": 1}}}, "opening hours"),
                                (lambda s: s | {"resources": {"nurse": {"count": 1}, "equipment": {"count": 0}}}, "equipment resources"),
                                (lambda s: s | {"participants": s["participants"] | {"reference_spread_days": 99}}, "spread beyond")):
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                build_scenario(change(spec()))
            self.assertIn(message, str(caught.exception))

    def test_duplicate_identifiers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.json", "b.json"):
                (Path(directory) / name).write_text(json.dumps(spec() | {"id": "same"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_specs(Path(directory))

    def test_scenarios_exercise_both_unscheduled_diagnostics(self):
        for id, expected in (("small_feasible", set()), ("baseline_30", set()),
                             ("tight_capacity", {"no_candidate_slots", "resource_competition_or_precedence"}),
                             ("equipment_outage", {"no_candidate_slots", "resource_competition_or_precedence"})):
            with self.subTest(scenario=id):
                scenario = load_scenario(id)
                result = greedy_schedule(scenario)
                self.assertEqual(validate_schedule(scenario, result.appointments), [])
                self.assertEqual({u.reason for u in result.unscheduled}, expected)
                self.assertEqual(len(result.appointments) + len(result.unscheduled),
                                 len(scenario.participants) * len(scenario.rules))

    def test_unknown_scenario(self):
        with self.assertRaises(KeyError):
            load_scenario("missing")

    def test_data_directory_is_the_repository_folder(self):
        self.assertEqual(DATA_DIR.name, "data")
        self.assertTrue(sorted(DATA_DIR.glob("*.json")))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario("tight_capacity")
        self.result = greedy_schedule(self.scenario)

    def test_appointment_rows_are_ordered_and_describe_the_window(self):
        rows = appointment_rows(self.scenario, self.result.appointments)
        self.assertEqual(len(rows), len(self.result.appointments))
        self.assertEqual(rows, sorted(rows, key=lambda row: (row["date"], row["start"], row["participant_id"], row["visit_id"])))
        for row in rows:
            self.assertLessEqual(row["window_earliest"], row["date"])
            self.assertLessEqual(row["date"], row["window_latest"])
            self.assertEqual(row["duration_minutes"] % 30, 0)
            self.assertTrue(row["resources"])

    def test_csv_headers_and_row_count(self):
        text = appointments_csv(self.scenario, self.result.appointments)
        lines = text.splitlines()
        self.assertEqual(lines[0], ",".join(APPOINTMENT_COLUMNS))
        self.assertEqual(len(lines), len(self.result.appointments) + 1)
        unscheduled = unscheduled_csv(self.scenario, self.result.unscheduled).splitlines()
        self.assertEqual(unscheduled[0], ",".join(UNSCHEDULED_COLUMNS))
        self.assertEqual(len(unscheduled), len(self.result.unscheduled) + 1)
        self.assertEqual(text, appointments_csv(self.scenario, self.result.appointments))

    def test_unscheduled_rows_keep_diagnostics(self):
        rows = unscheduled_rows(self.scenario, self.result.unscheduled)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["reason"], {"no_candidate_slots", "resource_competition_or_precedence"})
            self.assertGreaterEqual(row["candidate_count"], 0)

    def test_empty_export_still_has_a_header(self):
        self.assertEqual(appointments_csv(self.scenario, ()).splitlines(), [",".join(APPOINTMENT_COLUMNS)])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "state.sqlite3"
        self.store = ScheduleStore(self.path)
        self.addCleanup(self.store.close)
        self.scenario = load_scenario("small_feasible")
        self.appointments = greedy_schedule(self.scenario).appointments

    def test_nothing_committed_yields_version_zero(self):
        self.assertEqual(self.store.current_version("small_feasible"), 0)
        empty = self.store.load("small_feasible")
        self.assertEqual((empty.version, empty.appointments, empty.confirmed_at), (0, (), None))

    def test_confirm_allocates_versions_and_stamps_appointments(self):
        first = self.store.confirm("small_feasible", 0, self.appointments, "initial")
        self.assertEqual(first.version, 1)
        self.assertTrue(all(a.schedule_version == 1 for a in first.appointments))
        second = self.store.confirm("small_feasible", 1, self.appointments[:-1], "cancel one")
        self.assertEqual((second.version, len(second.appointments)), (2, len(self.appointments) - 1))
        self.assertEqual(self.store.appointment_counts("small_feasible"), {1: len(self.appointments), 2: len(self.appointments) - 1})
        self.assertEqual([header.version for header in self.store.versions("small_feasible")], [2, 1])
        self.assertEqual([header.reason for header in self.store.versions("small_feasible")], ["cancel one", "initial"])

    def test_committed_appointments_survive_a_new_connection(self):
        self.store.confirm("small_feasible", 0, self.appointments, "initial")
        self.store.close()
        with ScheduleStore(self.path) as reopened:
            loaded = reopened.load("small_feasible")
        self.assertEqual(loaded.version, 1)
        self.assertEqual({(a.key, a.interval, a.resource_ids) for a in loaded.appointments},
                         {(a.key, a.interval, a.resource_ids) for a in self.appointments})
        self.assertEqual(validate_schedule(self.scenario, loaded.appointments), [])

    def test_scenarios_keep_independent_version_counters(self):
        self.store.confirm("small_feasible", 0, self.appointments, "")
        self.assertEqual(self.store.current_version("baseline_30"), 0)
        other = load_scenario("baseline_30")
        self.store.confirm("baseline_30", 0, greedy_schedule(other).appointments[:3], "")
        self.assertEqual((self.store.current_version("small_feasible"), self.store.current_version("baseline_30")), (1, 1))

    def test_stale_base_version_is_rejected_and_writes_nothing(self):
        self.store.confirm("small_feasible", 0, self.appointments, "initial")
        with self.assertRaises(VersionConflict) as caught:
            self.store.confirm("small_feasible", 0, self.appointments[:1], "racing")
        self.assertEqual((caught.exception.expected, caught.exception.actual), (0, 1))
        self.assertEqual(self.store.current_version("small_feasible"), 1)
        self.assertEqual(len(self.store.load("small_feasible").appointments), len(self.appointments))

    def test_a_second_connection_cannot_confirm_against_a_superseded_version(self):
        with ScheduleStore(self.path) as other:
            base = other.current_version("small_feasible")
            self.store.confirm("small_feasible", base, self.appointments, "first writer")
            with self.assertRaises(VersionConflict):
                other.confirm("small_feasible", base, self.appointments[:2], "second writer")
            self.assertEqual(other.current_version("small_feasible"), 1)
            self.assertEqual(len(other.history("small_feasible")), len(self.appointments))

    def test_duplicate_proposal_keys_are_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            self.store.confirm("small_feasible", 0, self.appointments + self.appointments[:1], "")
        self.assertEqual(self.store.current_version("small_feasible"), 0)
        self.assertEqual(self.store.versions("small_feasible"), ())

    def test_history_records_additions_moves_and_cancellations(self):
        self.store.confirm("small_feasible", 0, self.appointments, "initial")
        moved = replace(self.appointments[0], resource_ids=("N2",))
        self.store.confirm("small_feasible", 1, (moved,) + self.appointments[2:], "repair")
        events = self.store.history("small_feasible")
        latest = [event for event in events if event.version == 2]
        self.assertEqual({event.event_type for event in latest}, {"move", "cancel"})
        move = next(event for event in latest if event.event_type == "move")
        self.assertEqual((move.previous.resource_ids, move.current.resource_ids), (("N1",), ("N2",)))
        self.assertEqual(move.current.interval, moved.interval)
        cancel = next(event for event in latest if event.event_type == "cancel")
        self.assertEqual((cancel.participant_id, cancel.visit_id), self.appointments[1].key)
        self.assertIsNone(cancel.current)
        self.assertEqual(events[0].version, 2)
        self.assertEqual([event.version for event in self.store.history("small_feasible", limit=1)], [2])
        self.assertEqual(len(events), len(self.appointments) + len(latest))

    def test_change_events_compare_placements_only(self):
        self.assertEqual(change_events(self.appointments, self.appointments), ())
        self.assertEqual([kind for kind, _, _ in change_events((), self.appointments[:2])], ["add", "add"])
        self.assertEqual([kind for kind, _, _ in change_events(self.appointments[:2], ())], ["cancel", "cancel"])
        stamped = replace(self.appointments[0], schedule_version=7)
        self.assertEqual(change_events((self.appointments[0],), (stamped,)), ())

    def test_unknown_version_is_reported(self):
        with self.assertRaises(KeyError):
            self.store.load("small_feasible", 3)

    def test_default_path_follows_the_environment_variable(self):
        original = os.environ.get("SCHEDULER_DB_PATH")
        os.environ["SCHEDULER_DB_PATH"] = str(self.path)
        self.addCleanup(lambda: os.environ.pop("SCHEDULER_DB_PATH") if original is None
                        else os.environ.update({"SCHEDULER_DB_PATH": original}))
        self.assertEqual(default_db_path(), self.path)
        with ScheduleStore() as store:
            self.assertEqual(store.path, self.path)


@unittest.skipUnless(HAS_STREAMLIT, "Streamlit and pandas are required for the interface test")
class ApplicationTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        original = os.environ.get("SCHEDULER_DB_PATH")
        os.environ["SCHEDULER_DB_PATH"] = str(Path(directory.name) / "app.sqlite3")
        self.addCleanup(lambda: os.environ.pop("SCHEDULER_DB_PATH") if original is None
                        else os.environ.update({"SCHEDULER_DB_PATH": original}))
        self.app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=120).run()

    def frames(self, columns):
        """Rendered tables whose columns match an export schema."""
        return [frame.value for frame in self.app.dataframe if list(frame.value.columns) == list(columns)]

    def download_labels(self):
        return [button.label for button in self.app.download_button]

    def test_loading_shows_inputs_without_touching_committed_state(self):
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.title[0].value, "Clinical Trial Visit Scheduler")
        self.assertEqual([metric.value for metric in self.app.metric][:2], ["3", "3"])
        self.assertTrue(any("Generate schedule" in info.value for info in self.app.info))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 0)

    def test_generate_confirm_and_export_the_first_schedule(self):
        self.app.button(key="generate").click().run()
        self.assertFalse(self.app.exception)
        self.assertIn("passed", [metric.value for metric in self.app.metric])
        self.assertTrue(any("Every protocol visit was scheduled." in m.value for m in self.app.success))
        proposed = self.frames(APPOINTMENT_COLUMNS)[0]
        self.assertEqual(len(proposed), 9)
        self.assertEqual(set(proposed["schedule_version"]), {0})
        self.assertIn("Download appointments CSV", self.download_labels())

        self.app.text_input(key="note").set_value("first commit").run()
        self.app.button(key="confirm").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("Committed version 1" in m.value for m in self.app.success))
        self.assertNotIn("proposal", self.app.session_state)
        with ScheduleStore() as store:
            committed = store.load("small_feasible")
        self.assertEqual((committed.version, committed.reason), (1, "first commit"))
        self.assertEqual(len(committed.appointments), 9)
        self.assertIn("Download committed CSV", self.download_labels())
        committed_frame = self.frames(APPOINTMENT_COLUMNS)[0]
        self.assertEqual(set(committed_frame["schedule_version"]), {1})
        with ScheduleStore() as store:
            events = store.history("small_feasible")
        self.assertEqual(len(events), 9)
        self.assertEqual({event.event_type for event in events}, {"add"})

    def metrics(self):
        return {metric.label: metric.value for metric in self.app.metric}

    def test_the_optimizer_engine_generates_and_confirms_a_proven_schedule(self):
        self.app.radio(key="engine").set_value("cpsat").run()
        self.app.button(key="generate").click().run()
        self.assertFalse(self.app.exception)
        metrics = self.metrics()
        self.assertEqual(metrics["Solver status"], "OPTIMAL")
        self.assertEqual(metrics["Proven optimal"], "yes")
        self.assertEqual(metrics["Independent validation"], "passed")
        self.assertEqual(metrics["Scheduled"], "9/9")
        stages = self.frames(("stage", "status", "runtime_seconds", "objective", "best_bound"))[0]
        self.assertEqual(list(stages["stage"]), ["scheduled_count", "existing_changes", "target_deviation"])
        self.assertEqual(set(stages["status"]), {"OPTIMAL"})

        self.app.button(key="confirm").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("Committed version 1" in message.value for message in self.app.success))
        with ScheduleStore() as store:
            committed = store.load("small_feasible")
        self.assertEqual(committed.version, 1)
        self.assertEqual(validate_schedule(load_scenario("small_feasible"), committed.appointments), [])

    def test_the_greedy_engine_reports_no_optimality_claim(self):
        self.app.button(key="generate").click().run()
        metrics = self.metrics()
        self.assertEqual(metrics["Solver status"], "GREEDY")
        self.assertEqual(metrics["Proven optimal"], "no")
        self.assertFalse(self.frames(("stage", "status", "runtime_seconds", "objective", "best_bound")))

    def test_discarding_a_proposal_leaves_the_database_unchanged(self):
        self.app.button(key="generate").click().run()
        self.assertIn("proposal", self.app.session_state)
        self.app.button(key="discard").click().run()
        self.assertNotIn("proposal", self.app.session_state)
        self.assertTrue(any("unchanged" in info.value for info in self.app.info))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 0)
            self.assertEqual(store.history("small_feasible"), ())

    def test_stale_proposal_cannot_overwrite_a_newer_version(self):
        self.app.button(key="generate").click().run()
        with ScheduleStore() as store:
            store.confirm("small_feasible", 0, greedy_schedule(load_scenario("small_feasible")).appointments[:1], "other session")
        self.app.button(key="confirm").click().run()
        self.assertTrue(any("not the expected" in error.value for error in self.app.error))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 1)
            self.assertEqual(len(store.load("small_feasible").appointments), 1)

    def commit_initial_schedule(self):
        """Generate and confirm the greedy baseline so a repair has a committed base."""
        self.app.button(key="generate").click().run()
        self.app.button(key="confirm").click().run()
        self.assertFalse(self.app.exception)
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 1)

    def add_nurse_outage(self, day=date(2026, 10, 12)):
        self.app.selectbox(key="outage_resource").select("N1").run()
        for key, value in (("outage_start_date", day), ("outage_end_date", day)):
            self.app.date_input(key=key).set_value(value).run()
        self.app.time_input(key="outage_start_time").set_value(dt_time(9)).run()
        self.app.time_input(key="outage_end_time").set_value(dt_time(17)).run()
        self.app.text_input(key="outage_reason").set_value("Nurse absence").run()
        self.app.button(key="add_outage").click().run()
        self.assertFalse(self.app.exception)

    def button_keys(self):
        return {button.key for button in self.app.button}

    def test_an_outage_yields_a_repair_proposal_that_only_commits_on_confirmation(self):
        self.commit_initial_schedule()
        self.add_nurse_outage()
        disruptions = self.app.session_state[DISRUPTIONS]
        self.assertEqual(disruptions["scenario_id"], "small_feasible")
        self.assertEqual(len(disruptions["records"]), 1)

        self.app.button(key="repair").click().run()
        self.assertFalse(self.app.exception)
        metrics = self.metrics()
        self.assertIn(metrics["Solver status"], ("OPTIMAL", "FEASIBLE"))
        self.assertEqual(metrics["Scheduled after repair"], "9/9")
        self.assertEqual(metrics["Repair validation"], "passed")
        self.assertEqual(metrics["Committed schedule problems"], "1")
        self.assertEqual((metrics["Added"], metrics["Moved"], metrics["Cancelled"]), ("0", "1", "0"))
        changes = self.frames(CHANGE_COLUMNS)[0]
        self.assertEqual(len(changes), 1)
        row = changes.iloc[0]
        self.assertEqual((row["participant_id"], row["visit_id"], row["change"]), ("P1", "V1_screening", "move"))
        self.assertEqual(row["previous_resources"], "N1")
        self.assertNotEqual((row["new_start"], row["new_resources"]), (row["previous_start"], "N1"))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 1)

        self.app.text_input(key="repair_note").set_value("nurse absence repair").run()
        self.app.button(key="confirm_repair").click().run()
        self.assertFalse(self.app.exception)
        self.assertNotIn(REPAIR, self.app.session_state)
        with ScheduleStore() as store:
            committed = store.load("small_feasible")
            events = [event for event in store.history("small_feasible") if event.version == 2]
        self.assertEqual((committed.version, committed.reason), (2, "nurse absence repair"))
        self.assertEqual(len(committed.appointments), 9)
        self.assertEqual([event.event_type for event in events], ["move"])
        self.assertEqual(validate_schedule(load_scenario("small_feasible"), committed.appointments), [])

    def test_discarding_a_repair_clears_its_inputs_and_leaves_the_database_unchanged(self):
        self.commit_initial_schedule()
        self.add_nurse_outage()
        self.app.button(key="repair").click().run()
        self.assertIn(REPAIR, self.app.session_state)
        self.app.button(key="discard_repair").click().run()
        self.assertNotIn(REPAIR, self.app.session_state)
        self.assertNotIn(DISRUPTIONS, self.app.session_state)
        self.assertTrue(any("unchanged" in info.value for info in self.app.info))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 1)
            self.assertEqual({event.event_type for event in store.history("small_feasible")}, {"add"})

    def test_a_pinned_appointment_is_never_released_to_absorb_an_outage(self):
        self.commit_initial_schedule()
        self.add_nurse_outage()
        self.app.multiselect(key="pins").set_value(["P1 / V1_screening"]).run()
        self.app.button(key="repair").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.metrics()["Solver status"], "INFEASIBLE")
        self.assertTrue(any("unpin" in warning.value for warning in self.app.warning))
        self.assertNotIn("confirm_repair", self.button_keys())
        self.assertFalse(self.frames(CHANGE_COLUMNS))
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 1)

    def test_a_newer_committed_version_rejects_a_stale_repair_proposal(self):
        self.commit_initial_schedule()
        self.add_nurse_outage()
        self.app.button(key="repair").click().run()
        with ScheduleStore() as store:
            store.confirm("small_feasible", 1, store.load("small_feasible").appointments[:4], "other session")
        self.app.button(key="confirm_repair").click().run()
        self.assertTrue(any("Generate the proposal again" in warning.value for warning in self.app.warning))
        self.assertTrue(any("not the expected" in error.value for error in self.app.error))
        self.assertIn(REPAIR, self.app.session_state)
        with ScheduleStore() as store:
            self.assertEqual(store.current_version("small_feasible"), 2)
            self.assertEqual(len(store.load("small_feasible").appointments), 4)

    def test_changed_disruption_inputs_never_reuse_an_earlier_proposal(self):
        self.commit_initial_schedule()
        self.app.button(key="repair").click().run()
        self.assertFalse(self.frames(CHANGE_COLUMNS))
        self.assertTrue(any("changes nothing" in message.value for message in self.app.success))
        self.add_nurse_outage()
        self.assertNotIn(REPAIR, self.app.session_state)
        self.app.button(key="repair").click().run()
        changes = self.frames(CHANGE_COLUMNS)[0]
        self.assertEqual(list(changes["change"]), ["move"])

    def test_a_rescheduling_request_replaces_only_the_chosen_clinic_day(self):
        self.commit_initial_schedule()
        self.app.selectbox(key="request_participant").select("P1").run()
        self.app.date_input(key="request_date").set_value(date(2026, 10, 12)).run()
        self.app.checkbox(key="request_unavailable").set_value(True).run()
        self.app.text_input(key="request_reason").set_value("Travel").run()
        self.app.button(key="add_request").click().run()
        self.assertFalse(self.app.exception)
        self.app.button(key="repair").click().run()
        self.assertFalse(self.app.exception)
        metrics = self.metrics()
        self.assertEqual(metrics["Scheduled after repair"], "9/9")
        self.assertEqual(metrics["Repair validation"], "passed")
        changes = self.frames(CHANGE_COLUMNS)[0]
        self.assertEqual(list(changes["participant_id"]), ["P1"])
        self.assertEqual(list(changes["visit_id"]), ["V1_screening"])
        self.assertNotEqual(changes.iloc[0]["new_start"][:10], "2026-10-12")

    def test_switching_scenario_clears_the_proposal_and_shows_diagnostics(self):
        self.app.button(key="generate").click().run()
        self.app.selectbox(key="scenario").select("tight_capacity").run()
        self.assertTrue(any("Generate schedule" in info.value for info in self.app.info))
        self.app.button(key="generate").click().run()
        self.assertFalse(self.app.exception)
        unscheduled = self.frames(UNSCHEDULED_COLUMNS)[0]
        self.assertIn("no_candidate_slots", set(unscheduled["reason"]))
        self.assertIn("Download unscheduled CSV", self.download_labels())


if __name__ == "__main__":
    unittest.main()
