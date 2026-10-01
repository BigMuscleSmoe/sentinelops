"""Structured output for an investigation.

Field descriptions are sent to the model as part of the output schema, so they
are written as instructions to it, not as notes for developers.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

PR_CONFIDENCE_THRESHOLD = 0.7


class StopReason(StrEnum):
    STEP_LIMIT = "step_limit"
    TOKEN_LIMIT = "token_limit"
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
    # Set by the guard, never by the model, so it's left out of the output
    # schema the model sees. None means the investigation ran to completion.
    halted_by: SkipJsonSchema[StopReason | None] = None

    @property
    def can_open_pr(self) -> bool:
        # A halted run didn't finish investigating; its confidence means nothing.
        return self.halted_by is None and self.confidence >= PR_CONFIDENCE_THRESHOLD
