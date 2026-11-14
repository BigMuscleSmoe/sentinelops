"""The agent loop end to end, through Strands, against a scripted Messages API."""

import asyncio
import json
import logging
import time
from typing import Literal

import pytest
from strands.models._native_output import _UNSUPPORTED_MECHANISMS

from fake_anthropic import ACCESS_TOKEN, FakeAnthropic, Reply, answer, signed_in, text, thinking, tool_use
from answers import DEPLOY_EVIDENCE, item
from guards import Limits
from hooks import COUNTED_FIELDS, count_margin
from models import StopReason
from pricing import price_for
from providers import Settings, build_model
from runner import RunReport, investigate, system_prompt

HAIKU = price_for("anthropic", "claude-haiku-5-5")
SETTINGS = Settings()
DIAGNOSIS = {
    "root_cause": "Release 1.43.0 removed the null check in apply_promo, so checkouts without a promo fail.",
    "confidence": 0.9,
    "evidence": DEPLOY_EVIDENCE,
    "affected_component": "checkout-api",
    "fault_location": "this_service",
}


class Tools:
    """Stand-ins with the real tools' names, counting their runs."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self.runs: list[str] = []
        self.sleep_s = sleep_s

    def all(self) -> list:
        def get_metrics(minutes: int = 10, period_seconds: Literal[10, 60] = 10) -> str:
            """Metrics.

            Args:
                minutes: How far back.
                period_seconds: The period.
            """
            self.runs.append("get_metrics")
            time.sleep(self.sleep_s)
            return "time  req  err%\n19:05:00*  276  10.1"

        def get_recent_logs(minutes: int = 10, errors_only: bool = True, limit: int = 20) -> str:
            """Logs.

            Args:
                minutes: How far back.
                errors_only: Failures only.
                limit: How many.
            """
            self.runs.append("get_recent_logs")
            return '{"error_type":"TypeError","stack":["checkout/pricing.py:15 in apply_promo"]}'

        return [get_metrics, get_recent_logs]


def run(fake: FakeAnthropic, tools: Tools | None = None, **kwargs) -> RunReport:
    model = build_model(SETTINGS, http_client=fake.client(), credentials=signed_in())
    return asyncio.run(
        investigate(
            "An alarm fired on checkout-api.",
            model=model,
            tools=(tools or Tools()).all(),
            price=HAIKU,
            max_output_tokens=SETTINGS.max_output_tokens,
            **kwargs,
        )
    )


def test_a_run_ends_with_the_models_hypothesis() -> None:
    fake = FakeAnthropic(
        [
            Reply([thinking("Errors first."), tool_use("get_metrics", {"minutes": 5})]),
            Reply([tool_use("get_recent_logs")]),
            answer(DIAGNOSIS),
        ]
    )
    tools = Tools()

    report = run(fake, tools)

    assert report.failure is None
    assert report.hypothesis is not None
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    assert report.hypothesis.halted_by is None
    assert tools.runs == ["get_metrics", "get_recent_logs"]
    assert report.tool_calls == ["get_metrics", "get_recent_logs"]
    assert len(fake.requests) == len(report.model_calls) == 3


def test_the_model_gets_the_settings_and_the_prompt() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])
    run(fake)

    request = fake.requests[0]
    body = request["body"]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 16_000
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Haiku 5.5 rejects sampling parameters with a 400.
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert "".join(block["text"] for block in body["system"]) == system_prompt()
    assert [t["name"] for t in body["tools"]] == ["get_metrics", "get_recent_logs", "Hypothesis"]


def test_every_tool_result_ends_with_the_budget() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)]
    )
    run(fake)

    results = fake.tool_results(2)
    assert [r.splitlines()[-1].split(",")[0] for r in results] == ["[budget: 14 steps", "[budget: 13 steps"]
    assert all(r.splitlines()[-1].endswith("s remaining]") and ", $0.0" in r for r in results)


def test_every_tool_result_is_labeled_as_a_source_and_its_log_lines_numbered() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), Reply([tool_use("get_metrics")]), answer(DIAGNOSIS)]
    )
    run(fake)

    metrics, logs, again = fake.tool_results(3)
    assert metrics.startswith("[source m1]\ntime  req  err%\n")
    assert logs.startswith('[source l1]\n[1] {"error_type":"TypeError"')
    assert again.startswith("[source m2]\n")


# A leak like the one that cost deploy-1029-0156-r2 its run: the closing tag inside the string.
LEAKED = {**DIAGNOSIS, "root_cause": f"{DIAGNOSIS['root_cause']}</root_cause>"}


def test_a_malformed_answer_is_sent_back_twice_then_the_run_halts() -> None:
    fake = FakeAnthropic([answer(LEAKED), answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert len(fake.requests) == 3, "no fourth call after the third rejection"
    (rejection,) = fake.tool_results(1)
    assert "holds tool-call markup ('</root_cause>')" in rejection
    assert report.hypothesis.halted_by is StopReason.RESEND_LIMIT
    assert "resend limit (2 resends of a malformed answer)" in report.hypothesis.not_checked[0]
    assert not report.hypothesis.can_open_pr


def test_an_answer_fixed_on_its_last_resend_is_kept() -> None:
    fake = FakeAnthropic([answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.halted_by is None
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]


def test_a_model_reporting_a_halt_with_no_evidence_fails_the_run() -> None:
    # A halt may carry no evidence, so this passes the answer's check; cleared of its
    # halt, it's an answer with no evidence, and nothing can resend it by then.
    fake = FakeAnthropic([answer({**DIAGNOSIS, "evidence": [], "halted_by": "step_limit"})])

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure.startswith("ValidationError")


def test_each_call_is_priced_from_its_own_usage_and_never_beyond_its_reservation() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)])
    report = run(fake)

    for call, request in zip(report.model_calls, fake.requests):
        # The fake bills one token per byte, the most the bound allows for,
        # and counts each prompt a few bytes short: no max_tokens or stream.
        assert call.prompt_tokens == request["bytes"]
        assert call.counted_tokens < call.prompt_tokens < call.limit_tokens < call.bound_tokens
        assert call.reserved_usd >= call.cost_usd
    assert report.cost_usd == pytest.approx(sum(c.cost_usd for c in report.model_calls))
    assert report.input_tokens == sum(r["bytes"] for r in fake.requests)


def test_the_sixteenth_tool_call_halts_the_run() -> None:
    # Small prompts, so the token limit can't trip first.
    fake = FakeAnthropic([Reply([tool_use("get_metrics")], input_tokens=1_000) for _ in range(16)] + [answer(DIAGNOSIS)])
    tools = Tools()

    report = run(fake, tools)

    assert report.hypothesis.halted_by is StopReason.STEP_LIMIT
    assert report.hypothesis.confidence == 0.0
    assert tools.runs == ["get_metrics"] * 15, "the 16th call is refused before the tool runs"
    assert len(fake.requests) == 16, "and the model call after it is never sent"


def test_a_call_that_could_pass_the_cost_cap_is_never_sent() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    report = run(fake, limits=Limits(max_cost_usd=0.005))  # the first call could cost ~$0.0098

    assert fake.requests == []
    assert len(fake.count_requests) == 1, "counted once, refused, and never retried"
    assert report.hypothesis.halted_by is StopReason.COST_LIMIT
    assert report.cost_usd == 0.0


def test_the_cost_cap_holds_across_a_run() -> None:
    # 60k-token prompts: each call costs ~$0.006, and the next could cost ~$0.02.
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")], input_tokens=60_000) for _ in range(10)] + [answer(DIAGNOSIS)]
    )

    report = run(fake, limits=Limits(max_cost_usd=0.03))

    assert report.hypothesis.halted_by is StopReason.COST_LIMIT
    assert len(fake.requests) == len(report.model_calls) == 2
    assert report.cost_usd <= 0.03
    assert "$0.03" in report.hypothesis.not_checked[0]


def test_the_wall_clock_cuts_off_a_slow_tool() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), answer(DIAGNOSIS)])

    report = run(fake, Tools(sleep_s=2.0), limits=Limits(max_wall_clock_s=0.5))

    assert report.hypothesis.halted_by is StopReason.WALL_CLOCK
    assert report.seconds < 1.5


def test_a_refusal_ends_the_run_without_a_hypothesis() -> None:
    fake = FakeAnthropic([Reply([text("I can't help with that.")], stop_reason="refusal")])

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure == "The model refused to go on with the investigation."
    assert len(report.model_calls) == 1 and report.cost_usd > 0, "a refusal is still paid for"


def test_a_model_that_stops_without_a_hypothesis_is_made_to_give_one() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)]
    )

    report = run(fake)

    assert fake.requests[2]["body"]["tool_choice"] == {"type": "tool", "name": "Hypothesis"}
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]


def test_each_call_is_counted_on_the_request_it_sends() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)]
    )

    run(fake)

    assert len(fake.count_requests) == len(fake.requests) == 3
    for counted, sent in zip(fake.count_requests, fake.requests):
        assert counted["body"] == {key: value for key, value in sent["body"].items() if key in COUNTED_FIELDS}
        assert counted["headers"].get("anthropic-beta") == sent["headers"].get("anthropic-beta")
    assert fake.count_requests[2]["body"]["tool_choice"] == {"type": "tool", "name": "Hypothesis"}
    # Pinned: a field Strands starts to send has to be sorted into the counted or uncounted ones on purpose.
    sent = {"max_tokens", "messages", "model", "stream", "system", "thinking", "tools"}
    assert [set(r["body"]) for r in fake.requests] == [sent, sent, sent | {"tool_choice"}]


def test_an_answer_in_native_json_is_counted_on_the_request_that_asks_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # As Sonnet 5.5 and Opus 5.5 answer, once they've rejected a forced tool in this process.
    monkeypatch.setitem(_UNSUPPORTED_MECHANISMS, ("anthropic", SETTINGS.model_id), {"forced"})
    fake = FakeAnthropic(
        [Reply([text("Done.")], stop_reason="end_turn"), Reply([text(json.dumps(DIAGNOSIS))], stop_reason="end_turn")]
    )

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    native = fake.requests[1]["body"]
    assert native["tool_choice"] == {"type": "none"} and native["output_config"]["format"]["type"] == "json_schema"
    assert fake.count_requests[1]["body"] == {key: value for key, value in native.items() if key in COUNTED_FIELDS}


def test_the_model_cannot_report_a_halt() -> None:
    fake = FakeAnthropic([answer({**DIAGNOSIS, "halted_by": "step_limit"})])

    report = run(fake)

    assert report.hypothesis.halted_by is None
    assert report.hypothesis.can_open_pr


def test_a_runbook_this_run_never_retrieved_is_not_cited(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.runner")
    fake = FakeAnthropic([answer({**DIAGNOSIS, "runbook_cited": "db-pool-exhaustion.md"})])

    report = run(fake)

    assert report.hypothesis.runbook_cited is None
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.runner"]
    assert events == [
        {
            "event": "citation_dropped",
            "cited": "db-pool-exhaustion.md",
            "reason": "no runbook search in this run returned it",
        }
    ]


def test_a_runbook_this_run_retrieved_is_kept() -> None:
    fake = FakeAnthropic([answer({**DIAGNOSIS, "runbook_cited": "db-pool-exhaustion.md"})])

    report = run(fake, retrieved_runbooks={"db-pool-exhaustion.md"})

    assert report.hypothesis.runbook_cited == "db-pool-exhaustion.md"


def test_a_failed_call_ends_the_run_and_is_charged_the_most_it_could_cost() -> None:
    fake = FakeAnthropic([])  # the fake answers 400: its script is empty

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure.startswith("BadRequestError")
    assert report.model_calls == []
    counted = fake.count_requests[0]["bytes"]  # the fake's count, unscripted
    assert report.cost_usd == pytest.approx(HAIKU.most_a_call_costs(counted + count_margin(counted), 16_000))


# Strict tool use's own limits (platform docs, structured outputs: schema complexity limits).
STRICT_MAX_OPTIONAL = 24
STRICT_MAX_UNIONS = 16
STRICT_REJECTS = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def schema_nodes(node, path="$"):
    """Every subschema in a JSON schema, with its path; property names are never taken for keywords."""
    if isinstance(node, list):
        for i, sub in enumerate(node):
            yield from schema_nodes(sub, f"{path}[{i}]")
    elif isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                for name, sub in value.items():
                    yield from schema_nodes(sub, f"{path}.{key}.{name}")
            else:
                yield from schema_nodes(value, f"{path}.{key}")


def test_the_answer_tool_alone_goes_out_strict_on_every_call() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)])

    run(fake)

    for sent in fake.requests + fake.count_requests:
        strict = {tool["name"]: tool.get("strict") for tool in sent["body"]["tools"]}
        assert strict == {"get_metrics": None, "get_recent_logs": None, "Hypothesis": True}


def test_the_strict_schema_is_one_the_api_takes() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    run(fake)

    schema = next(tool for tool in fake.requests[0]["body"]["tools"] if tool["name"] == "Hypothesis")["input_schema"]
    nodes = list(schema_nodes(schema))
    assert [(path, sorted(STRICT_REJECTS & set(node))) for path, node in nodes if STRICT_REJECTS & set(node)] == []
    objects = [(path, node) for path, node in nodes if node.get("type") == "object"]
    assert [path for path, node in objects] == ["$", "$.properties.evidence.items", "$.properties.inferences.items"]
    assert all(node["additionalProperties"] is False for _, node in objects)
    # Every field required: with 14 optional ones the API refused the schema as too complex to compile.
    assert [(path, node["required"]) for path, node in objects] == [(path, list(node["properties"])) for path, node in objects]
    # The limits are over the whole request; this is its only strict tool.
    optional = sum(len(set(node["properties"]) - set(node.get("required", []))) for _, node in objects)
    unions = sum(
        "anyOf" in prop or isinstance(prop.get("type"), list)
        for _, node in objects
        for prop in node["properties"].values()
    )
    assert optional <= STRICT_MAX_OPTIONAL and unions <= STRICT_MAX_UNIONS
    # The fields the answer may leave unset go out nullable (a type array), each a union: 11 of
    # Evidence's and runbook_cited. inferences and not_checked go out as plain lists.
    assert (optional, unions) == (0, 12), "pinned: a field added or made nullable moves these on purpose"


def test_the_strict_schema_is_only_what_the_api_needs_changed() -> None:
    # What the model reads is the same: the fields and their descriptions. What changes is what strict
    # tool use needs: no rejected keyword, closed objects, every field required, and no null where
    # Pydantic takes none. (The tool's own description is Strands', untouched: format_request sets only
    # input_schema and strict.)
    from strands.tools.structured_output.structured_output_utils import convert_pydantic_to_tool_spec

    from models import Hypothesis

    fake = FakeAnthropic([answer(DIAGNOSIS)])

    run(fake)

    sent = next(tool for tool in fake.requests[0]["body"]["tools"] if tool["name"] == "Hypothesis")
    strands = convert_pydantic_to_tool_spec(Hypothesis)
    original = dict(schema_nodes(strands["inputSchema"]["json"]))
    no_null = {"$.properties.inferences", "$.properties.not_checked"}
    changed = {"additionalProperties", "required", "type"}
    for path, node in schema_nodes(sent["input_schema"]):
        dropped = set(original[path]) - set(node)
        added = set(node) - set(original[path])
        assert dropped <= STRICT_REJECTS, path
        assert added <= {"additionalProperties"}, path
        assert {k: v for k, v in node.items() if k not in changed and not isinstance(v, (dict, list))} == {
            k: v
            for k, v in original[path].items()
            if k not in STRICT_REJECTS | changed and not isinstance(v, (dict, list))
        }, path
        if path in no_null:
            assert (original[path]["type"], node["type"]) == (["array", "null"], "array"), path
        else:
            assert node.get("type") == original[path].get("type"), path
        if "required" in node:
            assert node["required"] == list(node["properties"]), path


def test_an_answer_written_out_in_full_is_the_answer_it_was() -> None:
    # Under strict tool use the answer writes every field: null where it used to leave one out, and []
    # for no inferences. Pydantic reads it as the same answer, accepted first time.
    from models import Evidence, Hypothesis

    full = {
        **DIAGNOSIS,
        "evidence": [{**dict.fromkeys(Evidence.model_fields), **one} for one in DIAGNOSIS["evidence"]],
        "inferences": [],
        "not_checked": [],
        "runbook_cited": None,
    }
    written_out, left_out = FakeAnthropic([answer(full)]), FakeAnthropic([answer(DIAGNOSIS)])

    assert run(written_out).hypothesis == run(left_out).hypothesis
    assert len(written_out.requests) == len(left_out.requests) == 1
    # Strands' schema would have let inferences be null, which Pydantic refuses: the strict schema doesn't.
    with pytest.raises(ValueError, match="valid list"):
        Hypothesis.model_validate({**DIAGNOSIS, "inferences": None})


def test_a_strict_answer_is_still_checked_for_what_the_schema_no_longer_says() -> None:
    # minLength and the 0-1 bound left the sent schema; Pydantic still holds the answer to them.
    fake = FakeAnthropic([answer({**DIAGNOSIS, "confidence": 1.5}), answer({**DIAGNOSIS, "root_cause": ""}), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.confidence == DIAGNOSIS["confidence"]
    rejected = [fake.tool_results(i) for i in (1, 2)]
    assert "less than or equal to 1" in " ".join(rejected[0])
    assert "at least 1 character" in " ".join(rejected[1])
