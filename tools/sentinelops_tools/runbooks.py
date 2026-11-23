"""Runbook search: which runbooks a knowledge-base search puts first, and what the agent reads of them.

The knowledge base holds one document per runbook section, each tagged with
its runbook and section (tools/build_runbooks.py). One search returns every
section, best first. A runbook ranks by its first Symptoms or Diagnosis
section in that order: those describe what its fault looks like and how to check
for it. What this is not names the neighbors' symptoms on purpose, and
Resolution and Prevention describe fixes, so none of them ranks a runbook.

The search tool shows the top 2 runbooks in full (Symptoms, What this is not,
Diagnosis, in that order) and lists the rest by file and title, with no
scores. Resolution and Prevention are indexed but never shown: the agent fixes
nothing. The retrieval set (tests/fixtures/retrieval/) measures the ranking on
recorded searches.

The runbooks aren't dated, so a replay searches them as they are now.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sentinelops_tools.env import ToolEnv, aws_errors_as_text

RANKED_SECTIONS = ("Symptoms", "Diagnosis")
SHOWN_SECTIONS = ("Symptoms", "What this is not", "Diagnosis")
SHOWN = 2
# Retrieve's most. Every section comes back (35 for 7 runbooks), so a new
# runbook needs no change here.
MAX_RESULTS = 100
MAX_QUERY_CHARS = 500
MAX_RESULT_CHARS = 40_000  # as the log tool's
NOT_RETURNED = "The search did not return this section."
CUT_MARK = "...[cut]"


@dataclass(frozen=True)
class Hit:
    document: str  # "bad-deploy--symptoms.md"
    runbook: str  # "bad-deploy.md"
    section: str  # "Symptoms"
    score: float
    title: str = ""  # "Bad deploy"
    text: str = ""  # the document as indexed: its title line, the section's header, and its text


def hits_from_retrieve(response: dict[str, Any]) -> list[Hit]:
    """A Retrieve response's results in its order, best first, as the sections they are."""
    hits = []
    for result in response.get("retrievalResults", []):
        metadata = result.get("metadata", {})
        uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
        hits.append(
            Hit(
                document=uri.rsplit("/", 1)[-1],
                runbook=str(metadata.get("runbook", "")),
                section=str(metadata.get("section", "")),
                score=float(result.get("score", 0.0)),
                title=str(metadata.get("title", "")),
                text=str(result.get("content", {}).get("text", "")),
            )
        )
    return hits


def rank(hits: list[Hit]) -> list[str]:
    """Runbooks, best first, each where its first Symptoms or Diagnosis section comes in the search's order.

    By position, not score: the search returns its results best first, and the
    order is what it means by best.
    """
    ranked: list[str] = []
    for hit in hits:
        if hit.section in RANKED_SECTIONS and hit.runbook not in ranked:
            ranked.append(hit.runbook)
    return ranked


@aws_errors_as_text("the runbooks")
def search(env: ToolEnv, query: str) -> str:
    if env.bedrock_agent_runtime is None or not env.knowledge_base_id:
        return "Could not search the runbooks: no knowledge base is set up for this run."
    query = " ".join(str(query).split())
    if not query:
        return "Give search_runbooks a query: what you measured."
    notes = []
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
        notes.append(f"The query was cut to its first {MAX_QUERY_CHARS} characters.")
    response = env.bedrock_agent_runtime.retrieve(
        knowledgeBaseId=env.knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": MAX_RESULTS}},
    )
    return render(hits_from_retrieve(response), notes)


