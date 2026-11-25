"""A bad deploy's answer as the runner leaves it: a kept patch on 1.43.0's checkout/pricing.py, running at the alarm.

The file is the one GitHub holds on release/1.43.0 (the recorded contents
answer), so the patch's file_sha256 is of those exact bytes. The fix puts back
the null check 1.43.0 dropped, which makes the file 1.42.0's again.
"""

import base64
from pathlib import Path
from typing import Any

from fake_github import recorded
from sentinelops_core import code, patches
from sentinelops_core.models import Hypothesis

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_1_43_0 = base64.b64decode(recorded("get_file-pricing-at-release-1.43.0")["json"]["content"]).decode("utf-8")
# Read as text, so a CRLF checkout reads as LF, as GitHub holds it.
PRICING_1_42_0 = (REPO_ROOT / "services" / "checkout-api" / "checkout" / "pricing.py").read_text(encoding="utf-8")
NULL_CHECK = "    if promo is None:\n        return subtotal_cents\n"
PATCH_OLD = "def apply_promo(subtotal_cents: int, promo: Mapping[str, Any] | None) -> int:\n    return "
EDITS = [{"old": PATCH_OLD, "new": PATCH_OLD.replace("    return ", NULL_CHECK + "    return ")}]


def kept(edits: list[dict[str, str]] = EDITS, text: str = PRICING_1_43_0, version: str = "1.43.0") -> dict[str, Any]:
    """The patch as the run's check keeps it against text."""
    checked = patches.apply_edits("checkout/pricing.py", text, [(e["old"], e["new"]) for e in edits])
    return {
        "source": "c1",
        "file_path": "checkout/pricing.py",
        "edits": edits,
        "rationale": "1.43.0 subscripts promo even when a checkout has none; returning the subtotal then, as 1.42.0 did, ends the TypeError.",
        "version": version,
        "file_sha256": code.digest(text),
        "diff": checked.diff,
    }


ANSWER = {
    "root_cause": "The 1.43.0 deploy removed apply_promo's check for a missing promo, so checkouts without one raise TypeError.",
    "confidence": 0.86,
    "evidence": [
        {"kind": "value", "source": "m1", "column": "err%", "at": "19:05:00", "value": "10.1", "note": "errors in the alarm's minute"},
        {"kind": "value", "source": "l1", "column": "error_type", "lines": [1], "value": "TypeError", "note": "raised in apply_promo"},
    ],
    "affected_component": "checkout-api",
    "fault_location": "this_service",
    "remediation": kept(),
    "running_version": "1.43.0",
}


def hypothesis(**changes: Any) -> Hypothesis:
    return Hypothesis.model_validate({**ANSWER, **changes})
