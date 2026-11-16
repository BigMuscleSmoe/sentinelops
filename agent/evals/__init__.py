"""The eval harness (Phase 3, step 2): replay recorded alarms through the real agent, then grade.

    uv run python -m evals plan [--arm sonnet]     # the runs a sweep would make, and its cost bound
    uv run python -m evals sweep --go              # the sweep: real model calls, billed (from agent/)
    uv run python -m evals sweep --go --split test # the held-out alarms, once the pass bar is frozen
    uv run python -m evals packet <sweep> --out <packet.md>   # what a labeler reads, and blank labels
    uv run python -m evals grade <sweep> --audit <labels.json> [--flags <flags.json>]

A scenario (`scenarios`) is one alarm in a recorded window. A sweep (`sweep`)
runs each one through investigation.py on one arm (`arms`: the model and its
caps), with the window answering every AWS read but the sign-in, and keeps every
transcript whole. It doesn't grade. Checking a run is the checker's (`python -m
checker`), and each scenario's rubric (`rubrics`) reads its verdicts. Hand
labels point at an answer's parts by ID (`audit`), from a packet that shows only
what the run saw and answered (`packet`); `score` matches the checker's flags to
a committed audit by ID and holds the result to the committed pass bar
(`pass_bar.json`).

Local tooling, like checker/: the Runtime ships only the agent's top-level
modules, so none of this ships.
"""
