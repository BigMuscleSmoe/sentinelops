"""The eval harness (Phase 3, step 2): replay recorded alarms through the real agent, then grade.

    uv run python -m evals plan                    # the runs a sweep would make, and its cost bound
    uv run python -m evals sweep --go              # the sweep: real model calls, billed (from agent/)
    uv run python -m evals grade <sweep> --audit <labels.json>

A scenario (`scenarios`) is one alarm in a recorded window. A sweep (`sweep`)
runs each one through investigation.py, with the window answering every AWS
read but the sign-in, and keeps every transcript whole. It doesn't grade.
Grading (`score`) runs the grader frozen at its commit and each scenario's
rubric (`rubrics`), and scores the grader against a hand audit committed first.

Local tooling, like grader/: the Runtime ships only the agent's top-level
modules, so none of this ships.
"""
