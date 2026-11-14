import copy
import math

import pytest
from pydantic import ValidationError

from answers import UPSTREAM_EVIDENCE, item
from models import FaultLocation, Hypothesis, StopReason, trimmed_answer

VALID = {
    "root_cause": "Upstream payments-api p99 rose from 120ms to 4.8s at 14:02; checkout-api times out waiting on it.",
    "confidence": 0.82,
    "evidence": UPSTREAM_EVIDENCE,
    "affected_component": "payments-api",
    "fault_location": "upstream",
}
# The same answer, as if the fault were in this service: the gate can open.
IN_SERVICE = {**VALID, "affected_component": "checkout-api", "fault_location": "this_service"}


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
        {**IN_SERVICE, "confidence": 0.95, "halted_by": StopReason.STEP_LIMIT}
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
        (0.95, StopReason.COST_LIMIT, False),
        (1.0, StopReason.WALL_CLOCK, False),
        (1.0, StopReason.RESEND_LIMIT, False),
    ],
)
def test_can_open_pr(confidence: float, halted_by: StopReason | None, expected: bool) -> None:
    h = Hypothesis.model_validate({**IN_SERVICE, "confidence": confidence, "halted_by": halted_by})
    assert h.can_open_pr is expected


def test_every_field_is_described_for_the_model() -> None:
    schema = Hypothesis.model_json_schema()
    models = {"Hypothesis": schema, **{name: schema["$defs"][name] for name in ("Evidence", "Inference")}}
    for model, part in models.items():
        for name, prop in part["properties"].items():
            assert prop.get("description"), f"{model}.{name} has no description in the output schema"


def test_runbook_cited_defaults_to_none_and_is_offered_to_the_model() -> None:
    assert Hypothesis.model_validate(VALID).runbook_cited is None
    assert "runbook_cited" in Hypothesis.model_json_schema()["properties"]


def test_a_citation_this_run_retrieved_is_kept() -> None:
    h = Hypothesis.model_validate({**VALID, "runbook_cited": "db-pool-exhaustion.md"})
    assert h.with_citation_checked({"db-pool-exhaustion.md"}) is h


def test_a_citation_this_run_never_retrieved_is_cleared() -> None:
    # The model can name a file it never read. Only the run's own record counts.
    h = Hypothesis.model_validate({**VALID, "runbook_cited": "db-pool-exhaustion.md"})

    checked = h.with_citation_checked(set())

    assert checked.runbook_cited is None
    assert checked.root_cause == h.root_cause
    assert h.runbook_cited == "db-pool-exhaustion.md", "the original is left alone"


def test_no_citation_needs_no_check() -> None:
    h = Hypothesis.model_validate(VALID)
    assert h.with_citation_checked(set()) is h


# --- the fault location -----------------------------------------------------------


@pytest.mark.parametrize(("location", "expected"), [("this_service", True), ("upstream", False), ("undetermined", False)])
def test_only_a_fault_in_this_service_can_open_a_pr(location: str, expected: bool) -> None:
    h = Hypothesis.model_validate({**IN_SERVICE, "confidence": 0.95, "fault_location": location})
    assert h.can_open_pr is expected


def test_the_fault_location_is_required() -> None:
    with pytest.raises(ValidationError, match="fault_location"):
        Hypothesis.model_validate({k: v for k, v in VALID.items() if k != "fault_location"})


# --- typed evidence ---------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        item("value", source="m1", column="err%", at="19:05:00", value="10.1"),
        item("value", source="l2", column="error_type", lines=[3], value="TypeError"),
        item("value", source="d1", column="version", at="2026-11-12T07:46:29.556Z", value="1.43.0"),
        item("range", source="m1", column="p99_ms", start="18:41", end="18:55", low=472, high=860),
        item("range", source="l1", column="latency_ms", lines=[1, 2, 5], low=0.2, high=0.4),
        item("all", source="l1", column="status", value="500"),
        item("count", source="l1", count=20),
        item("first", source="l1", column="version", value="1.43.0", at="02:57:20.104"),
        item("none", source="m1", column="err%", start="18:30", end="18:55", value="0"),
        item("gap", start="18:56:06.039", end="19:00:00", seconds=234),
    ],
    ids=lambda e: e["kind"],
)
def test_each_kind_of_item_validates_with_what_it_needs(evidence: dict) -> None:
    Hypothesis.model_validate({**VALID, "evidence": [evidence]})


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (item("value", source="m1", column="err%", value="10.1"), "needs at or lines"),
        (item("value", source="l1", column="status", lines=[1, 2], value="500"), "cites one line"),
        (item("range", source="m1", column="err%", start="18:41", low=0, high=1), "needs end with its start"),
        (item("range", source="m1", column="err%", start="18:41", end="18:55", low=5, high=1), "low is more than high"),
        (item("count", column="status", count=3), "needs source"),
        (item("none", source="m1", start="18:30"), "needs end"),
        (item("gap", start="18:56", end="19:00"), "needs seconds"),
        (item("value", source="m1", column="err%", at="six past seven", value="1"), "must be a time"),
        (item("value", source="m1", column="err%", at="19:05", value="1", seen=True), "Extra inputs"),
        (item("value", source="m1", column="err%", at="19:05", value="1", note=""), "at least 1 character"),
    ],
)
def test_an_item_missing_what_its_kind_needs_is_rejected(evidence: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Hypothesis.model_validate({**VALID, "evidence": [evidence]})


def test_a_sentence_is_not_an_evidence_item() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate({**VALID, "evidence": ["payments-api p99 4.8s at 14:03"]})


def test_a_halt_may_carry_no_evidence_and_an_answer_may_not() -> None:
    assert Hypothesis.model_validate({**VALID, "evidence": [], "halted_by": StopReason.STEP_LIMIT}).evidence == []
    with pytest.raises(ValidationError, match="evidence needs at least one item"):
        Hypothesis.model_validate({**VALID, "evidence": []})


# --- inferences -------------------------------------------------------------------


def test_an_inference_rests_on_items_that_exist() -> None:
    two = [*UPSTREAM_EVIDENCE, item("count", source="l1", count=4)]
    h = Hypothesis.model_validate({**VALID, "evidence": two, "inferences": [{"claim": "The timeouts follow the slowdown.", "rests_on": ["e1", "e2"]}]})
    assert h.inferences[0].rests_on == ["e1", "e2"]
    for bad in (["e3"], ["e0"], ["1"], []):
        with pytest.raises(ValidationError):
            Hypothesis.model_validate({**VALID, "evidence": two, "inferences": [{"claim": "x", "rests_on": bad}]})


# --- leaked markup (R5) -----------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        {**VALID, "root_cause": "Pool exhausted.</root_cause>"},
        {**VALID, "root_cause": 'Pool exhausted. <parameter name="confidence">0.9'},
        {**VALID, "affected_component": "checkout-api</affected_component>"},
        {**VALID, "not_checked": ["the replica's limit</not_checked>"]},
        {**VALID, "evidence": [item("count", source="l1", count=2, note="two lines</note>")]},
        {**VALID, "inferences": [{"claim": "<invoke name=\"Hypothesis\">", "rests_on": ["e1"]}]},
    ],
)
def test_tool_call_markup_in_any_prose_field_is_rejected_by_name(answer: dict) -> None:
    with pytest.raises(ValidationError, match="holds tool-call markup"):
        Hypothesis.model_validate(answer)


