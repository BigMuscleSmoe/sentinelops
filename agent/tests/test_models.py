import math

import pytest
from pydantic import ValidationError

from models import Hypothesis, StopReason

VALID = {
    "root_cause": "Upstream payments-api p99 rose from 120ms to 4.8s at 14:02; checkout-api times out waiting on it.",
    "confidence": 0.82,
    "evidence": ["payments-api p99 4.8s at 14:03", "checkout-api CPU flat at 12%"],
    "affected_component": "payments-api",
}


def test_accepts_a_valid_hypothesis() -> None:
    h = Hypothesis.model_validate(VALID)
    assert h.affected_component == "payments-api"


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_accepts_confidence_at_the_bounds(confidence: float) -> None:
    Hypothesis.model_validate({**VALID, "confidence": confidence})


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 85, math.nan, math.inf])
def test_rejects_confidence_outside_zero_to_one(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**VALID, "confidence": confidence})


@pytest.mark.parametrize("field", ["root_cause", "affected_component"])
def test_rejects_empty_strings(field: str) -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**VALID, field: ""})


def test_rejects_a_hypothesis_with_no_evidence() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**VALID, "evidence": []})


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**VALID, "fix_applied": True})


def test_halted_by_defaults_to_none() -> None:
    assert Hypothesis.model_validate(VALID).halted_by is None


def test_halted_by_is_hidden_from_the_model() -> None:
    # The guard sets it; the model must not be invited to.
    assert "halted_by" not in Hypothesis.model_json_schema()["properties"]


def test_halted_by_survives_the_hop_to_the_remediation_flow() -> None:
    # The agent and the Durable Function are separate processes. If halted_by
    # were dropped in serialization, a halted run would arrive looking finished.
    halted = Hypothesis.model_validate(
        {**VALID, "confidence": 0.95, "halted_by": StopReason.STEP_LIMIT}
    )

    received = Hypothesis.model_validate_json(halted.model_dump_json())

    assert received.halted_by is StopReason.STEP_LIMIT
    assert not received.can_open_pr


@pytest.mark.parametrize(
    ("confidence", "halted_by", "expected"),
    [
        (0.85, None, True),
        (0.7, None, True),
        (0.69, None, False),
        (0.0, None, False),
        (0.95, StopReason.STEP_LIMIT, False),
        (1.0, StopReason.WALL_CLOCK, False),
    ],
)
def test_can_open_pr(confidence: float, halted_by: StopReason | None, expected: bool) -> None:
    h = Hypothesis.model_validate({**VALID, "confidence": confidence, "halted_by": halted_by})
    assert h.can_open_pr is expected


def test_every_field_is_described_for_the_model() -> None:
    schema = Hypothesis.model_json_schema()
    for name, prop in schema["properties"].items():
        assert prop.get("description"), f"{name} has no description in the output schema"
