"""Streamlit workflow: load a synthetic scenario, generate a schedule, review, confirm, and export.

SQLite holds committed schedules. A generated proposal stays in Streamlit session
state until it is confirmed, so discarding it leaves the database untouched. Only
the built-in scenario specifications are cached; schedules are solved on demand
and kept in session state, because a cached result keyed by scenario identifier
would survive a change to the scenario itself.
"""
from collections import Counter
from datetime import datetime, time
from time import perf_counter
import pandas as pd
import streamlit as st
from src.export import (APPOINTMENT_COLUMNS, CHANGE_COLUMNS, UNSCHEDULED_COLUMNS, appointment_rows,
                        change_rows, to_csv, unscheduled_rows)
from src.models import CLINIC_TZ, Interval, Scenario, ScheduleResult
from src.optimizer import OptimizationResult, optimize_schedule
from src.repair import repair_schedule, with_participant_availability, with_resource_outage
from src.scenarios import load_scenario, load_specs
from src.scheduler import greedy_schedule
from src.storage import ScheduleStore, VersionConflict, local
from src.validator import validate_schedule

PROPOSAL, REPAIR, DISRUPTIONS, FLASH = "proposal", "repair_proposal", "disruptions", "flash"
ENGINES = {"greedy": "Greedy baseline", "cpsat": "CP-SAT optimizer"}
CONFIRMABLE_STATUSES = ("OPTIMAL", "FEASIBLE")
STATUS_NOTES = {
    "OPTIMAL": "All three objectives are proven optimal. Review and confirm the proposal.",
    "FEASIBLE": "A valid schedule was found, but not every objective is proven optimal. "
                "Review and confirm the proposal.",
    "INFEASIBLE": "The current constraints cannot be satisfied, so there is nothing to confirm. "
                  "Adjust the inputs and generate again.",
    "UNKNOWN": "No verifiable schedule was found inside the search budget, so there is nothing to "
               "confirm. Raise the budget or relax the inputs and generate again.",
    "GREEDY": "Deterministic greedy baseline. It does not maximize the scheduled count and claims "
              "no optimality.",
}


@st.cache_data(show_spinner=False)
def cached_scenario(scenario_id: str) -> Scenario:
    return load_scenario(scenario_id)


def solve(engine: str, scenario: Scenario, budget: float) -> tuple[ScheduleResult | None, OptimizationResult | None, float]:
    """Run the selected engine; the greedy baseline reports no solver statistics."""
    started = perf_counter()
    if engine == "greedy":
        return greedy_schedule(scenario), None, perf_counter() - started
    report = optimize_schedule(scenario, time_limit_seconds=budget)
    return report.result, report, report.runtime_seconds


def participant_frame(scenario: Scenario) -> pd.DataFrame:
    return pd.DataFrame([{"participant_id": p.id, "reference_date": p.reference_date.isoformat(),
                          "available_days": len({i.start.date() for i in p.availability}),
                          "available_hours": round(sum((i.end - i.start).total_seconds() for i in p.availability) / 3600, 1),
                          "first_block": f"{p.availability[0].start:%Y-%m-%d %H:%M}-{p.availability[0].end:%H:%M}" if p.availability else ""}
                         for p in scenario.participants])


def resource_frame(scenario: Scenario) -> pd.DataFrame:
    return pd.DataFrame([{"resource_id": r.id, "kind": r.kind,
                          "working_days": len(r.working_hours),
                          "daily_shift": f"{r.working_hours[0].start:%H:%M}-{r.working_hours[0].end:%H:%M}" if r.working_hours else "",
                          "outage_days": len(r.outages),
                          "outage_dates": ", ".join(sorted({f"{i.start:%Y-%m-%d}" for i in r.outages}))}
                         for r in scenario.resources])


