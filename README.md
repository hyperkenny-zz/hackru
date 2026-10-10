# Clinical Trial Visit Scheduler

A local clinical trial visit scheduler using synthetic data. Includes shared domain models, inclusive date windows, a greedy baseline, CP-SAT optimization and repair, and an independent appointment validator.

## Development

Python 3.11 or newer is required. Install the application and OR-Tools dependencies pinned in `requirements.txt`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

The original greedy core tests can also run without installing dependencies:

```sh
python3 -m unittest discover -s tests -p 'test_core.py' -v
```

## Running the application

```sh
.venv/bin/python -m streamlit run app.py
```

Pick a scenario in the sidebar, choose the greedy baseline or the CP-SAT optimizer, then choose
**Generate schedule**. The schedule tab shows the solver status, total runtime, whether every
objective is proven optimal, the staged objectives, the proposed appointments, the unscheduled
visits with their diagnostic category, and the independent validator's verdict, and it exports the
appointment and unscheduled tables as CSV. A proposal is confirmed with an optional change note;
discarding it leaves the committed schedule untouched. Confirmation is blocked whenever the engine
returns no schedule, which covers `INFEASIBLE` and an unknown search with no validated incumbent.

The CP-SAT budget applies to the solver searches only. Candidate enumeration and model construction
are outside it, so the reported total runtime can exceed the budget; the page reports measured
runtime rather than promising a deadline.

The repair tab records two kinds of disruption: a resource outage over an explicit interval, and a
participant rescheduling request that replaces one clinic day's availability while keeping that
participant's other days. Recorded inputs build a draft scenario; they stay in the session, and
adding one discards any repair proposal generated from the earlier inputs. A repair starts from the
committed schedule, optionally with pinned appointments that must keep their exact time and
resources, and reports the solver status, how many validation problems the committed schedule has
under the draft scenario, the added, moved, and cancelled counts, the change table, the visits left
unscheduled, and the independent validator's verdict against the draft scenario. Pins are never
released automatically: if they make the repair infeasible, the page says to adjust the disruption
inputs or unpin those appointments. Confirmation uses the proposal's base version, so a schedule
committed meanwhile is reported as a conflict instead of being overwritten.

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

## Optimization and repair

The Streamlit Generate button runs either engine. The following engine APIs do not modify the
database or Streamlit state.

```python
from src.optimizer import optimize_schedule
from src.repair import repair_schedule, with_resource_outage

report = optimize_schedule(scenario, time_limit_seconds=5.0)
if report.result is not None:
    appointments = report.result.appointments

# saved is a CommittedSchedule loaded from ScheduleStore.
disrupted = with_resource_outage(scenario, resource_id, outage_interval)
proposal = repair_schedule(disrupted, saved.appointments, saved.version,
                          pinned=(), time_limit_seconds=5.0)
if proposal.optimization.result is not None:
    # Render proposal.changes for review before explicit confirmation.
    repaired = proposal.optimization.result
```

`with_participant_availability` similarly replaces one participant's availability for a rescheduling
request. Disruption helpers return new scenarios without mutating the original. A proposal carries
`base_version` for the existing atomic `ScheduleStore.confirm` version check. The application must
retain the disrupted scenario with the proposal, validate against that scenario before confirmation,
and persist disruption inputs if it needs to restore them after a reload. Current SQLite storage
persists schedules and audit history, but does not yet persist dynamic scenario changes.

Pins are `(participant_id, visit_id)` keys referring to existing appointments and preserve their
time and resources exactly. `existing` may include placements invalidated by new outages. A repair
requires every existing appointment's `schedule_version` to equal the supplied base version.

The optimizer maximizes scheduled visits, minimizes changes to existing placements (moves and
cancellations, counting each existing visit once), then minimizes summed absolute target-date
deviation in days. New appointments do not count as existing changes. Only proven objectives are
fixed before proceeding. Constant objectives are proved directly without another solver search.

`report.stages` exposes stage status, objective, bound, and runtime. `report.optimal` is true only
when all three objectives are proven optimal. A valid fallback or previous-stage incumbent is retained
on timeout, with status `FEASIBLE`. If pins make the model infeasible, status is `INFEASIBLE` and
`result` is `None`; an unknown search with no validated incumbent also returns `None`. Disable
confirmation whenever `result` is `None`. No protocol constraint or pin is silently relaxed.

The five-second budget is shared across CP-SAT searches. Candidate enumeration, model construction,
and fallback construction are outside that search budget and included in reported total runtime.
Sorted inputs, a fixed scenario seed, and one worker improve repeatability; wall-clock limits do not
guarantee identical results. Solver status meanings follow the
[official OR-Tools documentation](https://developers.google.com/optimization/cp/cp_solver).

## Evaluation

```sh
.venv/bin/python -m src.evaluate --time-limit 5 --output docs/evaluation.json
```

[Evaluation notes](docs/evaluation.md) summarize the committed machine-readable measurements and
limitations. Tests additionally enumerate all assignments on tiny instances to verify the three
objective priorities, and exercise outages, availability changes, cascading moves, pins, timeouts,
and confirmation/version/persistence integration.

## Next milestones

- Calendar views, disruption events, and repair comparison in the application.

See [product design](docs/product.md) and [technical design](docs/technical.md).
