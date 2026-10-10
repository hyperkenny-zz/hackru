"""Run reproducible-input baseline comparisons: python -m src.evaluate."""
import argparse
from dataclasses import asdict, replace
import json
import platform
from pathlib import Path
from time import perf_counter
import ortools
from src.metrics import schedule_metrics
from src.optimizer import optimize_schedule
from src.scenarios import load_scenario
from src.scheduler import greedy_schedule


def evaluate(time_limit_seconds=5.0):
    rows = []
    for name in ("small_feasible", "baseline_30", "tight_capacity", "equipment_outage"):
        scenario = load_scenario(name)
        existing = ()
        if name == "equipment_outage":
            uninterrupted = replace(scenario, resources=tuple(replace(r, outages=()) for r in scenario.resources))
            existing = greedy_schedule(uninterrupted).appointments
        started = perf_counter()
        baseline = greedy_schedule(scenario)
        greedy_runtime = perf_counter() - started
        optimized = optimize_schedule(scenario, existing, time_limit_seconds=time_limit_seconds)
        rows.append({"scenario": name, "participants": len(scenario.participants),
                     "visits": len(scenario.participants) * len(scenario.rules), "seed": scenario.seed,
                     "existing_appointments": len(existing),
                     "greedy": schedule_metrics(scenario, baseline, existing) | {"runtime_seconds": greedy_runtime},
                     "cp_sat": (schedule_metrics(scenario, optimized.result, existing) if optimized.result else {}) |
                     {"status": optimized.status, "optimal": optimized.optimal,
                      "runtime_seconds": optimized.runtime_seconds, "candidate_count": optimized.candidate_count,
                      "stages": [asdict(stage) for stage in optimized.stages]}})
    return {"environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "ortools": ortools.__version__, "solver_workers": 1},
            "command": f"python -m src.evaluate --time-limit {time_limit_seconds}",
            "search_budget_seconds": time_limit_seconds,
            "notes": "Search budget excludes preprocessing; total runtimes include it. Wall-clock timeouts may vary. "
                     "Outage comparison uses the same pre-outage greedy schedule as the reference for both algorithms.",
            "scenarios": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time-limit", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(evaluate(args.time_limit), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
