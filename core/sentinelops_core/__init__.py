"""What the agent, its tools, and the remediation flow must agree on, in one place so they can't drift.

- models: the answer's schema, and the PR gate (Hypothesis.can_open_pr).
- code: which of checkout-api's files may be read or patched, and how a read looks.
- patches: how a proposed fix is checked and turned into a diff, without running it.

Never copy a file out of here: two copies of the model are two copies of the rule.
"""
