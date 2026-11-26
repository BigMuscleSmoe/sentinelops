"""What the flow posts to GitHub, built by code: typed fields as they are, the model's words cleaned and fenced.

The user's calls (2026-11-23, addition 1; 2026-11-26, every stripping rule
below). The title, the commit message, and every comment hold typed fields
only: the incident's ID and alarm, the release, the file, the check. The
body adds the model's free text, each field in its own code block, so GitHub
renders none of it as Markdown; and before that, code strips from it what
would act on GitHub even outside a block, so a mistake in the fence still
mentions, links, and shows nothing:

  1. CR and CRLF become LF; anything outside printable ASCII, tab, and LF
     becomes "?" (bidi controls, zero-width characters, the fullwidth at
     sign, look-alike letters); a tab becomes a space.
  2. Images: ![alt](src) becomes alt. Links: [text](target) becomes text;
     reference definitions ([x]: target) and <scheme:...> autolinks go.
  3. Any scheme://..., www...., mailto:, javascript:, vbscript:, and data
     URIs up to the next space become "[link removed]".
  4. HTML: "<!--" and "-->" are removed until none is left (so "<!<!----"
     can't rejoin into one), so no marker like the flow's own
     <!-- incident: ... --> can be forged or hide text; a tag is removed;
     then any "<" still before a letter, "/", "!", or "?" gets a space after
     it, so no tag or comment can form from what's left.
  5. Every "@" is removed, so nothing mentions anyone (an address loses its
     at sign too).
  6. Issue references: "#" and "GH-" before a digit get a space after them,
     so "fixes #1" links or closes nothing.
  7. "!" before "[" gets a space, so no image can form; "://" left by
     anything above becomes ": //".
  8. Runs of four or more backticks become three, and every block's fence is
     longer than the longest run inside it, so nothing closes it early.
  9. Each field is capped, and the body is held to MAX_BODY (GitHub's limit
     is 65,536 characters). A field shown on one line of a list (an
     evidence item's column and value, a claim, a component, a thing not
     checked) has its line breaks made spaces first, so it can't forge
     another item's line.

Then validate's scan reads everything here (outgoing): an account ID, an
address, a key, a home path, a public IP, or an ARN rejects the PR.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sentinelops_core.models import Evidence, Hypothesis
from sentinelops_remediation.validate import Validated

CHECK = "test (services/checkout-api)"  # the one job the flow waits on (the user's call, 2026-11-24)
INCIDENT_ID = re.compile(r"[a-z][a-z0-9-]{0,40}-\d{8}T\d{6}Z", re.ASCII)
ALARM = re.compile(r"sentinelops-[a-z0-9-]{1,80}", re.ASCII)
OPENED_AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z", re.ASCII)
RUNBOOK = re.compile(r"[a-z0-9][a-z0-9-]{0,80}\.md", re.ASCII)

LINK_REMOVED = "[link removed]"
CUT = " [cut]"
NONE = "(none)"
MAX_BODY = 20_000  # every field at its cap and every list at its limit: about 19,200
ROOT_CAUSE_CHARS = 2_000
COMPONENT_CHARS = 300
RATIONALE_CHARS = 1_000
NOTE_CHARS = 300
VALUE_CHARS = 200
COLUMN_CHARS = 80
CLAIM_CHARS = 500
NOT_CHECKED_CHARS = 300
EVIDENCE_SHOWN = 12
INFERENCES_SHOWN = 8
NOT_CHECKED_SHOWN = 10
LINES_SHOWN = 20

# Every pattern stops at the next opener of its own kind, so it reads a long run
# of openers once, not once from each (no quadratic backtracking; the corpus's
# long runs are timed in test_body.py).
_IMAGE = re.compile(r"!\[([^\[\]\n]*)\]\([^()\n]*\)?")
_LINK = re.compile(r"\[([^\[\]\n]*)\]\([^()\n]*\)?")
_REFERENCE_DEFINITION = re.compile(r"(?m)^[ \t]*\[[^\[\]\n]+\]:.*$")
_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*>")
_TAG = re.compile(r"<[/!?]?[A-Za-z][^<>\n]*>")
# Each alternative starts only where a scheme can (not inside a longer word), so
# a long run of letters is read once, not once from every letter.
_URL = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://\S*|www\.\S*"
    r"|(?<![a-z])(?:mailto|javascript|vbscript):\S*|(?<![a-z])data:[a-z]+/\S*"
)
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
_ISSUE = re.compile(r"(?i)(#|\bgh-)(?=\d)")
_IMAGE_START = re.compile(r"!(?=\[)")
_LONG_TICKS = re.compile(r"`{4,}")
_TICKS = re.compile(r"`+")


def clean(text: str | None, cap: int) -> str:
    """The model's text with nothing left that GitHub would act on, at most cap characters (rules 1-8)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c if c == "\n" or " " <= c <= "~" else (" " if c == "\t" else "?") for c in text)
    while "<!--" in text or "-->" in text:
        text = text.replace("<!--", "").replace("-->", "")
    text = _IMAGE.sub(lambda m: m[1], text)
    text = _LINK.sub(lambda m: m[1], text)
    text = _REFERENCE_DEFINITION.sub("", text)
    text = _AUTOLINK.sub(LINK_REMOVED, text)
    text = _TAG.sub("", text)
    text = _URL.sub(LINK_REMOVED, text)
    # The rules below only insert spaces or remove one character, so nothing
    # they act on can form again from what's left.
    text = _TAG_START.sub("< ", text)
    text = text.replace("@", "")
    text = _ISSUE.sub(lambda m: m[1] + " ", text)
    text = _IMAGE_START.sub("! ", text)
    text = text.replace("://", ": //")
    text = _LONG_TICKS.sub("```", text)
    text = text.strip("\n")
    if len(text) > cap:
        text = text[: cap - len(CUT)].rstrip() + CUT
    return text


