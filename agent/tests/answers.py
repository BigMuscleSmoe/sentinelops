"""Answers shaped as the model sends them now: typed evidence items, a fault location, and a patch."""

from pathlib import Path
from typing import Any

from sentinelops_core import code, patches


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

# checkout-api's files as the read tool will show them. checkout/ is 1.42.0;
# 1.43.0 is the same code without apply_promo's null check (the service's
# sim/releases.py builds it that way at runtime). Read as text, so a CRLF
# checkout reads as LF, as the snapshot will.
CHECKOUT = Path(__file__).parents[2] / "services" / "checkout-api" / "checkout"
PRICING_1_42_0 = (CHECKOUT / "pricing.py").read_text(encoding="utf-8")
NULL_CHECK = "    if promo is None:\n        return subtotal_cents\n"
PRICING_1_43_0 = PRICING_1_42_0.replace(NULL_CHECK, "", 1)
CONFIG_1_42_0 = (CHECKOUT / "config.py").read_text(encoding="utf-8")

# A right patch for the bad deploy, as an answer writes it: against 1.43.0's
# checkout/pricing.py, read as c1, putting back the null check apply_promo lost.
PATCH_OLD = "def apply_promo(subtotal_cents: int, promo: Mapping[str, Any] | None) -> int:\n    return "
PATCH = {
    "source": "c1",
    "file_path": "checkout/pricing.py",
    "edits": [{"old": PATCH_OLD, "new": PATCH_OLD.replace("    return ", NULL_CHECK + "    return ")}],
    "rationale": "1.43.0 subscripts promo even when a checkout has none; returning the subtotal unchanged then, as 1.42.0 did, ends the TypeError.",
}


def checked(patch: dict[str, Any], text: str, version: str) -> dict[str, Any]:
    """The patch as the run's check keeps it against text: what a committed answer holds."""
    result = patches.apply_edits(patch["file_path"], text, [(e["old"], e["new"]) for e in patch["edits"]])
    return {**patch, "version": version, "file_sha256": code.digest(text), "diff": result.diff}


CHECKED_PATCH = checked(PATCH, PRICING_1_43_0, "1.43.0")
