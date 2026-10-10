# Scheduling Evaluation

Measured locally using Python 3.12.10 and OR-Tools 9.15.6755. Full platform, seeds, candidate counts, stage objectives, bounds, and timings are in [evaluation.json](evaluation.json).

Command: `.venv/bin/python -m src.evaluate --time-limit 5 --output docs/evaluation.json`.

| Scenario | Visits | Greedy scheduled | CP-SAT scheduled | Greedy changes | CP-SAT changes | Greedy deviation days | CP-SAT deviation days | Greedy seconds | CP-SAT total seconds | CP-SAT status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| small_feasible | 9 | 9 | 9 | 0 | 0 | 2 | 2 | 0.012 | 0.095 | OPTIMAL |
| baseline_30 | 90 | 90 | 90 | 0 | 0 | 25 | 25 | 0.433 | 5.802 | FEASIBLE |
| tight_capacity | 90 | 37 | 47 | 0 | 0 | 36 | 44 | 0.242 | 5.360 | FEASIBLE |
| equipment_outage | 90 | 68 | 78 | 51 | 32 | 35 | 45 | 1.097 | 6.375 | FEASIBLE |

All schedules above passed independent validation with zero violations. Normal and constrained-capacity comparisons use identical inputs and no existing schedule. The outage comparison first creates a common pre-outage greedy schedule with outages removed, then evaluates both methods against the same disrupted scenario and that reference schedule.

Existing changes count moves and cancellations, once per existing appointment; additions are excluded. Target deviation is summed over scheduled visits, so higher counts can also increase total deviation. Count takes priority over changes and deviation.

The small case proves all objectives optimal. In the outage case, scheduled count and existing changes were proven optimal, while target deviation remained feasible. The other large cases did not prove all stages optimal within the search budget. Check individual stage bounds before making stronger claims.

The five-second limit applies to the combined CP-SAT searches. Total runtime also includes input enumeration, model building, fallback construction, and independent validation, so it can exceed five seconds. Timings and timeout results can vary across runs and hardware; this is one measured run, not a statistical benchmark.

A tiny regression case additionally demonstrates the greedy limitation: it schedules a flexible participant before a participant with one available slot, producing one appointment; CP-SAT schedules both. Exhaustive enumeration verifies count, preservation, and target-deviation priorities on tiny cases.

## Application handoff

The existing Streamlit interface still calls the greedy baseline. Connect `optimize_schedule` for initial optimization and `repair_schedule` for repairs. Review the returned changes, retain the disrupted scenario, check `result is not None`, validate it, and only then use the existing version-checked confirmation flow. Dynamic disruption inputs still need application persistence. See the API examples in [README](../README.md).