@pytest.mark.parametrize("prose", ["p99 < 100 ms, then > 1,400 ms", "1.42.0 -> 1.43.0", "<none>", "a <b> tag"])
def test_ordinary_angle_brackets_are_kept(prose: str) -> None:
    assert Hypothesis.model_validate({**VALID, "root_cause": prose}).root_cause == prose


# --- a leaked tail on root_cause, cut instead of sent back ----------------------------------

CAUSE = VALID["root_cause"]
TAIL = '</root_cause>\n<parameter name="confidence">0.82'
LEAKED = {**VALID, "root_cause": CAUSE + TAIL}


@pytest.mark.parametrize(
    "tail",
    [
        # As Haiku 5.5 wrote them under strict tool use (sweep 20261114T183031Z, the check replay).
        '</root_cause>\n<parameter name="confidence">0.93',
        '</root_cause>\n<parameter name="confidence">0.9',
        # Before strict tool use, opening the fields they swallowed.
        '</root_cause>\n<parameter name="affected_component">payments-api',
        '</root_cause>\n<parameter name="runbook_cited">null',
        # Everything after the opener goes, more markup included.
        '</root_cause>\n<parameter name="confidence">0.82</parameter>\n<parameter name="fault_location">upstream',
        # A bare closing tag at the very end.
        "</root_cause>",
        "</root_cause>\n",
        # Whitespace before the tag goes with it.
        " \n</root_cause>\n<parameter name=\"confidence\">0.82",
    ],
)
def test_a_leaked_tail_at_the_end_of_root_cause_is_cut_from_the_tag_on(tail: str) -> None:
    answer = {**VALID, "root_cause": CAUSE + tail}
    before = copy.deepcopy(answer)

    cut, removed = trimmed_answer(answer)

    assert cut == VALID  # root_cause as it was before the tag, every other field as written
    assert removed == tail
    assert answer == before


@pytest.mark.parametrize(
    "answer",
    [
        # Another fault besides the tail: the normal rejection names them all.
        {k: v for k, v in LEAKED.items() if k != "confidence"},  # the tail swallowed it, as before strict
        {**LEAKED, "confidence": 1.5},
        {**LEAKED, "affected_component": ""},
        {**LEAKED, "evidence": [item("value", column="err%", at="19:05:00", value="10.1")]},  # no source
        {**LEAKED, "inferences": [{"claim": "Payments-api is the cause.", "rests_on": ["e9"]}]},
        # Markup anywhere else.
        {**LEAKED, "affected_component": "payments-api</affected_component>"},
        {**LEAKED, "runbook_cited": "db-pool-exhaustion.md</runbook_cited>"},
        {**LEAKED, "not_checked": ["the replica's limit</not_checked>"]},
        {**LEAKED, "evidence": [item("count", source="l1", count=2, note="two lines</note>")]},
        {**LEAKED, "inferences": [{"claim": '<invoke name="Hypothesis">', "rests_on": ["e1"]}]},
        {**VALID, "root_cause": 'Pool <invoke name="Hypothesis"> exhausted.' + TAIL},
        {**VALID, "root_cause": "Pool exhausted.</root_cause> Then more." + TAIL},
        # Not the tail: another tag, an opener naming no field, an opener alone, the tag not at the end.
        {**VALID, "root_cause": CAUSE + '</parameter>\n<parameter name="confidence">0.82'},
        {**VALID, "root_cause": CAUSE + "</confidence>"},
        {**VALID, "root_cause": CAUSE + '</root_cause>\n<parameter name="cause">0.82'},
        {**VALID, "root_cause": CAUSE + '<parameter name="confidence">0.82'},
        {**VALID, "root_cause": CAUSE + "</root_cause> and then more prose."},
        # Nothing left once cut.
        {**VALID, "root_cause": TAIL},
        {**VALID, "root_cause": " \n</root_cause>"},
        # Nothing to cut.
        VALID,
        {**VALID, "root_cause": None},
        "not an answer",
    ],
)
def test_anything_else_is_left_for_the_normal_rejection(answer) -> None:
    assert trimmed_answer(answer) is None
