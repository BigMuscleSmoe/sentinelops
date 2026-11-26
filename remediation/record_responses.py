"""Record the GitHub answers the client's tests are built on.

    uv run python record_responses.py           (from remediation/)
    uv run python record_responses.py --app     (the App's own, on the user's go)

The default records public reads only, sent without any token (the repository
is public): the release branch's head, checkout/pricing.py at that commit, and
the rules on main, release/1.43.0, and the fix branch.

--app records, as the App (its JWT signed with kms:Sign, as the sentinelops
profile; the IDs from .env.github-app, gitignored): GET /app, its
installations, an installation token's grant, the installation's repositories,
the repository, the checkout-api test job's check run on main's head, and the
fix branch's PRs. The token is revoked at the end, whatever happened. Nothing
is ever printed from an answer.

Every URL is built by the client's own github.url(), so a fixture can only be
an endpoint the client may call. Before saving, every ID is replaced: ruleset
IDs become 1, 2, 3; node IDs a fixed stand-in; the App's client ID and
installation ID the tests' own; every other number under an `id` or `*_id` key
(and the same number inside a string, a URL), and any other run of 6 or more
digits inside a string, a small stand-in by first appearance. The token, and
anything the hooks' email rule flags (the repository's ssh_url), become
stand-ins. A file still holding a real ID, anything token-shaped, or anything
the commit hooks' scan finds is refused, and then nothing is saved.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The commit hooks' scanner, the repo's one copy, as check_app.py finds it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".githooks"))

import commit_scan
from sentinelops_remediation import github

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures" / "github"
IDS_FILE = HERE / ".env.github-app"
BRANCH = "release/1.43.0"
FILE = "services/checkout-api/checkout/pricing.py"
RULES_ON = ("main", BRANCH, "sentinelops/fix-1.43.0-pricing")
FIX_BRANCH = "sentinelops/fix-1.43.0-pricing"
CHECK = "test (services/checkout-api)"  # the one job the flow waits on
TEST_CLIENT_ID = "Iv23liTestClientId"  # tests/fake_github.py's
TEST_INSTALLATION_ID = 4242
TEST_APP_ID = 101
TOKEN_STAND_IN = "TOKEN_REPLACED"
EMAIL_STAND_IN = "EMAIL_REPLACED"
TOKEN_SHAPED = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
DIGITS = re.compile(r"(?<![0-9A-Za-z])[0-9]+(?![0-9A-Za-z])")


def fetch(name: str, params: dict[str, object], query: dict[str, object] | None = None) -> dict[str, object]:
    headers, _ = github.encoded(None)  # no Authorization header: no token to record
    response = github.urllib_transport(github.Request("GET", github.url(name, params, query), headers, None))
    if response.status != 200:
        raise SystemExit(f"{name}: HTTP {response.status}")
    return {"endpoint": name, "params": params, "query": query or {}, "status": 200, "json": json.loads(response.body)}


class Scrubber:
    """One mapping of real IDs to stand-ins, shared by every file of a recording.

    Public answers (ids=False) get only ruleset and node IDs replaced. The App's
    (ids=True) get every ID: collect() first, so an ID met inside a URL before
    its own key still maps to the same stand-in.
    """

    def __init__(self, ids: bool, fixed: dict[int | str, int | str] | None = None) -> None:
        self.all_ids = ids
        self.ruleset_ids: dict[int, int] = {}
        self.ids: dict[int | str, int | str] = dict(fixed or {})
        self.next_id = 1

    def collect(self, value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if _id_key(k) and type(v) is int:
                    self._stand_in(v)
                self.collect(v, k)
        elif isinstance(value, list):
            for item in value:
                self.collect(item, key)

    def _stand_in(self, real: int) -> int | str:
        if real not in self.ids:
            self.ids[real] = self.next_id
            self.next_id += 1
        return self.ids[real]

    def _text(self, text: str) -> str:
        for real, stand_in in self.ids.items():
            if isinstance(real, str):
                text = text.replace(real, str(stand_in))

        def digits(match: re.Match[str]) -> str:
            real = int(match.group())
            if real in self.ids and len(match.group()) >= 4:
                return str(self.ids[real])
            return str(self._stand_in(real)) if len(match.group()) >= 6 else match.group()

        text = commit_scan.EMAIL.sub(_email, text)
        return DIGITS.sub(digits, text)

    def walk(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._item(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        if isinstance(value, str) and self.all_ids:
            return self._text(value)
        return value

    def _item(self, key: str, value: Any) -> Any:
        if key == "ruleset_id" and type(value) is int:
            return self.ruleset_ids.setdefault(value, len(self.ruleset_ids) + 1)
        if key == "node_id":
            return "NODE_ID"
        if self.all_ids and key == "token" and isinstance(value, str):
            return TOKEN_STAND_IN
        if self.all_ids and _id_key(key) and type(value) is int:
            return self._stand_in(value)
        return self.walk(value)

    def exchange(self, exchange: dict[str, Any]) -> dict[str, Any]:
        out = dict(exchange)
        for part in ("params", "query", "json", "request_json"):
            if part in out:
                out[part] = self.walk(out[part])
        return out

    def leftovers(self, text: str, account: str | None = None) -> list[str]:
        """What a saved file must not hold: a real ID (4+ digits, or the client ID), a token, a hooks' finding."""
        found = sorted({f"the hooks' {f.rule} rule" for f in commit_scan.scan_text("fixture", text, account)})
        for n, real in enumerate(self.ids, 1):
            if isinstance(real, int) and real < 1000:
                continue  # too short to tell from any other small number; replaced wherever it's an ID's value
            if re.search(rf"(?<![0-9A-Za-z]){re.escape(str(real))}(?![0-9A-Za-z])", text):
                found.append(f"real ID #{n}")
        if TOKEN_SHAPED.search(text):
            found.append("token-shaped text")
        return found


