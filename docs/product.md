# Product Design: Clinical Trial Visit Scheduler

## Goal and Users
Help clinical trial coordinators schedule visits within protocol windows, participant availability, and clinic resource limits. After resource outages or rescheduling requests, propose repairs that minimize disruption to existing appointments.

The project follows Regeneron's "Inside the Window" direction. Demonstrations use synthetic data without real patients or hospital integrations.

## MVP Scope
- One trial, one clinic, and 30 synthetic participants with multiple visits each.
- Two resource types: nurses and equipment. Each visit is a fixed-duration, indivisible appointment with explicit resource requirements.
- Inputs: visit rules, participant reference dates and availability, resource working hours, and existing appointments.
- Outputs: a schedule, unscheduled-visit explanations, risk lists, repair comparisons, and exportable appointments.

## Core Workflow
1. Load a built-in scenario and inspect participants, rules, and resources.
2. Generate a schedule; show appointments, unscheduled visits, and visit windows in calendar and list views.
3. Select an appointment to inspect its calculation basis, resource requirements, and status.
4. Introduce a resource outage or participant rescheduling request and identify affected appointments.
5. Generate a repair proposal showing additions, moves, cancellations, and unscheduled visits.
6. Apply the proposal only after coordinator confirmation, preserving a change history. Discarding it leaves the committed schedule unchanged.

## Scheduling Principles
Hard constraints: visits stay within their windows, participants are available, required resources are available without conflicts, appointments fit clinic hours, and declared visit precedence is respected.

Optimize in priority order: maximize scheduled visits, minimize changes to existing appointments, then minimize deviation from target dates.

Explain scheduling conflicts without automatically relaxing protocol rules. Explanations are feasibility diagnostics, not claims of a minimal conflict set.

## Acceptance Criteria
- Complete the generate, outage, repair, confirm, and export workflow.
- Independently validate every committed appointment; no resource overlaps or out-of-window visits.
- Explicitly mark visits with no feasible slot as unscheduled.
- Unconfirmed proposals do not modify committed state; confirmed changes remain traceable.
- Produce reproducible results for identical inputs and configuration.
- Honestly compare scheduled counts, constraint violations, appointment changes, and runtime against a simple greedy baseline.

## Out of Scope
Real hospital integrations, live messages or calls, authentication, multiple centers, clinical judgment, multi-step visits, dropout prediction, and LLM-based scheduling. Skip a separate mock demo and implement the working product directly.

## Competition Deliverables
An open-source GitHub repository (preferably MIT), setup and test instructions, synthetic data and assumptions, evaluation results, an approximately three-minute video, and approximately 10-12 slides. Submit through the official HackRU process.
