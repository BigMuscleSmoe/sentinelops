"""The agent loop end to end, through Strands, against a scripted Messages API."""

import asyncio
import json
import logging
import time
from typing import Literal

import pytest

from fake_anthropic import FakeAnthropic, Reply, answer, text, thinking, tool_use
from guards import Limits
from models import StopReason
from pricing import price_for
from providers import Settings, build_model
from runner import RunReport, investigate, system_prompt

HAIKU = price_for("anthropic", "claude-haiku-5-5")
SETTINGS = Settings()
DIAGNOSIS = {
    "root_cause": "Release 1.43.0 removed the null check in apply_promo, so checkouts without a promo fail.",
    "confidence": 0.9,
    "evidence": ["err% 10.1 in the 19:05 row", "TypeError at checkout/pricing.py:15 in apply_promo"],
    "affected_component": "checkout-api",
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
    model = build_model(SETTINGS, "sk-ant-test-0000", http_client=fake.client())
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
    assert request["headers"]["x-api-key"] == "sk-ant-test-0000"
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


def test_each_call_is_priced_from_its_own_usage_and_never_beyond_its_reservation() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)])
    report = run(fake)

    for call, request in zip(report.model_calls, fake.requests):
        # The fake bills one token per byte, the most the bound allows for.
        assert call.prompt_tokens == request["bytes"]
        assert call.bound_tokens >= call.prompt_tokens
        assert call.reserved_usd >= call.cost_usd
    assert report.cost_usd == pytest.approx(sum(c.cost_usd for c in report.model_calls))
    assert report.input_tokens == sum(r["bytes"] for r in fake.requests)


def test_the_sixteenth_tool_call_halts_the_run() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]) for _ in range(16)] + [answer(DIAGNOSIS)])
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
    assert "$0.03" in report.hypothesis.evidence[0]


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
    assert report.cost_usd == pytest.approx(HAIKU.most_a_call_costs(fake_bound(fake), 16_000), rel=0.2)


def fake_bound(fake: FakeAnthropic) -> int:
    return fake.requests[0]["bytes"] + 2_000
