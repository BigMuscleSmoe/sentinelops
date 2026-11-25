"""The remediation flow: from an answered incident to a draft fix PR, with no model calls.

- github: the App's identity (its JWT signed in KMS) and a fixed list of endpoints with no merge.
- validate: everything a fix must pass before anything is written to GitHub.

BUILD_PLAN.md, Phase 4 design. The flow's handler and its durable steps come in
evening 5; the PR gate stays Hypothesis.can_open_pr, imported from
sentinelops_core like everything the agent and the flow must agree on.
"""
