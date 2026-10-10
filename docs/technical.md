# Technical Design: Clinical Trial Visit Scheduler

## Technology Stack
Python 3.11+, Streamlit, Pandas, OR-Tools CP-SAT, and pytest; local JSON/CSV and SQLite. Use a monolithic application to reduce deployment and integration overhead. No LLM or paid service dependency. Pin the dependency versions actually installed.

## Modules
- `app.py`: inputs, calendar/list views, disruption events, proposal comparisons, and confirmation.
- `src/models.py`: data models and input validation.
- `src/scenarios.py`: synthetic scenario specifications expanded into `Scenario` objects.
- `src/windows.py`: visit windows calculated from reference dates.
- `src/scheduler.py`: greedy baseline, CP-SAT scheduling, and repair.
- `src/validator.py`: independent constraint checks without reusing solver constraint construction.
- `src/storage.py`: SQLite state, schedule versions, and audit events.
- `src/export.py`: appointment and unscheduled-visit tables and CSV export.
- `src/metrics.py`: evaluation. Place tests in `tests/` and synthetic scenarios in `data/`.

## Data Conventions
- Study/VisitRule: rule version, reference-date type, target day offset, early/late tolerance, duration, resource requirements, and precedence.
- Participant: synthetic ID, reference date, and availability intervals.
- Resource: ID, nurse/equipment type, working hours, and outage intervals.
- Appointment: participant, visit, start/end times, assigned resources, and schedule version.
- ChangeEvent: event type, affected scope, old/new appointments, confirmation time, and reason.
- Scenario: complete inputs, configuration, random seed, and result metrics.

Use clinic time in America/New_York, 30-minute slots, and durations that are integer multiples of a slot. Reference-date offset zero means that same day. Date windows include both endpoints; appointments must fit entirely within allowed dates and working hours. Time intervals use [start, end), allowing adjacent appointments. Reject ambiguous or nonexistent local times. Keep sample scenarios away from daylight-saving transitions and test date boundaries.

## Scheduling and Repair
1. Validate inputs, calculate date windows, and enumerate candidate slots and resource combinations satisfying personal availability and resource opening rules.
2. Create Boolean candidate variables. Select at most one per visit, prevent participant/resource overlaps, and enforce declared precedence.
3. Optimize in stages: maximize scheduled visits, minimize existing appointment changes, then minimize target-date deviation. Fix a previous stage's objective only after proving it optimal.
4. Set a total solver budget, initially five seconds. On timeout, retain a valid proposal and record the stage, status, runtime, and bounds; do not label a feasible solution optimal. Fix the seed, use one solver worker, and sort inputs consistently. Wall-clock time limits do not guarantee optimality or identical results.
5. Repair relative to the committed schedule. Prefer preserving feasible appointments without locking all of them; allow necessary cascading changes. Support explicitly pinned appointments.
6. For unscheduled visits, distinguish no candidate slots from competition for shared resources or precedence constraints. Provide candidate statistics without inventing precise causal explanations.
7. Independently validate proposals before review. On confirmation, verify the base version is unchanged and atomically save the new schedule and audit events.

## State and Interface
SQLite is the source of committed state; Streamlit session state holds only pending proposals and interface state. Export appointment CSV files and scenario/difference JSON. Include baseline, equipment-outage, and infeasible scenarios. Confirmed state survives page refreshes.

## Validation and Evaluation
- Unit tests: window boundaries, month/year transitions, leap years, invalid inputs, resource outages, and precedence.
- Integration tests: insufficient capacity, rescheduling, infeasibility, pinned appointments, discarded proposals, confirmation/version conflicts, and reload persistence.
- Independently validate every result; use exhaustive search on tiny cases to verify optimization objectives.
- Compare the greedy baseline and solver on identical inputs and hard constraints. Include normal, constrained-capacity, and sudden-outage scenarios. Record environment, dataset size, seed, commands, scheduled counts, violations, changes, target-date deviation, and runtime.
- In addition to core logic tests, launch the application and verify the complete interaction flow, followed by user acceptance testing.

## Implementation Order
Data and date rules, working greedy workflow, CP-SAT and independent validation, disruption repair/comparison/confirmation, tests and evaluation, then documentation and competition materials. Commit each complete, verified change to Git; add relevant tests for behavioral changes.

## Limitations
Simulated capacity and availability do not represent full clinic operations. Unscheduled explanations are not formal unsatisfiable cores. Report performance and optimization gains only as measured. If time becomes limited, preserve a reliable greedy workflow and conflict explanations, and clearly document the optimizer's completion status.
