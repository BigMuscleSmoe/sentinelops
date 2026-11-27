"""backfill_trace: the recorded run's trace rebuilt from its record, log times, and flow history.

The record here is synthetic, shaped as the trigger stores the Runtime's
answer (agent/investigation.py record()); no AWS call is made.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import backfill_trace as backfill
from pricing import price_for
from run_trace import Cleaner, Steps
from sentinelops_core import trace as fmt
from test_main import DIAGNOSIS

FAKE_ID = "4318" + "2905" + "7716"
T0 = datetime(2026, 11, 26, 18, 36, 25, 594000, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def use(use_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": use_id, "name": name, "input": arguments}}


def result(use_id: str, source: str, body: str) -> dict[str, Any]:
    return {"toolResult": {"toolUseId": use_id, "status": "success", "content": [{"text": f"[source {source}]\n{body}"}]}}


def answer(*content: dict[str, Any], tokens: int = 100) -> dict[str, Any]:
    return {"role": "assistant", "content": list(content), "metadata": {"usage": {"inputTokens": tokens, "outputTokens": 10}}}


RECORD = {
    "alarm": {"name": "sentinelops-checkout-errors"},
    "model": {"provider": "anthropic", "model_id": "claude-haiku-5-5"},
    "hypothesis": {**DIAGNOSIS, "runbook_cited": None},
    "citation_dropped": None,
    "remediation_dropped": None,
    "model_calls": [{}, {}, {}],
    "tool_calls": ["get_metrics", "get_recent_logs", "get_recent_logs"],
    "messages": [
        {"role": "user", "content": [{"text": "An alarm fired."}]},
        answer(use("a", "get_metrics", minutes=5), use("b", "get_recent_logs")),
        {"role": "user", "content": [result("a", "m1", "rows"), result("b", "l1", "lines")]},
        answer({"reasoningContent": {"reasoningText": {"text": "Errors since 18:35."}}}, use("c", "get_recent_logs"), tokens=200),
        {"role": "user", "content": [result("c", "l2", "more lines")]},
        answer({"text": "Done."}, use("h", "Hypothesis", **DIAGNOSIS), tokens=300),
    ],
}
REMEDIATION = {"status": "ready", "pr": "opened", "pr_number": 2.0, "pushed": True, "checks": "success"}
FLOW = {
    "load": (at(58), at(58.04)),
    "open": (at(58.2), at(63.6)),
    "checks-1": (at(123.4), at(124.0)),
    "checks-2": (at(184.0), at(184.5)),
    "conclude": (at(184.6), at(186.0)),
    "record": (at(186.1), at(186.1)),
}


def sources(**changes: Any) -> backfill.Sources:
    fields = {
        "record": RECORD,
        "invoked_at": T0,
        "model_ends": [at(13), at(20), at(54)],
        "tool_starts": [at(13.001), at(17.5), at(20.001)],
        "finished_at": at(54.01),
        "flow_steps": FLOW,
        "remediation": REMEDIATION,
    }
    return backfill.Sources(**{**fields, **changes})


def steps() -> Steps:
    return Steps(Cleaner(FAKE_ID), price_for("anthropic", "claude-haiku-5-5"))


def test_the_run_is_rebuilt_in_hook_order_with_each_result_landing_as_the_next_tool_starts() -> None:
    found = sources()

    events = backfill.agent_events(found, steps())

    assert [(fmt.iso(when), e.kind) for when, e in events] == [
        ("2026-11-26T18:36:25.594Z", "run_started"),
        ("2026-11-26T18:36:38.594Z", "model_call"),
        ("2026-11-26T18:36:38.595Z", "tool_call"),
        ("2026-11-26T18:36:43.093Z", "tool_result"),  # the next tool's start, less 1 ms
        ("2026-11-26T18:36:43.094Z", "tool_call"),
        ("2026-11-26T18:36:44.094Z", "tool_result"),  # estimated: no earlier get_recent_logs, so 1 s
        ("2026-11-26T18:36:45.594Z", "model_call"),
        ("2026-11-26T18:36:45.594Z", "thinking"),
        ("2026-11-26T18:36:45.595Z", "tool_call"),
        ("2026-11-26T18:36:46.595Z", "tool_result"),  # estimated, still 1 s: its duration was never observed
        ("2026-11-26T18:37:19.594Z", "model_call"),
        ("2026-11-26T18:37:19.594Z", "said"),
        ("2026-11-26T18:37:19.594Z", "answered"),  # the answer tool's call, not a step
    ]
    kinds = {e.kind: e for _, e in events}
    assert kinds["run_started"] == fmt.RunStarted(alarm="errors")
    assert [e.input_tokens for _, e in events if isinstance(e, fmt.ModelCall)] == [100, 200, 300]
    assert [(e.call, e.source, e.text) for _, e in events if isinstance(e, fmt.ToolResult)] == [
        ("t1", "m1", "rows"),
        ("t2", "l1", "lines"),
        ("t3", "l2", "more lines"),
    ]
    assert len(found.estimates) == 2 and all("estimated" in line for line in found.estimates)


def test_an_observed_duration_estimates_the_same_tools_last_result() -> None:
    record = {**RECORD, "messages": [*RECORD["messages"][:3], answer(use("c", "get_metrics"), use("h", "Hypothesis", **DIAGNOSIS))]}
    record["messages"].insert(4, {"role": "user", "content": [result("c", "m2", "rows again")]})
    record = {**record, "model_calls": [{}, {}], "tool_calls": ["get_metrics", "get_recent_logs", "get_metrics"]}
    found = sources(record=record, model_ends=[at(13), at(20)], tool_starts=[at(13.0), at(17.5), at(20.5)])

    events = backfill.agent_events(found, steps())

    last = [when for when, e in events if isinstance(e, fmt.ToolResult)][-1]
    assert last == at(20.5) + timedelta(seconds=4.499)  # get_metrics took 4.499 s the first time


def test_an_estimate_never_passes_the_next_model_calls_end() -> None:
    found = sources(model_ends=[at(13), at(17.6), at(54)])

    events = backfill.agent_events(found, steps())

    second = [when for when, e in events if isinstance(e, fmt.ToolResult)][1]
    assert second == at(17.6) - timedelta(milliseconds=1)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"model_ends": [at(13), at(20)]}, "model_call lines"),
        ({"tool_starts": [at(13.001), at(17.5)]}, "more tool calls than tool_call lines"),
        ({"tool_starts": [at(13.001), at(17.5), at(20.001), at(30)]}, "with no tool call in the record"),
    ],
)
def test_log_lines_that_dont_match_the_record_are_refused(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(SystemExit, match=reason):
        backfill.agent_events(sources(**changes), steps())


def test_the_flow_is_handed_off_opened_polled_and_ready() -> None:
    assert backfill.flow_events(sources()) == [
        ("load", at(58), fmt.HandedOff()),
        ("open", at(63.6), fmt.PrOpened(number=2)),
        ("checks-1", at(124.0), fmt.PrChecks(check=backfill.CHECK, state="pending", poll=1)),
        ("checks-2", at(184.5), fmt.PrChecks(check=backfill.CHECK, state="success", poll=2)),
        ("conclude", at(186.0), fmt.PrReady(number=2)),
    ]


def test_a_same_fix_from_before_the_cap_counts_as_commented_and_a_failed_check_ends_unready() -> None:
    events = backfill.flow_events(sources(remediation={**REMEDIATION, "pr": "commented", "status": "checks_failed", "checks": "failure"}))

    assert events[1] == ("open", at(63.6), fmt.PrSameFix(number=2, commented=True))
    assert events[-1] == ("checks-2", at(184.5), fmt.PrChecks(check=backfill.CHECK, state="failure", poll=2))


@pytest.mark.parametrize("remediation", [{"status": "no_pr"}, {"status": "rejected", "rejected_check": "base"}])
def test_a_flow_that_opened_nothing_isnt_backfilled(remediation: dict[str, Any]) -> None:
    with pytest.raises(SystemExit, match="only a flow that opened or found a PR"):
        backfill.flow_events(sources(remediation=remediation))


def test_the_remediation_maps_scalars_read_as_python() -> None:
    assert [backfill._plain(v) for v in ({"S": "ready"}, {"N": "2"}, {"BOOL": True}, {"NULL": True})] == ["ready", 2.0, True, None]