def rule_frame(scenario: Scenario) -> pd.DataFrame:
    return pd.DataFrame([{"visit_id": r.id, "rule_version": r.version, "target_day": r.target_day,
                          "window_days": f"-{r.early_days}/+{r.late_days}",
                          "duration_minutes": r.duration_minutes,
                          "requirements": ", ".join(f"{count} {kind}" for kind, count in r.requirements),
                          "predecessors": ", ".join(r.predecessors)}
                         for r in scenario.rules])


def frame(rows: list[dict], columns) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(columns))


def download(label: str, text: str, name: str, key: str) -> None:
    st.download_button(label, text.encode("utf-8"), file_name=name, mime="text/csv", key=key)


def flash(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message)


def apply_disruptions(scenario: Scenario, records: tuple[dict, ...]) -> Scenario:
    """Fold disruption inputs over a scenario, returning a new draft scenario.

    An availability change replaces one participant's whole availability list, so
    the blocks recorded for other clinic days are carried over unchanged.
    """
    draft = scenario
    for record in records:
        if record["kind"] == "outage":
            draft = with_resource_outage(draft, record["resource_id"],
                                         Interval(record["start"], record["end"]))
            continue
        participant = next((p for p in draft.participants if p.id == record["participant_id"]), None)
        if participant is None:
            raise ValueError(f"Unknown participant {record['participant_id']}")
        kept = tuple(i for i in participant.availability if i.start.date() != record["date"])
        replacement = () if record["start"] is None else (
            Interval(datetime.combine(record["date"], record["start"], CLINIC_TZ),
                     datetime.combine(record["date"], record["end"], CLINIC_TZ)),)
        draft = with_participant_availability(draft, record["participant_id"],
                                              tuple(sorted(kept + replacement, key=lambda i: i.start)))
    return draft


def disruption_detail(record: dict) -> str:
    if record["kind"] == "outage":
        return f"{record['start']:%Y-%m-%d %H:%M} to {record['end']:%Y-%m-%d %H:%M}"
    if record["start"] is None:
        return f"{record['date']:%Y-%m-%d} unavailable all day"
    return f"{record['date']:%Y-%m-%d} available {record['start']:%H:%M}-{record['end']:%H:%M}"


def disruption_frame(records: tuple[dict, ...]) -> pd.DataFrame:
    return pd.DataFrame([{"event": "resource outage" if record["kind"] == "outage" else "availability change",
                          "target": record.get("resource_id") or record["participant_id"],
                          "detail": disruption_detail(record), "reason": record["reason"]}
                         for record in records])


def disruption_records(scenario_id: str) -> tuple[dict, ...]:
    """Session-only disruption inputs belonging to the selected scenario."""
    stored = st.session_state.get(DISRUPTIONS)
    return stored["records"] if stored and stored["scenario_id"] == scenario_id else ()


def store_disruption(scenario_id: str, base: Scenario, record: dict) -> bool:
    """Validate a disruption input against the current draft, then keep it."""
    records = disruption_records(scenario_id) + (record,)
    try:
        apply_disruptions(base, records)
    except ValueError as error:
        st.error(f"That disruption input is not valid: {error}")
        return False
    st.session_state[DISRUPTIONS] = {"scenario_id": scenario_id, "records": records}
    return True


def stage_frame(report: OptimizationResult) -> pd.DataFrame:
    return pd.DataFrame([{"stage": stage.name, "status": stage.status,
                          "runtime_seconds": round(stage.runtime_seconds, 3),
                          "objective": stage.objective, "best_bound": stage.best_bound}
                         for stage in report.stages])


