"""Structured output for an investigation.

Field descriptions are sent to the model as part of the output schema, so they
are written as instructions to it, not as notes for developers.
"""

from collections.abc import Collection
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

PR_CONFIDENCE_THRESHOLD = 0.7


class StopReason(StrEnum):
    STEP_LIMIT = "step_limit"
    TOKEN_LIMIT = "token_limit"
    COST_LIMIT = "cost_limit"
    WALL_CLOCK = "wall_clock"


class Hypothesis(BaseModel):
    """The agent's diagnosis of an incident."""

    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(
        min_length=1,
        description=(
            "The mechanism that caused the incident, in one or two sentences. "
            "Name what changed or failed and how that produces the symptom, "
            "not the symptom itself."
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
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "Observations from tool calls in this investigation that support the "
            "root cause or rule out alternatives. Include values and timestamps."
        ),
    )
    affected_component: str = Field(
        min_length=1,
        description=(
            "Where the fault is. If an upstream dependency is at fault, name the "
            "dependency, not the service that alarmed."
        ),
    )
    runbook_cited: str | None = Field(
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
        return self.halted_by is None and self.confidence >= PR_CONFIDENCE_THRESHOLD
