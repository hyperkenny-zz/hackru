"""Independent evaluation metrics for baseline and optimized schedules."""
from src.models import Scenario, ScheduleResult
from src.optimizer import placement
from src.validator import validate_schedule
from src.windows import visit_window


def schedule_metrics(scenario: Scenario, result: ScheduleResult, existing=()) -> dict:
    participants = {p.id: p for p in scenario.participants}
    rules = {r.id: r for r in scenario.rules}
    current = {a.key: a for a in result.appointments}
    return {
        "scheduled": len(result.appointments),
        "unscheduled": len(result.unscheduled),
        "violations": len(validate_schedule(scenario, result.appointments)),
        "existing_changes": sum(a.key not in current or placement(a) != placement(current[a.key]) for a in existing),
        "target_deviation_days": sum(abs((a.interval.start.date() - visit_window(participants[a.participant_id], rules[a.visit_id]).target).days)
                                     for a in result.appointments),
    }
