"""Structured output for an investigation.

Field descriptions are sent to the model as part of the output schema, so they
are written as instructions to it, not as notes for developers.

Evidence is typed and cited (BUILD_PLAN.md, "The grader redesign", R1): each
item is one statement about one tool result, written as data, so code checks it
against exactly what it points at instead of guessing from prose. What no tool
result shows on its own goes in `inferences`, and what wasn't looked at in
`not_checked`.

From Phase 4 the answer can carry a patch (`remediation`), which the agent
writes inside its guarded session, so nothing downstream calls a model. Its
fields hold no rule that could reject the answer: a bad patch is dropped by the
run's check (runner.check_remediation), never sent back, so it can't cost the
diagnosis a resend.

This module lives in sentinelops_core, which the agent and the remediation flow
both import: `can_open_pr` is the one place that decides whether a PR may be
opened, for both of them.
"""

import re
from collections.abc import Collection
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, WithJsonSchema, model_validator
from pydantic.json_schema import SkipJsonSchema

PR_CONFIDENCE_THRESHOLD = 0.7

# Tool-call markup written inside a field: Haiku 5.5 closed root_cause with
# </root_cause> and wrote the next fields' tags inside the string (sweep
# 20261113T201129Z). Saying so in the validation error is what the resend reads.
_MARKUP = re.compile(r"</[A-Za-z_]+>|<(?:parameter|invoke)\b")
_TIME = re.compile(r"(\d{4}-\d\d-\d\dT)?\d\d:\d\d(:\d\d(\.\d{1,6})?)?Z?")
_ITEM = re.compile(r"e([1-9]\d*)")
# What evidence may cite: the alarm, or a metrics, log, or deploy-history result by its
# label (the agent's sources.PREFIXES: m, l, d). A runbook search (r) or a code read (c)
# observes nothing that happened, and the prompt's rule alone left 11 of 19 resends in
# sweep 20261124T072730Z on code cited as evidence. Strict tool use decodes to the
# pattern, so the model can't write another source. It's the schema's alone, not
# Pydantic's: an answer valid when written still validates, and the checker grades
# any such item miscited.
EVIDENCE_SOURCE = r"^(alarm|[mld][1-9][0-9]*)$"


def _no_markup(text: str) -> str:
    found = _MARKUP.search(text)
    if found:
        raise ValueError(
            f"holds tool-call markup ({found.group()!r}): give each field its own JSON value, "
            "with no tags inside a string"
        )
    return text


def _a_time(text: str | None) -> str | None:
    if text is not None and not _TIME.fullmatch(text):
        raise ValueError(
            "must be a time as the source shows it, such as 18:59:00, 08:03:17.897, or 2026-11-12T07:46:29.556Z"
        )
    return text


Text = Annotated[str, AfterValidator(_no_markup)]
Time = Annotated[str | None, AfterValidator(_a_time)]


class StopReason(StrEnum):
    STEP_LIMIT = "step_limit"
    TOKEN_LIMIT = "token_limit"
    COST_LIMIT = "cost_limit"
    WALL_CLOCK = "wall_clock"
    RESEND_LIMIT = "resend_limit"


class FaultLocation(StrEnum):
    THIS_SERVICE = "this_service"
    UPSTREAM = "upstream"
    UNDETERMINED = "undetermined"


class EvidenceKind(StrEnum):
    VALUE = "value"
    RANGE = "range"
    ALL = "all"
    COUNT = "count"
    FIRST = "first"
    LAST = "last"
    NONE = "none"
    GAP = "gap"


# The fields each kind can't do without. A tuple is "one of these".
_NEEDS: dict[EvidenceKind, tuple[str | tuple[str, ...], ...]] = {
    EvidenceKind.VALUE: ("source", "column", "value", ("at", "lines")),
    EvidenceKind.RANGE: ("source", "column", "low", "high", ("start", "lines")),
    EvidenceKind.ALL: ("source", "column", "value"),
    EvidenceKind.COUNT: ("source", "count"),
    EvidenceKind.FIRST: ("source", "at"),
    EvidenceKind.LAST: ("source", "at"),
    EvidenceKind.NONE: ("source", "start", "end"),
    EvidenceKind.GAP: ("start", "end", "seconds"),
}


