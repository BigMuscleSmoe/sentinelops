"""Structured output for an investigation.

Field descriptions are sent to the model as part of the output schema, so they
are written as instructions to it, not as notes for developers.

Evidence is typed and cited (BUILD_PLAN.md, "The grader redesign", R1): each
item is one statement about one tool result, written as data, so code checks it
against exactly what it points at instead of guessing from prose. What no tool
result shows on its own goes in `inferences`, and what wasn't looked at in
`not_checked`.
"""

import re
from collections.abc import Collection
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

PR_CONFIDENCE_THRESHOLD = 0.7

# Tool-call markup written inside a field: Haiku 5.5 closed root_cause with
# </root_cause> and wrote the next fields' tags inside the string (sweep
# 20261113T201129Z). Saying so in the validation error is what the resend reads.
_MARKUP = re.compile(r"</[A-Za-z_]+>|<(?:parameter|invoke)\b")
_TIME = re.compile(r"(\d{4}-\d\d-\d\dT)?\d\d:\d\d(:\d\d(\.\d{1,6})?)?Z?")
_ITEM = re.compile(r"e([1-9]\d*)")


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
            "count: count of the cited lines (every line of the result if none are cited) have column equal "
            "to value; with no column, the result shows count lines. "
            "first, last: the oldest or newest line the result shows, among those with column equal to value "
            "if one is given, is at at. "
            "none: nothing in the result from start to end has column equal to value; with no column, the "
            "result has no row, line, or record there at all. "
            "gap: seconds pass from start to end."
        )
    )
    source: str | None = Field(
        default=None,
        description=(
            "The tool result this comes from, by the label on its first line (m1, l2, d1), or alarm for the "
            "alarm message. Every kind but gap needs one."
        ),
    )
    column: str | None = Field(
        default=None,
        description=(
            "The metric column as the table heads it (err%, pool_wait_p99_ms), the log line's field "
            "(latency_ms, error_type), or the deploy record's field (version, kind) or changed key."
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
        description="Log lines by the numbers the result gives them: [3] is line 3.",
    )
    value: str | None = Field(
        default=None,
        description=(
            "The value as the source shows it: 19.5, 1.43.0, false, TypeError. A changed key's value as "
            "old->new: 20->2."
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
            "The file name of the runbook this diagnosis follows, exactly as a "
            "runbook search in this investigation returned it. Null if no search "
            "returned a runbook that fits."
        ),
    )
    # Set by the guard, never by the model, so it's left out of the output
    # schema the model sees. None means the investigation ran to completion.
    halted_by: SkipJsonSchema[StopReason | None] = None

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

    def with_citation_checked(self, retrieved: Collection[str]) -> "Hypothesis":
        """This hypothesis, with runbook_cited cleared unless this run's searches returned it.

        The model can name any file it likes; only the run knows what it read.
        """
        if self.runbook_cited is None or self.runbook_cited in retrieved:
            return self
        return self.model_copy(update={"runbook_cited": None})

    @property
    def can_open_pr(self) -> bool:
        # A halted run didn't finish investigating; its confidence means nothing.
        # A fault outside this service has no fix in this repo (Phase 3, decision 4).
        return (
            self.halted_by is None
            and self.fault_location is FaultLocation.THIS_SERVICE
            and self.confidence >= PR_CONFIDENCE_THRESHOLD
        )