def report_panel(report: OptimizationResult | None, status: str, runtime: float) -> None:
    """Solver status, total runtime, and the staged objectives behind a proposal."""
    columns = st.columns(3)
    columns[0].metric("Solver status", status)
    columns[1].metric("Total runtime", f"{runtime:.2f} s")
    columns[2].metric("Proven optimal", "yes" if report is not None and report.optimal else "no")
    st.caption(STATUS_NOTES.get(status, status))
    if report is None:
        return
    st.caption(f"{report.candidate_count} candidate appointments enumerated. The search budget covers "
               "the CP-SAT searches only; candidate enumeration and model construction add to the "
               "total runtime above, so the total can exceed the budget.")
    with st.expander("Solver stages"):
        st.dataframe(stage_frame(report), width="stretch", hide_index=True)
        st.caption("Objectives are solved in order: scheduled count, changes to existing "
                   "appointments, then summed target-date deviation. A later stage runs only while "
                   "budget remains, and `NOT_RUN` means the budget was exhausted first.")


st.set_page_config(page_title="Clinical Trial Visit Scheduler", layout="wide")
st.title("Clinical Trial Visit Scheduler")

specs = load_specs()
if not specs:
    st.error("No scenario specifications found in data/.")
    st.stop()

with st.sidebar:
    st.header("Scenario")
    scenario_id = st.selectbox("Synthetic scenario", list(specs), key="scenario",
                               format_func=lambda id: specs[id]["name"])
    st.caption(specs[scenario_id]["description"])
    st.caption(f"Seed {specs[scenario_id]['seed']} · horizon from {specs[scenario_id]['start_date']} "
               f"over {specs[scenario_id]['days']} days")
    st.header("Engine")
    engine = st.radio("Scheduling engine", list(ENGINES), format_func=ENGINES.get, key="engine")
    budget = st.number_input("CP-SAT search budget (seconds)", min_value=1.0, max_value=120.0,
                             value=5.0, step=1.0, key="budget",
                             disabled=engine == "greedy", help="Shared across the three CP-SAT "
                             "search stages. Total runtime also includes candidate enumeration "
                             "and model construction.")

try:
    builtin_scenario = cached_scenario(scenario_id)
except (ValueError, KeyError) as error:
    st.error(f"Scenario {scenario_id} is invalid: {error}")
    st.stop()

snapshot_error = None
with ScheduleStore() as store:
    try:
        committed = store.load(scenario_id)
    except ValueError as error:
        snapshot_error, committed = error, store.load(scenario_id, 0)
    version_headers = store.versions(scenario_id)
    version_counts = store.appointment_counts(scenario_id)
    events = store.history(scenario_id, limit=500)
if snapshot_error is not None:
    st.error(f"{snapshot_error}. The built-in specification is shown instead, and nothing can be "
             "confirmed until the stored snapshot is readable.")
    st.stop()

# A confirmed version owns the scenario it was validated against, including any
# outage or availability change already accepted; older versions have no snapshot.
base_scenario = committed.scenario or builtin_scenario
restored = committed.scenario is not None

proposal = st.session_state.get(PROPOSAL)
if proposal and proposal["scenario_id"] != scenario_id:
    proposal = None
repair = st.session_state.get(REPAIR)
if repair and repair["scenario_id"] != scenario_id:
    repair = None
records = disruption_records(scenario_id)
try:
    draft_scenario = apply_disruptions(base_scenario, records)
except ValueError as error:
    st.error(f"The disruption inputs held in this session no longer apply: {error}")
    draft_scenario, records = base_scenario, ()
    st.session_state.pop(DISRUPTIONS, None)

with st.sidebar:
    st.header("Workflow")
    if st.button("Generate schedule", type="primary", width="stretch", key="generate"):
        with st.spinner(f"Generating a schedule with the {ENGINES[engine].lower()}..."):
            result, report, runtime = solve(engine, base_scenario, float(budget))
        st.session_state[PROPOSAL] = {"scenario_id": scenario_id, "engine": engine,
                                      "base_version": committed.version, "scenario": base_scenario,
                                      "generated_at": datetime.now().astimezone(), "result": result,
                                      "report": report, "runtime": runtime,
                                      "status": report.status if report else result.status}
        st.rerun()
    if proposal and st.button("Discard proposal", width="stretch", key="discard"):
        del st.session_state[PROPOSAL]
        flash("Proposal discarded. The committed schedule is unchanged.", "info")
        st.rerun()
    if (repair or records) and st.button("Discard repair", width="stretch", key="discard_repair"):
        st.session_state.pop(REPAIR, None)
        st.session_state.pop(DISRUPTIONS, None)
        flash("Repair proposal and disruption inputs discarded. The committed schedule is "
              "unchanged.", "info")
        st.rerun()
    st.caption(f"Committed version: {committed.version}"
               + (f" · {len(committed.appointments)} appointments" if committed.version else " (nothing committed yet)"))

