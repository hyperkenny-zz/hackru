"""Presentation logic for disruption diagnostics and clinic-time timelines."""
from dataclasses import replace
from datetime import date, datetime
from src.models import Appointment, CLINIC_TZ, Interval, Participant, Resource, Scenario, VisitRule
from src.repair import with_participant_availability, with_resource_outage
from src.schedule_views import disruption_impacts, impact_rows, timeline_rows
from src.ui_schedule import timeline_chart
from test_core import interval


def chained():
    hours = interval(9, 12)
    s = Scenario((Participant("P", date(2026, 10, 12), (hours,)),),
                 (VisitRule("A", 0, 0, 0, 60, (("nurse", 1),)),
                  VisitRule("B", 0, 0, 0, 60, (("nurse", 1),), ("A",)),
                  VisitRule("C", 0, 0, 0, 60, (("nurse", 1),), ("B",))),
                 (Resource("N", "nurse", (hours,)),), (hours,))
    appointments = tuple(Appointment("P", id, interval(start, start + 1), ("N",))
                         for id, start in (("A", 9), ("B", 10), ("C", 11)))
    return s, appointments


def test_impacts_distinguish_direct_outage_and_transitive_review():
    s, items = chained()
    disrupted = with_resource_outage(s, "N", interval(9, 10))
    impacts = disruption_impacts(disrupted, tuple(reversed(items)))
    assert [(i.key[1], i.category) for i in impacts] == [
        ("A", "Directly affected"), ("B", "Successor to review"), ("C", "Successor to review")]
    assert "N" in impacts[0].reasons[0]
    rows = impact_rows(items, impacts)
    assert rows[0]["start"] == "2026-10-12 09:00"
    assert "may remain unchanged" in rows[1]["reason"]
    assert not disruption_impacts(s, items)


def test_rescheduling_impacts_and_half_open_outage_boundaries():
    s, items = chained()
    changed = with_participant_availability(s, "P", (interval(10, 12),))
    assert "Participant is unavailable" in disruption_impacts(changed, items)[0].reasons[0]
    outside = with_resource_outage(s, "N", interval(8, 9))
    assert not disruption_impacts(outside, items)
    after = with_resource_outage(s, "N", interval(12, 13))
    assert not disruption_impacts(after, items)


def test_timeline_duplicates_by_resource_but_not_participant():
    s, items = chained()
    item = replace(items[0], resource_ids=("N", "E"))
    rows = timeline_rows(s, (item,), date(2026, 10, 12))
    assert [r["lane"] for r in rows] == ["Schedule · N", "Schedule · E"]
    assert all((r["start_minute"], r["end_minute"]) == (540, 600) for r in rows)
    assert rows[0]["window"] == "2026-10-12 to 2026-10-12"
    assert len(timeline_rows(s, (item,), date(2026, 10, 12), "Participant")) == 1


def test_timeline_outage_overlay_clipping_and_empty_day():
    s, items = chained()
    disrupted = with_resource_outage(s, "N", interval(9, 10))
    rows = timeline_rows(disrupted, items, date(2026, 10, 12), statuses={items[0].key: "Directly affected"})
    assert rows[0]["status"] == "Directly affected"
    assert rows[-1]["kind"] == "Outage"
    assert len(timeline_rows(disrupted, items, date(2026, 10, 12), "Participant")) == 3
    assert timeline_rows(disrupted, items, date(2026, 10, 13)) == []
    span = Interval(datetime(2026, 10, 12, 23, 30, tzinfo=CLINIC_TZ),
                    datetime(2026, 10, 13, 0, 30, tzinfo=CLINIC_TZ))
    item = replace(items[0], interval=span)
    first = timeline_rows(s, (item,), date(2026, 10, 12))[0]
    second = timeline_rows(s, (item,), date(2026, 10, 13))[0]
    assert (first["start_minute"], first["end_minute"]) == (1410, 1440)
    assert (second["start_minute"], second["end_minute"]) == (0, 30)


def test_chart_is_valid_and_uses_numeric_clinic_time():
    s, items = chained()
    rows = timeline_rows(s, items, date(2026, 10, 12))
    spec = timeline_chart(rows).to_dict()
    assert spec["layer"][0]["encoding"]["x"]["type"] == "quantitative"
    assert len(spec["layer"]) == 2
    assert "America/New_York" in spec["layer"][0]["encoding"]["x"]["title"]