class Evidence(BaseModel):
    """One observation from one tool result in this investigation, stated so code can check it there."""

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind = Field(
        description=(
            "What this item states. "
            "value: column is value at one row (at) or one log line (lines). "
            "range: across the rows from start to end, or the cited lines, column runs from low to high, "
            "its smallest and largest value. "
            "all: every row from start to end, every cited line, or with neither every line of the result, "
            "has column equal to value. "
            "count: count of the cited lines (with no lines cited, every line of the result) have column "
            "equal to value; with no column, the result shows count lines. "
            "first, last: the oldest or newest line the result shows, among those with column equal to value "
            "if one is given, is at at. "
            "none: no row, line, or record in the result from start to end has column equal to value; with "
            "no column, the result has nothing there at all. It's checked against everything in the window, "
            "not only what matters: a window that holds a record you judge unrelated isn't none, so cite that "
            "record in an item of its own and say in its note why it isn't the cause. "
            "gap: seconds pass from start to end."
        )
    )
    source: Annotated[str, WithJsonSchema({"type": "string", "pattern": EVIDENCE_SOURCE})] | None = Field(
        default=None,
        description=(
            "The tool result this comes from: a metrics, log, or deploy-history result by the label on its "
            "first line, or alarm for the alarm message. Never a runbook search or a code read: what a read "
            "shows goes in inferences. Every kind but gap needs one."
        ),
    )
    column: str | None = Field(
        default=None,
        description=(
            "The metric column as the table heads it (err%, pool_wait_p99_ms), the log line's field "
            "(latency_ms, error_type), or the deploy record's field (version, kind). For a config change, "
            "the changed key's own name, as its key shows it: MAX_RETRIES."
        ),
    )
    at: Time = Field(
        default=None,
        description=(
            "One time as the source shows it: a metric row's start (18:59:00), or a log line's or deploy "
            "record's timestamp, whole or from its hour on (07:46:29.556)."
        ),
    )
    start: Time = Field(
        default=None,
        description="The first time of a window, as the source shows times. The window includes it.",
    )
    end: Time = Field(
        default=None,
        description=(
            "The last time of a window, as the source shows times. The window includes it. For none, the "
            "window must be one the result covers."
        ),
    )
    lines: list[int] | None = Field(
        default=None,
        description=(
            "Every log line it rests on, each by the number the result gives it: [3] is line 3, and [3, 4, 5] "
            "is lines 3 to 5. A list holds only the numbers in it: [1, 30] is lines 1 and 30, not 1 to 30. "
            "For a run of lines, list every one, or give start and end for the lines in that stretch of time; "
            "for every line of the result, leave lines out."
        ),
    )
    value: str | None = Field(
        default=None,
        description=(
            "The value as the source shows it: 19.5, 2.7.0, false, SSLError. A changed key's value as "
            "old->new: 30->5."
        ),
    )
    low: float | None = Field(default=None, description="For range: the smallest value among the cited rows or lines.")
    high: float | None = Field(default=None, description="For range: the largest value among the cited rows or lines.")
    count: int | None = Field(default=None, ge=0, description="For count: how many.")
    seconds: float | None = Field(default=None, description="For gap: the seconds from start to end.")
    note: Text = Field(
        min_length=1,
        description=(
            "What this shows for the diagnosis, in a few words. Every number or time it rests on goes in the "
            "fields above, not here."
        ),
    )

    @model_validator(mode="after")
    def _has_what_its_kind_needs(self) -> Self:
        missing = []
        for need in _NEEDS[self.kind]:
            names = need if isinstance(need, tuple) else (need,)
            if all(getattr(self, name) in (None, []) for name in names):
                missing.append(" or ".join(names))
        if missing:
            raise ValueError(f"a {self.kind.value} item needs {', '.join(missing)}")
        if self.kind is EvidenceKind.VALUE and self.at is None and len(self.lines or []) != 1:
            raise ValueError("a value item cites one line, or one time with at")
        if self.kind is EvidenceKind.RANGE and self.lines is None and self.end is None:
            raise ValueError("a range item needs end with its start, or lines")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("low is more than high")
        return self