if FLASH in st.session_state:
    kind, message = st.session_state.pop(FLASH)
    getattr(st, kind)(message)

visits = len(base_scenario.participants) * len(base_scenario.rules)
columns = st.columns(5)
for column, (label, value) in zip(columns, (("Participants", len(base_scenario.participants)),
                                            ("Visit rules", len(base_scenario.rules)),
                                            ("Resources", len(base_scenario.resources)),
                                            ("Clinic days", len(base_scenario.clinic_hours)),
                                            ("Protocol visits", visits))):
    column.metric(label, value)

inputs_tab, schedule_tab, repair_tab, history_tab = st.tabs(["Inputs", "Schedule", "Repair", "History"])

with inputs_tab:
    st.subheader("Visit rules")
    st.dataframe(rule_frame(base_scenario), width="stretch", hide_index=True)
    left, right = st.columns(2)
    with left:
        st.subheader("Participants")
        st.dataframe(participant_frame(base_scenario), width="stretch", hide_index=True)
    with right:
        st.subheader("Resources")
        st.dataframe(resource_frame(base_scenario), width="stretch", hide_index=True)
    st.caption(f"Scenario restored from committed version {committed.version}, including every "
               "confirmed outage and availability change."
               if restored else
               "Built-in specification from data/. A confirmed version stores the scenario it was "
               "validated against, and this page then shows that scenario instead."
               )
    st.caption("Reference dates anchor every visit window. Availability blocks and working hours are "
               "half-open intervals inside a single clinic day.")