def render(hits: list[Hit], notes: list[str] | None = None) -> str:
    order = _order(hits)
    if not order:
        return "\n".join(["The search found no runbooks.", *(notes or [])])
    titles = {h.runbook: h.title for h in hits}
    shown, rest = order[:SHOWN], order[SHOWN:]
    lines = [
        f"The {len(shown)} runbooks closest to the query, in full: "
        + ", ".join(SHOWN_SECTIONS)
        + ". The rest are listed after them, by name only.",
        *(notes or []),
    ]
    for runbook in shown:
        lines += ["", f"=== {runbook}: {titles[runbook]} ==="]
        for section in SHOWN_SECTIONS:
            body = next((_body(h) for h in hits if h.runbook == runbook and h.section == section), NOT_RETURNED)
            lines += ["", f"## {section}", "", body]
    lines += ["", "=== Not shown, further from the query ==="]
    lines += [f"- {runbook}: {titles[runbook]}" for runbook in rest] or ["(none)"]
    text = "\n".join(lines)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n{CUT_MARK} The result passed {MAX_RESULT_CHARS:,} characters."
    return text


def _order(hits: list[Hit]) -> list[str]:
    """Every runbook in the results: the ranked ones first, then any with no ranked section, as they came."""
    order = rank(hits)
    for hit in hits:
        if hit.runbook and hit.runbook not in order:
            order.append(hit.runbook)
    return order


def _body(hit: Hit) -> str:
    """A section's text without the document's title line and section header."""
    lines = hit.text.strip("\n").split("\n")
    while lines and (lines[0].startswith("# ") or lines[0].startswith("## ") or not lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip("\n") or NOT_RETURNED


# Reading the text back into data, for the citation check and the eval grader.
# It lives beside the renderer so a change to one is a change to both.

_HEAD = re.compile(r"The (\d+) runbooks closest to the query, in full: ")
_RUNBOOK = re.compile(r"=== (\S+\.md): (.*) ===$")
_SECTION = re.compile(r"## (.+)$")
_REST = re.compile(r"- (\S+\.md): (.*)$")
_REST_HEAD = "=== Not shown, further from the query ==="


@dataclass(frozen=True)
class RunbookPage:
    """One `search` result: the runbooks it showed, closest first, and those it only listed."""

    shown: list[str]
    listed: list[str]
    titles: dict[str, str] = field(default_factory=dict)
    sections: dict[str, dict[str, str]] = field(default_factory=dict)  # runbook -> section -> text
    cut: bool = False
    # The shown runbook the cap's cut fell in, if it fell in one rather than in the list after.
    partial: str | None = None

    @property
    def in_full(self) -> list[str]:
        """The shown runbooks the model read whole: every one, unless the cut fell in one."""
        return [runbook for runbook in self.shown if runbook != self.partial]


def parse_results(text: str) -> RunbookPage | None:
    """A `search` result as data, or None if the text isn't one (an error, or a search that found nothing)."""
    lines = text.split("\n")
    if not lines or not _HEAD.match(lines[0]):
        return None
    cut = any(line.startswith(CUT_MARK) for line in lines)
    shown, listed, titles, sections = [], [], {}, {}
    runbook = section = None
    body: list[str] = []

    def close() -> None:
        if runbook is not None and section is not None:
            sections[runbook][section] = "\n".join(body).strip("\n")

    in_rest = False
    partial = None
    for line in lines[1:]:
        if line.startswith(CUT_MARK):
            # A cut before the list falls in the last runbook begun, or just after its end.
            # Nothing shows which, so that runbook doesn't count as read whole.
            partial = None if in_rest else runbook
            break
        if line == _REST_HEAD:
            close()
            runbook = section = None
            in_rest = True
            continue
        if in_rest:
            if found := _REST.match(line):
                listed.append(found[1])
                titles[found[1]] = found[2]
            continue
        if found := _RUNBOOK.match(line):
            close()
            runbook, section, body = found[1], None, []
            shown.append(runbook)
            titles[runbook] = found[2]
            sections[runbook] = {}
            continue
        if runbook is not None and (found := _SECTION.match(line)) and found[1] in SHOWN_SECTIONS:
            close()
            section, body = found[1], []
            continue
        if section is not None:
            body.append(line)
    close()
    return RunbookPage(shown=shown, listed=listed, titles=titles, sections=sections, cut=cut, partial=partial)
