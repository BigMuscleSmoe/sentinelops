"""Answers shaped as the model sends them now: typed evidence items and a fault location."""

from typing import Any


def item(kind: str, note: str = "seen in the result", **fields: Any) -> dict[str, Any]:
    """One typed evidence item."""
    return {"kind": kind, **fields, "note": note}


# What a bad-deploy run cites: the alarm's minute, the failing line, the deploy.
DEPLOY_EVIDENCE = [
    item("value", source="m1", column="err%", at="19:05:00", value="10.1", note="errors in the alarm's minute"),
    item("value", source="l1", column="error_type", lines=[1], value="TypeError", note="raised in apply_promo"),
]
UPSTREAM_EVIDENCE = [
    item("value", source="m1", column="payments_p99_ms", at="14:03:00", value="4800", note="payments-api slow"),
]