def line(text: str | None, cap: int) -> str:
    """clean(), on one line: for a field shown as one line of a list (rule 9)."""
    return clean(" ".join((text or "").splitlines()), cap)


def fenced(text: str) -> str:
    """A code block GitHub shows as plain text: its fence longer than any backtick run inside (rule 8)."""
    body = text or NONE
    longest = max((len(run) for run in _TICKS.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body}\n{fence}"


@dataclass(frozen=True)
class Incident:
    """The incident's typed fields, from its item: every one checked against its form."""

    id: str
    alarm: str
    opened_at: str

    def __post_init__(self) -> None:
        if not INCIDENT_ID.fullmatch(self.id):
            raise ValueError("not an incident ID")
        if not ALARM.fullmatch(self.alarm):
            raise ValueError("not one of the stack's alarms")
        if not OPENED_AT.fullmatch(self.opened_at):
            raise ValueError("not a UTC time")


def marker(incident_id: str, kind: str = "") -> str:
    """The HTML comment the flow finds its own writes by: the body's or a comment's first line."""
    if not INCIDENT_ID.fullmatch(incident_id) or kind not in ("", "checks"):
        raise ValueError("not a marker the flow writes")
    return f"<!-- incident: {incident_id}{' ' + kind if kind else ''} -->"


def title(fix: Validated, incident: Incident) -> str:
    return f"Fix {_module(fix)} on release {fix.version} (incident {incident.id})"


def commit_message(fix: Validated, incident: Incident) -> str:
    return (
        f"Fix {_module(fix)} on release {fix.version}\n\n"
        f"Proposed by SentinelOps for incident {incident.id} (alarm {incident.alarm}).\n"
        "A person reviews it before anything merges.\n"
    )


def pr_body(hypothesis: Hypothesis, fix: Validated, incident: Incident) -> str:
    """The draft PR's description: the incident's facts in a table, the agent's words in code blocks."""
    runbook = hypothesis.runbook_cited if hypothesis.runbook_cited and RUNBOOK.fullmatch(hypothesis.runbook_cited) else None
    rows = [
        ("Service", "`checkout-api`"),
        ("Alarm", f"`{incident.alarm}`"),
        ("Incident opened", incident.opened_at),
        ("Release running", fix.version),
        ("Fix targets", f"`{fix.base}`"),
        ("File", f"`{fix.path}`"),
        ("Fault location", hypothesis.fault_location.value.replace("_", " ")),
        ("Confidence", f"{hypothesis.confidence:.2f}"),
        ("Runbook followed", f"`{runbook}`" if runbook else "none"),
        ("Gates the PR", f"`{CHECK}`"),
    ]
    sections = [
        ("Root cause", clean(hypothesis.root_cause, ROOT_CAUSE_CHARS)),
        ("Affected component", line(hypothesis.affected_component, COMPONENT_CHARS)),
        ("Evidence", _evidence(hypothesis.evidence)),
        ("Inferences", _inferences(hypothesis)),
        ("Why this change", clean(hypothesis.remediation.rationale if hypothesis.remediation else "", RATIONALE_CHARS)),
        ("Not checked", _listed(hypothesis.not_checked, NOT_CHECKED_SHOWN, NOT_CHECKED_CHARS)),
    ]
    parts = [
        marker(incident.id),
        f"**SentinelOps proposes this fix for incident `{incident.id}`.** It was opened as a draft and is "
        f"marked ready for review only when `{CHECK}` passes. A person reviews it, then merges or closes it: "
        "nothing here merges or deploys, and nothing in it was run.",
        "| | |\n|---|---|\n" + "\n".join(f"| {name} | {value} |" for name, value in rows),
        "The sections below are the agent's own words, shown as plain text. Links, mentions, images, and "
        "markup were removed from them first.",
    ]
    parts += [f"### {heading}\n\n{fenced(text)}" for heading, text in sections]
    parts.append(
        "---\n\nThe change is this PR's one commit: the Files changed tab shows it. The agent read the file "
        f"at {fix.version}; its edits were checked in its run and again here, against `{fix.base}`, before "
        "anything was written."
    )
    body = "\n\n".join(parts) + "\n"
    if len(body) > MAX_BODY:
        raise ValueError(f"the body is {len(body)} characters, past {MAX_BODY}")
    return body


def same_fix_comment(fix: Validated, incident: Incident, pushed: bool) -> str:
    """For an open PR another incident made: this incident found the same fix."""
    change = "This incident's push updated the file." if pushed else "The file here already holds this incident's fix."
    return (
        f"{marker(incident.id)}\n"
        f"Incident `{incident.id}` (alarm `{incident.alarm}`, opened {incident.opened_at}) proposed a fix to "
        f"`{fix.path}` on {fix.version}, the file this PR changes. {change}\n"
    )


def checks_comment(incident: Incident, conclusion: str | None) -> str:
    """When the check failed or didn't finish: the PR stays a draft."""
    what = f"finished: {conclusion}" if conclusion else "didn't finish within 15 minutes"
    return (
        f"{marker(incident.id, 'checks')}\n"
        f"`{CHECK}` {what}. This PR stays a draft; nothing marks it ready for review.\n"
    )


def _module(fix: Validated) -> str:
    return fix.path.removeprefix("services/checkout-api/")


def _evidence(items: Iterable[Evidence]) -> str:
    items = list(items)
    lines = []
    for n, item in enumerate(items[:EVIDENCE_SHOWN], 1):
        fields = [f"e{n}", item.kind.value, line(item.source, 20) or "-"]
        for name in ("at", "start", "end"):
            if getattr(item, name) is not None:
                fields.append(f"{name}={line(getattr(item, name), 40)}")
        if item.lines:
            shown = ", ".join(str(n) for n in item.lines[:LINES_SHOWN])
            fields.append(f"lines={shown}{', ...' if len(item.lines) > LINES_SHOWN else ''}")
        if item.column is not None:
            fields.append(f"column={line(item.column, COLUMN_CHARS)}")
        if item.value is not None:
            fields.append(f"value={line(item.value, VALUE_CHARS)}")
        for name in ("low", "high", "count", "seconds"):
            if getattr(item, name) is not None:
                fields.append(f"{name}={getattr(item, name):g}")
        lines.append("  ".join(fields))
        lines.append("    " + clean(item.note, NOTE_CHARS).replace("\n", "\n    "))
    if len(items) > EVIDENCE_SHOWN:
        lines.append(f"[{len(items) - EVIDENCE_SHOWN} more items not shown]")
    return "\n".join(lines)


def _inferences(hypothesis: Hypothesis) -> str:
    shown = []
    for inference in hypothesis.inferences[:INFERENCES_SHOWN]:
        sources = ", ".join(ref for ref in inference.rests_on if re.fullmatch(r"e[1-9][0-9]{0,2}", ref))
        shown.append(f"- {line(inference.claim, CLAIM_CHARS)} (from {sources or 'no item'})")
    if len(hypothesis.inferences) > INFERENCES_SHOWN:
        shown.append(f"[{len(hypothesis.inferences) - INFERENCES_SHOWN} more not shown]")
    return "\n".join(shown)


def _listed(texts: list[str], shown: int, cap: int) -> str:
    lines = [f"- {line(text, cap)}" for text in texts[:shown]]
    if len(texts) > shown:
        lines.append(f"[{len(texts) - shown} more not shown]")
    return "\n".join(lines)
