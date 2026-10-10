# Clinical Trial Visit Scheduler

A local clinical trial visit scheduler using synthetic data. The first implementation provides shared domain models, inclusive date windows, a deterministic greedy baseline, and an independent appointment validator.

## Development

Python 3.11 or newer is required. The scheduling core uses only the standard library; the
Streamlit application adds the dependencies pinned in `requirements.txt`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests can also run without installing dependencies. The interface tests are skipped when
Streamlit and Pandas are absent; every other test runs on the standard library alone:

```sh
python3 -m unittest discover -s tests -v
```

## Running the application

```sh
.venv/bin/python -m streamlit run app.py
```

Pick a scenario in the sidebar, review its participants, resources, and visit rules, then choose
**Generate schedule**. The schedule tab shows the proposed appointments, the unscheduled visits with
their diagnostic category, and the independent validator's verdict, and it exports both tables as
CSV. A proposal is confirmed with an optional change note; discarding it leaves the committed
schedule untouched.

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

## Synthetic scenarios

`data/` holds one JSON specification per scenario: a clinic horizon, participant and resource
counts, visit rules, optional part-time resource shifts, and optional outages. `src.scenarios`
expands a specification into a `Scenario`; expansion is deterministic for a fixed seed, so a
scenario identifier always produces identical scheduler inputs. Availability blocks and working
hours never span a clinic day, weekends are excluded, and the horizons avoid daylight-saving
transitions. Expansion rejects specifications whose horizon misses a visit window, whose shifts fall
outside clinic opening hours, or that declare fewer resources than a rule requires.

| Scenario | Participants | Resources | Purpose |
| --- | --- | --- | --- |
| `small_feasible` | 3 | 2 nurses, 1 equipment | Every visit has a feasible slot |
| `baseline_30` | 30 | 5 nurses, 2 equipment | The MVP population over three clinic weeks |
| `tight_capacity` | 30 | 1 morning-shift nurse, 1 equipment | Oversubscribed shared resources |
| `equipment_outage` | 30 | 5 nurses, 2 equipment with outages | Equipment withdrawn during the dosing weeks |

```python
from src.scenarios import load_scenario, load_specs

scenario = load_scenario("baseline_30")
```

## Committed state and versions

`src.storage.ScheduleStore` keeps committed schedules, per-scenario version numbers, and the change
history in SQLite at `scheduler.sqlite3`, overridable with the `SCHEDULER_DB_PATH` environment
variable. Version zero means nothing is committed. `confirm` allocates the next version, rejects a
stale base version with `VersionConflict`, and writes the appointments and their `add`, `move`, and
`cancel` events in one transaction. Pending proposals stay in Streamlit session state and never
reach the store, so discarding a proposal cannot modify committed state.

## Next milestones

- CP-SAT optimization, repair relative to existing appointments, pinned appointments, and evaluation metrics, owned by the scheduling contributor.
- Calendar views, disruption events, and repair comparison in the application.

See [product design](docs/product.md) and [technical design](docs/technical.md).