class Inference(BaseModel):
    """A conclusion from evidence items that no single tool result shows."""

    model_config = ConfigDict(extra="forbid")

    claim: Text = Field(min_length=1, description="The conclusion, in one sentence.")
    rests_on: list[str] = Field(
        min_length=1,
        description="The evidence items it follows from, by position: e1 is the first evidence item, e2 the second.",
    )


class Edit(BaseModel):
    """One passage of the file, replaced."""

    model_config = ConfigDict(extra="forbid")

    old: str = Field(
        description=(
            "A passage copied exactly from the file as the read shows it, indentation included, without the "
            "line number and tab before each line. It must occur exactly once in the file: take in enough of "
            "the lines around it to make it unique."
        )
    )
    new: str = Field(description="What replaces old, indented the same way. Empty deletes old.")


class Remediation(BaseModel):
    """A change to this service's code that removes the root cause, for a person to review."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        description=(
            "The read this change is against, by the label on its first line: a read_repo_file result showing "
            "the file itself in full, not a listing."
        )
    )
    file_path: str = Field(
        description="The file's path exactly as that read's first line shows it, as checkout/<module>.py."
    )
    edits: list[Edit] = Field(
        description="One to three replacements in that file, none overlapping another, together the smallest change that removes the root cause."
    )
    rationale: Text = Field(description="Why this change removes the root cause, in one or two sentences.")
    # Set by code once the run answers (runner.check_remediation), never by the
    # model, so they're left out of the output schema the model sees.
    version: SkipJsonSchema[str | None] = None  # the release the read showed
    file_sha256: SkipJsonSchema[str | None] = None  # of the file as read; the flow finds the same bytes on GitHub
    diff: SkipJsonSchema[str | None] = None  # rendered from the edits


class Hypothesis(BaseModel):
    """The agent's diagnosis of an incident."""

    model_config = ConfigDict(extra="forbid")

    root_cause: Text = Field(
        min_length=1,
        description=(
            "The mechanism that caused the incident, in one or two sentences. "
            "Name what changed or failed and how that produces the symptom, "
            "not the symptom itself. Every number in it must also be in an evidence item."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description=(
            "How likely this root cause is to be correct, from 0 to 1. "
            "Below 0.4 means you don't know."
        ),
    )
    evidence: list[Evidence] = Field(
        description=(
            "What you observed through tool calls in this investigation, one checkable statement per item, "
            "each citing the tool result it comes from. Include what rules out the alternatives, not only "
            "what supports the root cause."
        ),
    )
    inferences: list[Inference] = Field(
        default_factory=list,
        description="Conclusions from the evidence that no tool result shows on its own, such as a cause or a comparison.",
    )
    not_checked: list[Text] = Field(
        default_factory=list,
        description="What bears on the diagnosis that you couldn't or didn't check.",
    )
    affected_component: Text = Field(
        min_length=1,
        description=(
            "Where the fault is. If an upstream dependency is at fault, name the "
            "dependency, not the service that alarmed."
        ),
    )
    fault_location: FaultLocation = Field(
        description=(
            "this_service if the fault is in checkout-api itself, upstream if it's in a dependency such as "
            "payments-api, undetermined if the evidence doesn't show which."
        ),
    )
    runbook_cited: Text | None = Field(
        default=None,
        description=(
            "The file name of the runbook this diagnosis follows, exactly as a search in this investigation "
            "showed it in full: the name.md in its === name.md: Title === line. A runbook the search only listed "
            "by name can't be cited. Null if no runbook shown in full fits: one fits only if its Symptoms match "
            "what you measured and nothing in its What this is not applies."
        ),
    )
    remediation: Remediation | None = Field(
        default=None,
        description=(
            "A fix for the root cause as a change to this service's code, which a person reviews before "
            "anything changes. Only when the cause is in checkout-api's own code, and only against a file you "
            "read in full with read_repo_file in this investigation, at the version that was running: the "
            "smallest change that removes the cause. Null if the cause is a configuration change, is outside "
            "this service, or is in code you didn't read in full."
        ),
    )
    # Set by the guard, never by the model, so it's left out of the output
    # schema the model sees. None means the investigation ran to completion.
    halted_by: SkipJsonSchema[StopReason | None] = None
    # Set by the runner after every run, never by the model: the release running
    # when the alarm fired, the version on the newest deploy-history record at or
    # before then among the run's own reads. None if no read showed one.
    running_version: SkipJsonSchema[str | None] = None

    @model_validator(mode="after")
    def _evidence_and_references(self) -> Self:
        # Only a halt, which the guard writes, comes back with nothing observed.
        if self.halted_by is None and not self.evidence:
            raise ValueError("evidence needs at least one item")
        for inference in self.inferences:
            for ref in inference.rests_on:
                match = _ITEM.fullmatch(ref)
                if match is None or int(match[1]) > len(self.evidence):
                    raise ValueError(
                        f"rests_on names {ref!r}, but the evidence items are e1 to e{len(self.evidence)}"
                    )
        return self

    def with_citation_checked(self, in_full: Collection[str]) -> "Hypothesis":
        """This hypothesis, with runbook_cited cleared unless it names, exactly, a runbook this run's searches showed in full.

        The model can name any file it likes; only the run knows what it read (runner.check_citation).
        """
        if self.runbook_cited is None or self.runbook_cited in in_full:
            return self
        return self.model_copy(update={"runbook_cited": None})

    @property
    def can_open_pr(self) -> bool:
        # A halted run didn't finish investigating; its confidence means nothing.
        # A fault outside this service has no fix in this repo (Phase 3, decision 4).
        # A PR needs a patch the run's check kept (Phase 4): none means the fix
        # isn't in this service's code, or the patch failed the check and was
        # dropped. Only the check sets version, file_sha256, and diff, so an
        # answer that never went through it opens nothing. The patch must be
        # against the release that was running: one made on the release a deploy
        # replaced fixes code that wasn't serving (sweep 20261124T072730Z's
        # deploy-1112-2000 restored a guard 1.42.0 already had, twice over).
        patch = self.remediation
        return (
            self.halted_by is None
            and self.fault_location is FaultLocation.THIS_SERVICE
            and self.confidence >= PR_CONFIDENCE_THRESHOLD
            and patch is not None
            and None not in (patch.version, patch.file_sha256, patch.diff)
            and patch.version == self.running_version
        )


# Haiku 5.5's leak under strict tool use (sweep 20261114T183031Z, 6 of 9 first
# answers): root_cause closed with its own closing tag inside the string, then
# the next field's tool-call opener, with that field also written as its own
# value. That tail, or a bare closing tag, at the very end is all that's cut.
_LEAKED_TAIL = re.compile(
    r"\s*</root_cause>(?:\s*<parameter name=\"(?:"
    + "|".join(Hypothesis.model_json_schema()["properties"])
    + r")\">.*)?\s*\Z",
    re.DOTALL,
)


def trimmed_answer(answer: Any) -> tuple[dict[str, Any], str] | None:
    """The answer with a leaked tail cut off root_cause, and the text cut off.

    None unless root_cause ends in that tail and the cut answer then validates
    in full: every other field, the checks across fields, and the cut root_cause
    itself, so markup left anywhere still gets the normal rejection and a resend.
    The answer passed in is left as it was.
    """
    root_cause = answer.get("root_cause") if isinstance(answer, dict) else None
    tail = _LEAKED_TAIL.search(root_cause) if isinstance(root_cause, str) else None
    if tail is None:
        return None
    cut = {**answer, "root_cause": root_cause[: tail.start()]}
    try:
        Hypothesis.model_validate(cut)
    except ValidationError:
        return None
    return cut, root_cause[tail.start() :]
