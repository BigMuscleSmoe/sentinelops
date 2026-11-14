"""Check a run's typed evidence against the tool results it cites, in code: no model, no AWS.

The grader's successor (BUILD_PLAN.md, "The grader redesign"). Step 2 builds
the first part: `sources` reads a run's labeled tool results back into data,
and `resolve` finds what each evidence item points at, or says why it can't.
The checks per kind, which say whether an item holds, are step 4.

Local tooling and the eval harness's, like `grader`: the Runtime package ships
only the agent's top-level modules, so this never ships.
"""
