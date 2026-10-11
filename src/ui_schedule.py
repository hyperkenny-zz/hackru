"""Native Streamlit schedule visualization and appointment explanations."""
import altair as alt
import pandas as pd
import streamlit as st
from src.models import Appointment, Scenario
from src.schedule_views import timeline_rows
from src.windows import visit_window

STATUS_COLORS = {"Scheduled": "#2563eb", "Unchanged": "#2563eb", "Added": "#15803d",
                 "Moved": "#b45309", "Cancelled": "#6b7280", "Directly affected": "#dc2626",
                 "Successor to review": "#a16207", "Outage": "#dc2626"}


def timeline_chart(rows: list[dict]) -> alt.LayerChart:
    data = pd.DataFrame(rows)
    domain = [max(0, (int(data.start_minute.min()) // 60) * 60 - 30),
              min(1440, ((int(data.end_minute.max()) + 59) // 60) * 60 + 30)]
    shown_statuses = [status for status in STATUS_COLORS if status in set(data.status)]
    chart = alt.Chart(data).encode(
        x=alt.X("start_minute:Q", title="Clinic time · America/New_York",
                scale=alt.Scale(domain=domain),
                axis=alt.Axis(values=list(range(0, 1441, 60)),
                              labelExpr="floor(datum.value / 60) + ':' + (datum.value % 60 < 10 ? '0' : '') + (datum.value % 60)")),
        x2="end_minute:Q",
        y=alt.Y("lane:N", title=None, sort=sorted(data.lane.unique(), key=lambda lane: (0 if lane.startswith("Before repair") else 1, lane)), axis=alt.Axis(labelLimit=240)),
        color=alt.Color("status:N", title="Status", scale=alt.Scale(domain=shown_statuses, range=[STATUS_COLORS[status] for status in shown_statuses]),
                        legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("participant:N", title="Participant"), alt.Tooltip("visit:N", title="Visit"),
                 alt.Tooltip("resources:N", title="Resources"), alt.Tooltip("start:N", title="Start (clinic time)"),
                 alt.Tooltip("end:N", title="End (clinic time)"), alt.Tooltip("window:N", title="Allowed dates"),
                 alt.Tooltip("target:N", title="Target date"), alt.Tooltip("status:N", title="Status")])
    outages = chart.transform_filter(alt.datum.kind == "Outage").mark_bar(size=28, opacity=0.18)
    visits = chart.transform_filter(alt.datum.kind == "Appointment").mark_bar(size=14, cornerRadius=3)
    return (outages + visits).properties(height=max(120, 32 * data.lane.nunique()))


def render_timeline(series: tuple[tuple[str, Scenario, tuple[Appointment, ...], dict], ...], key: str) -> None:
    st.subheader("Appointment timeline")
    days = sorted({i.start.date() for _, s, _, _ in series for i in s.clinic_hours} |
                  {a.interval.start.date() for _, _, items, _ in series for a in items})
    if not days:
        st.caption("No clinic days or appointments to display.")
        return
    occupied = sorted({a.interval.start.date() for _, _, items, _ in series for a in items})
    default = days.index(occupied[0]) if occupied else 0
    day = st.selectbox("Timeline day", days, index=default, key=f"{key}_day", format_func=lambda d: d.strftime("%a, %b %d, %Y"))
    group = st.selectbox("Group timeline by", ("Resource", "Participant"), key=f"{key}_group")
    rows = [row for label, scenario, items, statuses in series
            for row in timeline_rows(scenario, items, day, group, label, statuses)]
    st.caption("Times use America/New_York. Hover over an appointment for its visit window and target date. "
               "In resource view, an appointment appears once for each assigned resource; shaded bands show outages.")
    if rows:
        st.altair_chart(timeline_chart(rows), key=f"{key}_chart", width="stretch",
                        alt="Appointment times grouped by resource or participant, with outages and review status")
    else:
        st.info("No appointments or resource outages on this day. Select another timeline day.")


def render_appointment_details(scenario: Scenario, appointments: tuple[Appointment, ...], key: str) -> None:
    if not appointments:
        return
    with st.expander("Appointment details and visit window"):
        ordered = sorted(appointments, key=lambda a: (a.interval.start, a.key))
        by_key = {a.key: a for a in ordered}
        chosen = st.selectbox("Appointment", list(by_key), key=f"{key}_appointment",
                              format_func=lambda k: f"{k[0]} / {k[1]} · {by_key[k].interval.start:%b %d %H:%M}")
        a = by_key[chosen]
        p = next(p for p in scenario.participants if p.id == a.participant_id)
        rule = next(r for r in scenario.rules if r.id == a.visit_id)
        window = visit_window(p, rule)
        st.table({"Participant / visit": f"{p.id} / {rule.id}", "Rule version": rule.version,
                  "Reference date": str(p.reference_date),
                  "Target calculation": f"{p.reference_date} + {rule.target_day} days = {window.target}",
                  "Allowed dates (inclusive)": f"{window.earliest} to {window.latest}",
                  "Appointment (clinic time)": f"{a.interval.start:%Y-%m-%d %H:%M} to {a.interval.end:%Y-%m-%d %H:%M}",
                  "Duration": f"{rule.duration_minutes} minutes", "Assigned resources": ", ".join(a.resource_ids) or "None",
                  "Required resources": ", ".join(f"{n} {kind}" for kind, n in rule.requirements) or "None",
                  "Predecessors": ", ".join(rule.predecessors) or "None"},
                 alt="Selected appointment, reference-date calculation, visit window, and resource requirements")
