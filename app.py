"""Streamlit workflow: load a synthetic scenario, generate a schedule, review, confirm, and export.

SQLite holds committed schedules. A generated proposal stays in Streamlit session
state until it is confirmed, so discarding it leaves the database untouched.
"""
from datetime import datetime
import pandas as pd
import streamlit as st
from src.export import (APPOINTMENT_COLUMNS, UNSCHEDULED_COLUMNS, appointment_rows, to_csv,
                        unscheduled_rows)
from src.models import Scenario, ScheduleResult
from src.scenarios import load_scenario, load_specs
from src.scheduler import greedy_schedule
from src.storage import ScheduleStore, VersionConflict, local
from src.validator import validate_schedule

PROPOSAL, FLASH = "proposal", "flash"


@st.cache_data(show_spinner=False)
def cached_scenario(scenario_id: str) -> Scenario:
    return load_scenario(scenario_id)


@st.cache_data(show_spinner="Generating a schedule with the greedy baseline...")
def cached_schedule(scenario_id: str) -> ScheduleResult:
    return greedy_schedule(cached_scenario(scenario_id))


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
        st.session_state[PROPOSAL] = {"scenario_id": scenario_id, "base_version": committed.version,
                                      "generated_at": datetime.now().astimezone(),
                                      "result": cached_schedule(scenario_id)}
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
        result = proposal["result"]
        errors = validate_schedule(scenario, result.appointments)
        scheduled = len(result.appointments)
        head = st.columns(4)
        head[0].metric("Scheduled", f"{scheduled}/{visits}")
        head[1].metric("Unscheduled", len(result.unscheduled))
        head[2].metric("Base version", proposal["base_version"])
        head[3].metric("Independent validation", "failed" if errors else "passed")
        st.caption(f"Status {result.status} · generated {proposal['generated_at']:%Y-%m-%d %H:%M:%S %Z}")
        if errors:
            st.error("The independent validator rejected this proposal; it cannot be confirmed.")
            st.code("\n".join(errors[:20]))

        st.subheader("Proposed appointments")
        rows = appointment_rows(scenario, result.appointments)
        st.dataframe(frame(rows, APPOINTMENT_COLUMNS), width="stretch", hide_index=True)
        download("Download appointments CSV", to_csv(rows, APPOINTMENT_COLUMNS),
                 f"{scenario_id}_proposed_appointments.csv", "proposal_csv")

        st.subheader("Unscheduled visits")
        if result.unscheduled:
            unscheduled = unscheduled_rows(scenario, result.unscheduled)
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
