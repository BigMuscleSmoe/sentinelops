"""Record the GitHub reads the validation step makes, for its offline tests.

    uv run python record_responses.py        (from remediation/)

Public reads only, sent without any token (the repository is public), so no
token can be recorded: the release branch's head, checkout/pricing.py at that
commit, and the rules on main, release/1.43.0, and the fix branch. Each URL is
built by the client's own github.url(), so a fixture can only be an endpoint
the client may call. Ruleset and node IDs are replaced; nothing else is
changed. The App's own endpoints (its JWT, installation tokens) can't be
recorded until it's installed (evening 4, step 11); their test answers are
written from GitHub's REST docs and say so.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from sentinelops_remediation import github

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "github"
BRANCH = "release/1.43.0"
FILE = "services/checkout-api/checkout/pricing.py"
RULES_ON = ("main", BRANCH, "sentinelops/fix-1.43.0-pricing")


def fetch(name: str, params: dict[str, object], query: dict[str, object] | None = None) -> dict[str, object]:
    headers, _ = github.encoded(None)  # no Authorization header: no token to record
    response = github.urllib_transport(github.Request("GET", github.url(name, params, query), headers, None))
    if response.status != 200:
        raise SystemExit(f"{name}: HTTP {response.status}")
    return {"endpoint": name, "params": params, "query": query or {}, "status": 200, "json": json.loads(response.body)}


def scrub(exchange: dict[str, object], ruleset_ids: dict[int, int]) -> dict[str, object]:
    """Ruleset IDs become 1, 2, 3 by first appearance; node IDs a fixed stand-in."""

    def walk(value: object) -> object:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "ruleset_id" and isinstance(item, int):
                    out[key] = ruleset_ids.setdefault(item, len(ruleset_ids) + 1)
                elif key == "node_id":
                    out[key] = "NODE_ID"
                else:
                    out[key] = walk(item)
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return {**exchange, "json": walk(exchange["json"])}


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    about = f"Recorded {stamp} without a token (public repository); ruleset and node IDs replaced."
    ruleset_ids: dict[int, int] = {}
    head = fetch("get_ref", {"branch": BRANCH})
    sha = head["json"]["object"]["sha"]  # type: ignore[index]
    exchanges = {
        "get_ref-release-1.43.0": head,
        "get_file-pricing-at-release-1.43.0": fetch("get_file", {"path": FILE}, {"ref": sha}),
        **{f"branch_rules-{re.sub(r'[^a-z0-9.]+', '-', b)}": fetch("branch_rules", {"branch": b}) for b in RULES_ON},
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for slug, exchange in exchanges.items():
        text = json.dumps({"about": about, **scrub(exchange, ruleset_ids)}, indent=2) + "\n"
        if re.search(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9A-Za-z]|\.[0-9])", text):  # the hooks' 12-digit rule
            raise SystemExit(f"{slug}: holds a 12-digit number; not saved")
        (FIXTURES / f"{slug}.json").write_text(text, encoding="utf-8", newline="\n")
        print(f"saved {slug}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