with schedule_tab:
    if proposal is None:
        st.info("Select a scenario and choose Generate schedule to produce a proposal.")
    else:
        result, report = proposal["result"], proposal["report"]
        st.caption(f"{ENGINES[proposal['engine']]} · generated "
                   f"{proposal['generated_at']:%Y-%m-%d %H:%M:%S %Z}")
        report_panel(report, proposal["status"], proposal["runtime"])
        if result is None:
            st.error("The engine returned no schedule, so there is nothing to review or confirm. "
                     "The committed schedule is unchanged.")
        else:
            errors = validate_schedule(proposal["scenario"], result.appointments)
            head = st.columns(4)
            head[0].metric("Scheduled", f"{len(result.appointments)}/{visits}")
            head[1].metric("Unscheduled", len(result.unscheduled))
            head[2].metric("Base version", proposal["base_version"])
            head[3].metric("Independent validation", "failed" if errors else "passed")
            if errors:
                st.error("The independent validator rejected this proposal; it cannot be confirmed.")
                st.code("\n".join(errors[:20]))

            st.subheader("Proposed appointments")
            rows = appointment_rows(proposal["scenario"], result.appointments)
            st.dataframe(frame(rows, APPOINTMENT_COLUMNS), width="stretch", hide_index=True)
            download("Download appointments CSV", to_csv(rows, APPOINTMENT_COLUMNS),
                     f"{scenario_id}_proposed_appointments.csv", "proposal_csv")

            st.subheader("Unscheduled visits")
            if result.unscheduled:
                unscheduled = unscheduled_rows(proposal["scenario"], result.unscheduled)
                st.dataframe(frame(unscheduled, UNSCHEDULED_COLUMNS), width="stretch", hide_index=True)
                st.caption("`no_candidate_slots` means no slot satisfied the window, availability, clinic hours, "
                           "and resource rules. `resource_competition_or_precedence` means candidates existed but "
                           "lost to competition for shared resources or to a missing predecessor. Counts are "
                           "candidates before competition and precedence checks, not a minimal conflict set.")
                download("Download unscheduled CSV", to_csv(unscheduled, UNSCHEDULED_COLUMNS),
                         f"{scenario_id}_unscheduled.csv", "unscheduled_csv")
            else:
                st.success("Every protocol visit was scheduled.")

            st.subheader("Confirmation")
            note = st.text_input("Change note", placeholder="Why this schedule is being committed", key="note")
            if st.button("Confirm proposal", type="primary", disabled=bool(errors), key="confirm"):
                try:
                    with ScheduleStore() as store:
                        saved = store.confirm(scenario_id, proposal["base_version"],
                                              result.appointments, note, proposal["scenario"])
                except VersionConflict as error:
                    st.error(f"{error}. Generate the proposal again from the current version.")
                else:
                    del st.session_state[PROPOSAL]
                    flash(f"Committed version {saved.version} with {len(saved.appointments)} appointments.")
                    st.rerun()

    st.divider()
    st.subheader("Committed schedule")
    if committed.version == 0:
        st.caption("Nothing committed for this scenario yet.")
    else:
        st.caption(f"Version {committed.version} confirmed {local(committed.confirmed_at):%Y-%m-%d %H:%M %Z}"
                   + (f" · {committed.reason}" if committed.reason else ""))
        committed_rows = appointment_rows(base_scenario, committed.appointments)
        st.dataframe(frame(committed_rows, APPOINTMENT_COLUMNS), width="stretch", hide_index=True)
        download("Download committed CSV", to_csv(committed_rows, APPOINTMENT_COLUMNS),
                 f"{scenario_id}_committed_v{committed.version}.csv", "committed_csv")

