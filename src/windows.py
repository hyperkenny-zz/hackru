"""Inclusive protocol date windows, measured from reference day zero."""
from dataclasses import dataclass
from datetime import date, timedelta
from src.models import Participant, VisitRule


@dataclass(frozen=True)
class VisitWindow:
    earliest: date
    target: date
    latest: date


def visit_window(participant: Participant, rule: VisitRule) -> VisitWindow:
    target = participant.reference_date + timedelta(days=rule.target_day)
    return VisitWindow(target - timedelta(days=rule.early_days), target,
                       target + timedelta(days=rule.late_days))
