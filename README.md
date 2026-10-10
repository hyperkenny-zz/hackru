# Clinical Trial Visit Scheduler

A local clinical trial visit scheduler using synthetic data. The first implementation provides shared domain models, inclusive date windows, a deterministic greedy baseline, and an independent appointment validator.

## Development

Python 3.11 or newer is required. The scheduling core uses only the standard library.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests can also run without installing dependencies:

```sh
python3 -m unittest discover -s tests -v
```

## Application integration

Construct immutable objects from `src.models`, then call:

```python
from src.scheduler import greedy_schedule
from src.validator import validate_schedule

result = greedy_schedule(scenario)
assert not validate_schedule(scenario, result.appointments)
```

`Scenario` contains participants, visit rules, resources, and clinic opening intervals. All participants receive all scenario visit rules. Rule offsets refer to each participant's reference date. IDs must be unique within each entity type. Resources have type `nurse` or `equipment`.

Use aware datetimes; inputs normalize to America/New_York. Ambiguous and nonexistent clinic wall times are rejected. Candidate starts fall on a 30-minute grid. Windows include both dates and intervals are half-open. An appointment must fit in one declared interval for each applicable availability or opening-hours constraint; merge contiguous intervals before input if they represent continuous availability.

`ScheduleResult.appointments` contains selected resource IDs and appointment intervals. `unscheduled` contains participant/visit IDs, a diagnostic category, and the number of candidates before competition and precedence checks. A missing predecessor prevents a successor from being scheduled. Diagnostics do not claim a minimal conflict set.

The baseline orders rules topologically, breaks ties by rule ID, and orders participants by window deadline and ID. It prefers candidates nearest the target date, then earlier starts and resource IDs. It is deterministic but does not maximize scheduled count or claim optimality. The independent validator checks every returned schedule.

The scheduler does not write storage. The application owns persistence, version allocation, and confirmation. `Appointment.schedule_version` defaults to zero until the application assigns a committed version.

## Next milestones

- Synthetic scenarios and a Streamlit workflow, owned by the application contributor.
- CP-SAT optimization, repair relative to existing appointments, pinned appointments, and evaluation metrics, owned by the scheduling contributor.
- SQLite proposal confirmation, version conflicts, and audit history, owned by the application contributor.

See [product design](docs/product.md) and [technical design](docs/technical.md).
