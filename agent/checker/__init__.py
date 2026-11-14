"""Check a run's typed evidence against the tool results it cites, in code: no model, no AWS.

The prose grader's successor (BUILD_PLAN.md, "The grader redesign"):
`sources` reads a run's labeled tool results back into data, `resolve` finds
what each evidence item points at or says why it can't, and `checks` says
whether it holds there, one check per kind. `figures` counts the figures in
root_cause no item states; `plant` corrupts items that hold, to measure the
checker's recall; `trial` holds the typed-evidence trial's marks.

Local tooling and the eval harness's: the Runtime package ships only the
agent's top-level modules, so this never ships.
"""
