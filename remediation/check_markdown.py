"""Render the hostile corpus's PR bodies once with GitHub's own Markdown renderer, and check nothing in them acts.

    uv run python check_markdown.py        # from remediation/; one unauthenticated POST /markdown

The offline tests (tests/test_body.py) check the body's text; this checks
what GitHub makes of it (decision 6). One request: two PR bodies built by
body.py from answers carrying every corpus string (tests/hostile.py) across
every free field, then a control section of raw, unsanitized text. Rendered
in gfm mode with the context octocat/Hello-World, whose low issue numbers
exist, so a reference that got through would become a link. The checks, in
the bodies: no link but GitHub's own heading permalinks, no image, no user
mention or issue link, no details, script, or iframe, and exactly six code
blocks a body. In the control: a mention, an image, and a link do render, so
the check isn't passing on a renderer that links nothing.

No token, so it reads nothing of this repo and writes nothing; the text is
synthetic. Prints counts and verdicts only, never the HTML.
"""

import json
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "tests"), str(HERE.parent / ".githooks")]

from answers import hypothesis  # noqa: E402
from hostile import AT, CORPUS  # noqa: E402

from sentinelops_remediation import body  # noqa: E402
from sentinelops_remediation.body import Incident  # noqa: E402
from sentinelops_remediation.validate import Validated  # noqa: E402

ENDPOINT = "https://api.github.com/markdown"
CONTEXT = "octocat/Hello-World"
SPLIT = "CONTROL-SECTION-FOLLOWS"
INCIDENT = Incident("checkout-api-20261125T043000Z", "sentinelops-checkout-errors", "2026-11-25T04:30:00.123Z")
FIX = Validated(
    "1.43.0",
    "release/1.43.0",
    "ee9ef23a8d7eb51670b5c00e655a9d1814e0a640",
    "services/checkout-api/checkout/pricing.py",
    "",
    "",
    "sentinelops/fix-1.43.0-pricing",
)
CONTROL = f"{AT}octocat and #1 and ![pixel](https://example.com/p.png) and [link](https://example.com/x)"


def filled(strings: list[str]) -> object:
    """The recorded answer with the strings in its free fields, in order, as many as the body shows."""
    take = iter(strings)

    def next_text() -> str:
        return next(take, "plain")

    plain = hypothesis()
    item = plain.evidence[0]
    evidence = [
        item.model_copy(update={"note": next_text(), "value": next_text(), "column": next_text()})
        for _ in range(body.EVIDENCE_SHOWN)
    ]
    claim = plain.model_validate({**plain.model_dump(), "inferences": [{"claim": "x", "rests_on": ["e1"]}]}).inferences[0]
    inferences = [claim.model_copy(update={"claim": next_text()}) for _ in range(body.INFERENCES_SHOWN)]
    return plain.model_copy(
        update={
            "root_cause": next_text(),
            "affected_component": next_text(),
            "runbook_cited": next_text(),
            "evidence": evidence,
            "inferences": inferences,
            "not_checked": [next_text() for _ in range(body.NOT_CHECKED_SHOWN)],
            "remediation": plain.remediation.model_copy(update={"rationale": next_text()}),
        }
    )


def bodies() -> list[str]:
    per_body = 3 + 3 * body.EVIDENCE_SHOWN + body.INFERENCES_SHOWN + body.NOT_CHECKED_SHOWN + 1
    chunks = [CORPUS[i : i + per_body] for i in range(0, len(CORPUS), per_body)]
    return [body.pr_body(filled(chunk), FIX, INCIDENT) for chunk in chunks]


class Tags(HTMLParser):
    """Every start tag with its attributes, and whether it sits inside a heading's permalink."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {k: v or "" for k, v in attrs}))


def tags(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = Tags()
    parser.feed(html)
    return parser.tags


def permalink(attrs: dict[str, str]) -> bool:
    return "anchor" in attrs.get("class", "").split() and attrs.get("href", "").startswith("#")


def findings(found: list[tuple[str, dict[str, str]]]) -> dict[str, int]:
    classes = [attrs.get("class", "") for _, attrs in found]
    return {
        "links (not permalinks)": sum(1 for tag, attrs in found if tag == "a" and not permalink(attrs)),
        "images": sum(1 for tag, _ in found if tag == "img"),
        "user mentions": sum("user-mention" in c for c in classes),
        "issue links": sum("issue-link" in c for c in classes),
        "details, script, iframe": sum(1 for tag, _ in found if tag in ("details", "summary", "script", "iframe")),
        "code blocks": sum(1 for tag, _ in found if tag == "pre"),
    }


def main() -> int:
    built = bodies()
    text = "\n\n---\n\n".join(built) + f"\n\n---\n\n{SPLIT}\n\n{CONTROL}\n"
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"text": text, "mode": "gfm", "context": CONTEXT}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "markdown-render-check",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = response.status
        html = response.read().decode("utf-8")
    print(f"POST /markdown: HTTP {status}; sent {len(text):,} characters in {len(built)} bodies; got {len(html):,}")
    if SPLIT not in html:
        print("FAIL: the control section's marker isn't in the answer")
        return 1
    sanitized, control = html.split(SPLIT, 1)
    seen = findings(tags(sanitized))
    shown = findings(tags(control))
    print("bodies:  " + ", ".join(f"{name} {count}" for name, count in seen.items()))
    print("control: " + ", ".join(f"{name} {count}" for name, count in shown.items()))

    failed = [name for name, count in seen.items() if name != "code blocks" and count]
    if seen["code blocks"] != 6 * len(built):
        failed.append(f"code blocks {seen['code blocks']}, not {6 * len(built)}")
    unproven = [name for name in ("links (not permalinks)", "images", "user mentions") if not shown[name]]
    if not shown["issue links"]:
        print(f"note: the control's #1 rendered no issue link in {CONTEXT}; the reference check rests on the offline tests")
    if unproven:
        print("FAIL: the control rendered no " + ", ".join(unproven) + ": the check proves nothing")
        return 1
    if failed:
        print("FAIL: " + "; ".join(failed))
        return 1
    print("PASS: nothing in the bodies renders as a link, image, mention, or reference; every block closed where it opened")
    return 0


if __name__ == "__main__":
    sys.exit(main())