with repair_tab:
    st.subheader("Disruption inputs")
    st.caption("Disruption inputs build a draft scenario for review. They are held in this session "
               "only until a repair proposal is confirmed, and confirming stores the draft scenario "
               "with that version, so accepted outages and availability changes survive a reload.")
    kinds = {r.id: r.kind for r in base_scenario.resources}
    clinic_days = sorted({i.start.date() for i in base_scenario.clinic_hours})
    outage_column, availability_column = st.columns(2)
    with outage_column:
        st.markdown("**Resource outage**")
        outage_resource = st.selectbox("Resource", list(kinds), key="outage_resource",
                                       format_func=lambda id: f"{id} ({kinds[id]})")
        outage_start_date = st.date_input("Start date", value=clinic_days[0], key="outage_start_date")
        outage_start_time = st.time_input("Start time", value=time(9), step=1800, key="outage_start_time")
        outage_end_date = st.date_input("End date", value=clinic_days[0], key="outage_end_date")
        outage_end_time = st.time_input("End time", value=time(17), step=1800, key="outage_end_time")
        outage_reason = st.text_input("Reason", key="outage_reason", placeholder="Equipment maintenance")
        if st.button("Add outage", key="add_outage"):
            record = {"kind": "outage", "resource_id": outage_resource,
                      "start": datetime.combine(outage_start_date, outage_start_time, CLINIC_TZ),
                      "end": datetime.combine(outage_end_date, outage_end_time, CLINIC_TZ),
                      "reason": outage_reason}
            if store_disruption(scenario_id, base_scenario, record):
                st.session_state.pop(REPAIR, None)
                flash(f"Outage recorded for {outage_resource}.", "info")
                st.rerun()
    with availability_column:
        st.markdown("**Participant rescheduling request**")
        request_participant = st.selectbox("Participant", [p.id for p in base_scenario.participants],
                                           key="request_participant")
        request_date = st.date_input("Clinic day", value=clinic_days[0], key="request_date")
        request_unavailable = st.checkbox("Unavailable all day", key="request_unavailable")
        request_start_time = st.time_input("New start time", value=time(9), step=1800,
                                           key="request_start_time", disabled=request_unavailable)
        request_end_time = st.time_input("New end time", value=time(13), step=1800,
                                         key="request_end_time", disabled=request_unavailable)
        request_reason = st.text_input("Request reason", key="request_reason", placeholder="Travel")
        st.caption("This replaces that participant's availability on the chosen day. Blocks recorded "
                   "for other days are kept.")
        if st.button("Replace availability", key="add_request"):
            record = {"kind": "availability", "participant_id": request_participant,
                      "date": request_date, "reason": request_reason,
                      "start": None if request_unavailable else request_start_time,
                      "end": None if request_unavailable else request_end_time}
            if store_disruption(scenario_id, base_scenario, record):
                st.session_state.pop(REPAIR, None)
                flash(f"Availability change recorded for {request_participant}.", "info")
                st.rerun()

    if records:
        st.dataframe(disruption_frame(records), width="stretch", hide_index=True)
        with st.expander("Draft scenario resources"):
            st.dataframe(resource_frame(draft_scenario), width="stretch", hide_index=True)
    else:
        st.caption("No disruption inputs recorded in this session.")

    st.subheader("Repair proposal")
    if committed.version == 0:
        st.info("Confirm a schedule first. A repair proposal starts from the committed schedule and "
                "minimizes changes to it.")
    else:
        pin_labels = {f"{a.participant_id} / {a.visit_id}": a.key
                      for a in sorted(committed.appointments, key=lambda a: a.key)}
        pins = st.multiselect("Pinned appointments that must keep their time and resources",
                              list(pin_labels), key="pins")
        prior_errors = validate_schedule(draft_scenario, committed.appointments)
        st.caption(f"Committed version {committed.version} with {len(committed.appointments)} "
                   f"appointments. Under the draft scenario the committed schedule has "
                   f"{len(prior_errors)} validation problems.")
        if st.button("Generate repair proposal", type="primary", key="repair"):
            try:
                with st.spinner("Generating a repair proposal..."):
                    generated = repair_schedule(scenario=draft_scenario,
                                                existing=committed.appointments,
                                                base_version=committed.version,
                                                pinned=tuple(pin_labels[label] for label in pins),
                                                time_limit_seconds=float(budget))
            except ValueError as error:
                st.error(f"The repair inputs are not valid: {error}")
            else:
                st.session_state[REPAIR] = {
                    "scenario_id": scenario_id, "scenario": draft_scenario, "proposal": generated,
                    "records": records, "pinned": tuple(pin_labels[label] for label in pins),
                    "prior_errors": len(prior_errors), "generated_at": datetime.now().astimezone()}
                st.rerun()

    if repair is not None:
        st.divider()
        proposal_report = repair["proposal"].optimization
        st.caption(f"Generated {repair['generated_at']:%Y-%m-%d %H:%M:%S %Z} from version "
                   f"{repair['proposal'].base_version} with {len(repair['pinned'])} pinned "
                   f"appointments and {len(repair['records'])} disruption inputs.")
        report_panel(proposal_report, proposal_report.status, proposal_report.runtime_seconds)
        if proposal_report.result is None:
            st.error("No repair proposal is available, so there is nothing to confirm. The committed "
                     "schedule is unchanged.")
            if repair["pinned"]:
                st.warning("Pinned appointments are never released automatically. Adjust the "
                           "disruption inputs or unpin the conflicting appointments, then generate "
                           "the proposal again.")
        else:
            repaired = proposal_report.result
            changes = repair["proposal"].changes
            counted = Counter(event for event, _, _ in changes)
            errors = validate_schedule(repair["scenario"], repaired.appointments)
            head = st.columns(4)
            head[0].metric("Scheduled after repair", f"{len(repaired.appointments)}/{visits}")
            head[1].metric("Unscheduled after repair", len(repaired.unscheduled))
            head[2].metric("Committed schedule problems", repair["prior_errors"])
            head[3].metric("Repair validation", "failed" if errors else "passed")
            counts = st.columns(3)
            for column, (event, label) in zip(counts, (("add", "Added"), ("move", "Moved"),
                                                       ("cancel", "Cancelled"))):
                column.metric(label, counted[event])
            if errors:
                st.error("The independent validator rejected this repair against the draft scenario; "
                         "it cannot be confirmed.")
                st.code("\n".join(errors[:20]))
            if repair["proposal"].base_version != committed.version:
                st.warning(f"This proposal was generated from version "
                           f"{repair['proposal'].base_version}, but version {committed.version} is "
                           "committed now. Generate the proposal again.")

            st.subheader("Changes")
            if changes:
                rows = change_rows(changes)
                st.dataframe(frame(rows, CHANGE_COLUMNS), width="stretch", hide_index=True)
                download("Download changes CSV", to_csv(rows, CHANGE_COLUMNS),
                         f"{scenario_id}_repair_changes.csv", "changes_csv")
            else:
                st.success("The committed schedule remains feasible; the proposal changes nothing.")

            st.subheader("Unscheduled after repair")
            if repaired.unscheduled:
                repaired_unscheduled = unscheduled_rows(repair["scenario"], repaired.unscheduled)
                st.dataframe(frame(repaired_unscheduled, UNSCHEDULED_COLUMNS), width="stretch",
                             hide_index=True)
                download("Download repair unscheduled CSV",
                         to_csv(repaired_unscheduled, UNSCHEDULED_COLUMNS),
                         f"{scenario_id}_repair_unscheduled.csv", "repair_unscheduled_csv")
            else:
                st.success("Every protocol visit is scheduled after the repair.")

            st.subheader("Confirmation")
            repair_note = st.text_input("Repair note", key="repair_note",
                                        placeholder="Why this repair is being committed")
            if st.button("Confirm repair", type="primary", disabled=bool(errors), key="confirm_repair"):
                try:
                    with ScheduleStore() as store:
                        saved = store.confirm(scenario_id, repair["proposal"].base_version,
                                              repaired.appointments, repair_note, repair["scenario"])
                except VersionConflict as error:
                    st.error(f"{error}. Generate the repair proposal again from the current version.")
                else:
                    del st.session_state[REPAIR]
                    st.session_state.pop(DISRUPTIONS, None)
                    flash(f"Committed version {saved.version} with {len(saved.appointments)} "
                          "appointments. Its disruption inputs are now part of the committed "
                          "scenario.")
                    st.rerun()

with history_tab:
    st.subheader("Schedule versions")
    if version_headers:
        st.dataframe(pd.DataFrame([{"version": header.version,
                                    "confirmed_at": f"{local(header.confirmed_at):%Y-%m-%d %H:%M %Z}",
                                    "appointments": version_counts.get(header.version, 0),
                                    "note": header.reason} for header in version_headers]),
                     width="stretch", hide_index=True)
    else:
        st.caption("No versions committed yet.")

    st.subheader("Change history")
    if events:
        st.dataframe(pd.DataFrame([{
            "version": event.version, "event": event.event_type,
            "participant_id": event.participant_id, "visit_id": event.visit_id,
            "from": f"{event.previous.interval.start:%Y-%m-%d %H:%M} {' '.join(event.previous.resource_ids)}" if event.previous else "",
            "to": f"{event.current.interval.start:%Y-%m-%d %H:%M} {' '.join(event.current.resource_ids)}" if event.current else "",
            "confirmed_at": f"{local(event.confirmed_at):%Y-%m-%d %H:%M %Z}", "note": event.reason}
            for event in events]), width="stretch", hide_index=True)
    else:
        st.caption("No change events recorded yet.")