def _email(match: re.Match[str]) -> str:
    """An address the hooks would flag becomes a stand-in; an example or noreply one stays."""
    if commit_scan.EXAMPLE_DOMAINS.search(match.group(1)) or commit_scan.NOREPLY.search(match.group()):
        return match.group()
    return EMAIL_STAND_IN


def _id_key(key: str) -> bool:
    return key == "id" or key.endswith("_id")


def save(exchanges: dict[str, dict[str, Any]], about: str, scrubber: Scrubber, account: str | None = None) -> None:
    """Scrub, check every file, then write them all, or none. `account`: the hooks' account ID (git config)."""
    for exchange in exchanges.values():
        for part in ("params", "query", "json", "request_json"):
            if scrubber.all_ids and part in exchange:
                scrubber.collect(exchange[part])
    texts = {
        slug: json.dumps({"about": about, **scrubber.exchange(ex)}, indent=2) + "\n"
        for slug, ex in exchanges.items()
    }
    for slug, text in texts.items():  # all checked before any is written
        if problems := scrubber.leftovers(text, account):
            raise SystemExit(f"{slug}: {', '.join(problems)}; nothing saved")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for slug, text in texts.items():
        (FIXTURES / f"{slug}.json").write_text(text, encoding="utf-8", newline="\n")
        print(f"saved {slug}.json")


def public() -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    head = fetch("get_ref", {"branch": BRANCH})
    sha = head["json"]["object"]["sha"]  # type: ignore[index]
    exchanges = {
        "get_ref-release-1.43.0": head,
        "get_file-pricing-at-release-1.43.0": fetch("get_file", {"path": FILE}, {"ref": sha}),
        **{f"branch_rules-{re.sub(r'[^a-z0-9.]+', '-', b)}": fetch("branch_rules", {"branch": b}) for b in RULES_ON},
    }
    about = f"Recorded {stamp} without a token (public repository); ruleset and node IDs replaced."
    save(exchanges, about, Scrubber(ids=False), commit_scan.account_id())


def _ids() -> tuple[int, str, int]:
    values = {}
    for line in IDS_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return (
        int(values["SENTINELOPS_GITHUB_APP_ID"]),
        values["SENTINELOPS_GITHUB_CLIENT_ID"],
        int(values["SENTINELOPS_GITHUB_INSTALLATION_ID"]),
    )


def as_the_app() -> None:
    import boto3  # here: the public recording needs no AWS

    app_id, client_id, installation_id = _ids()
    kms = boto3.Session(profile_name="sentinelops", region_name="us-east-1").client("kms")
    seen: list[tuple[github.Request, github.Response]] = []

    def recording(request: github.Request) -> github.Response:
        response = github.urllib_transport(request)
        seen.append((request, response))
        return response

    app = github.GitHubApp(client_id, installation_id, kms, transport=recording)
    try:
        main_sha = app.head_commit("main")
        planned: list[tuple[str, str, dict[str, object], dict[str, object]]] = [
            ("app", "app", {}, {}),
            ("app_installations", "app_installations", {}, {"per_page": 100}),
            ("installation_repositories", "installation_repositories", {}, {"per_page": 100}),
            ("repository", "repository", {}, {}),
            ("check_runs-main-checkout-api", "check_runs", {"sha": main_sha}, {"check_name": CHECK, "per_page": 100}),
            (
                "list_pulls-fix-1.43.0-pricing",
                "list_pulls",
                {},
                {"head": f"{github.OWNER}:{FIX_BRANCH}", "base": BRANCH, "state": "all"},
            ),
        ]
        for _, name, params, query in planned:
            app.call(name, params, query)
    finally:
        app.revoke()
    labels = {("GET", github.url(n, p, q)): (slug, n, p, q) for slug, n, p, q in planned}
    labels[("POST", github.url("installation_token", {"installation_id": installation_id}))] = (
        "installation_token", "installation_token", {"installation_id": installation_id}, {}
    )
    exchanges: dict[str, dict[str, Any]] = {}
    for request, response in seen:
        if (request.method, request.url) not in labels:
            continue  # main's head (already recorded publicly) and the revoke (a 204, no body)
        slug, name, params, query = labels[(request.method, request.url)]
        exchange: dict[str, Any] = {"endpoint": name, "params": params, "query": query, "status": response.status}
        if request.method != "GET":
            exchange["method"] = request.method
            exchange["request_json"] = json.loads(request.body or b"null")
        exchange["json"] = json.loads(response.body)
        exchanges[slug] = exchange
    missing = {slug for slug, *_ in labels.values()} - set(exchanges)
    if missing:
        raise SystemExit(f"not recorded: {sorted(missing)}; nothing saved")
    scrubber = Scrubber(True, {app_id: TEST_APP_ID, client_id: TEST_CLIENT_ID, installation_id: TEST_INSTALLATION_ID})
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    about = (
        f"Recorded {stamp} as the App (KMS-signed JWT, one installation token, revoked after); "
        "the token and every ID replaced (record_responses.py)."
    )
    save(exchanges, about, scrubber, commit_scan.account_id())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", action="store_true", help="record the App's own answers (a KMS signature, a token)")
    args = parser.parse_args(argv)
    as_the_app() if args.app else public()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
