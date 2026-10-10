"""Streamlit workflow: load a synthetic scenario, generate a schedule, review, confirm, and export.

SQLite holds committed schedules. A generated proposal stays in Streamlit session
state until it is confirmed, so discarding it leaves the database untouched. Only
the built-in scenario specifications are cached; schedules are solved on demand
and kept in session state, because a cached result keyed by scenario identifier
would survive a change to the scenario itself.
"""
from datetime import datetime
from time import perf_counter
import pandas as pd
import streamlit as st
from src.export import (APPOINTMENT_COLUMNS, UNSCHEDULED_COLUMNS, appointment_rows, to_csv,
                        unscheduled_rows)
from src.models import Scenario, ScheduleResult
from src.optimizer import OptimizationResult, optimize_schedule
from src.scenarios import load_scenario, load_specs
from src.scheduler import greedy_schedule
from src.storage import ScheduleStore, VersionConflict, local
from src.validator import validate_schedule

PROPOSAL, FLASH = "proposal", "flash"
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
    scenario_id = st.selectbox("Synthetic scenario", list(specs), format_func=lambda id: specs[id]["name"])
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
    scenario = cached_scenario(scenario_id)
except (ValueError, KeyError) as error:
    st.error(f"Scenario {scenario_id} is invalid: {error}")
    st.stop()

with ScheduleStore() as store:
    committed = store.load(scenario_id)
    version_headers = store.versions(scenario_id)
    version_counts = store.appointment_counts(scenario_id)
    events = store.history(scenario_id, limit=500)

proposal = st.session_state.get(PROPOSAL)
if proposal and proposal["scenario_id"] != scenario_id:
    proposal = None

with st.sidebar:
    st.header("Workflow")
    if st.button("Generate schedule", type="primary", width="stretch", key="generate"):
        with st.spinner(f"Generating a schedule with the {ENGINES[engine].lower()}..."):
            result, report, runtime = solve(engine, scenario, float(budget))
        st.session_state[PROPOSAL] = {"scenario_id": scenario_id, "engine": engine,
                                      "base_version": committed.version, "scenario": scenario,
                                      "generated_at": datetime.now().astimezone(), "result": result,
                                      "report": report, "runtime": runtime,
                                      "status": report.status if report else result.status}
        st.rerun()
    if proposal and st.button("Discard proposal", width="stretch", key="discard"):
        del st.session_state[PROPOSAL]
        flash("Proposal discarded. The committed schedule is unchanged.", "info")
        st.rerun()
    st.caption(f"Committed version: {committed.version}"
               + (f" · {len(committed.appointments)} appointments" if committed.version else " (nothing committed yet)"))

if FLASH in st.session_state:
    kind, message = st.session_state.pop(FLASH)
    getattr(st, kind)(message)

visits = len(scenario.participants) * len(scenario.rules)
columns = st.columns(5)
for column, (label, value) in zip(columns, (("Participants", len(scenario.participants)),
                                            ("Visit rules", len(scenario.rules)),
                                            ("Resources", len(scenario.resources)),
                                            ("Clinic days", len(scenario.clinic_hours)),
                                            ("Protocol visits", visits))):
    column.metric(label, value)

inputs_tab, schedule_tab, history_tab = st.tabs(["Inputs", "Schedule", "History"])

with inputs_tab:
    st.subheader("Visit rules")
    st.dataframe(rule_frame(scenario), width="stretch", hide_index=True)
    left, right = st.columns(2)
    with left:
        st.subheader("Participants")
        st.dataframe(participant_frame(scenario), width="stretch", hide_index=True)
    with right:
        st.subheader("Resources")
        st.dataframe(resource_frame(scenario), width="stretch", hide_index=True)
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
                        saved = store.confirm(scenario_id, proposal["base_version"], result.appointments, note)
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
        committed_rows = appointment_rows(scenario, committed.appointments)
        st.dataframe(frame(committed_rows, APPOINTMENT_COLUMNS), width="stretch", hide_index=True)
        download("Download committed CSV", to_csv(committed_rows, APPOINTMENT_COLUMNS),
                 f"{scenario_id}_committed_v{committed.version}.csv", "committed_csv")

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
