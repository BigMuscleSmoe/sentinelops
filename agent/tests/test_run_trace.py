"""run_trace: the live run's trace for the page, and the format it writes (sentinelops_core.trace).

Runs here are scripted (fake_anthropic) and the table is an in-memory stand-in,
so nothing reaches AWS or a model. The fakes the scan must catch are built at
run time: written out whole, they'd be caught in this file.
"""

import asyncio
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import investigation
from fake_anthropic import FakeAnthropic, Reply, answer, signed_in, text, thinking, tool_use
from fake_gateway import URL, FakeGateway
from answers import CHECKED_PATCH
from pricing import price_for
from providers import Settings, build_model
from run_trace import CITATION_DROPPED, HALTS, PATCH_DROPPED, Cleaner, Steps, Trace, Writer, open_trace
from runner import investigate
from sentinelops_core import trace as fmt
from sentinelops_core.models import Hypothesis, StopReason
from sentinelops_tools.env import ToolEnv
from test_main import DIAGNOSIS, ERRORS, FEDERATED, HAIKU, investigating, replay_session
from test_runner import Tools

FAKE_ID = "4318" + "2905" + "7716"
INCIDENT = "checkout-api-20261126T183624Z"
T0 = datetime(2026, 11, 26, 18, 36, 25, 594000, tzinfo=UTC)


class Table:
    """Stands in for the DynamoDB client: put_item keeps each item, in order, or raises what it's told to."""

    def __init__(self, fail: Exception | None = None, delay_s: float = 0.0) -> None:
        self.items: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.fail = fail
        self.delay_s = delay_s
        self.threads: set[str] = set()

    def put_item(self, TableName: str, Item: dict[str, Any]) -> dict[str, Any]:  # noqa: N803 - boto3's names
        self.calls.append(TableName)
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        self.items.append(Item)
        return {}

    def events(self) -> list[tuple[str, str, fmt.EventModel]]:
        return [fmt.read(item) for item in self.items]

    def kinds(self) -> list[str]:
        return [item["kind"]["S"] for item in self.items]


def tracing(table: Table, *, background: bool = False) -> Trace:
    return Trace(Steps(Cleaner(FAKE_ID), HAIKU), Writer(table, "sentinelops-trace", INCIDENT, background=background))


def scripted() -> FakeAnthropic:
    return FakeAnthropic(
        [
            Reply([thinking("Errors first."), text("Reading the metrics."), tool_use("get_metrics", {"minutes": 5})]),
            Reply([tool_use("get_recent_logs")]),
            answer({**DIAGNOSIS, "runbook_cited": None}),
        ]
    )


def run(fake: FakeAnthropic, trace: Trace | None = None) -> Any:
    model = build_model(Settings(), http_client=fake.client(), credentials=signed_in())
    if trace is not None:
        trace.started(ERRORS)
    report = asyncio.run(
        investigate(
            "An alarm fired on checkout-api.",
            model=model,
            tools=Tools().all(),
            price=HAIKU,
            max_output_tokens=Settings().max_output_tokens,
            observers=() if trace is None else (trace.hooks,),
        )
    )
    if trace is not None:
        trace.finished(report)
        trace.close()
    return report


# --- a traced run ----------------------------------------------------------------


def test_a_traced_run_writes_each_step_in_order() -> None:
    table = Table()

    report = run(scripted(), tracing(table))

    assert report.hypothesis is not None
    assert table.kinds() == [
        "run_started",
        "model_call",
        "thinking",
        "said",
        "tool_call",
        "tool_result",
        "model_call",
        "tool_call",
        "tool_result",
        "model_call",
        "answered",
    ]
    assert [item["sk"]["S"] for item in table.items] == [f"agent#{n:04d}" for n in range(1, 12)]
    assert {item["pk"]["S"] for item in table.items} == {INCIDENT}
    events = [event for _, _, event in table.events()]
    assert events[0] == fmt.RunStarted(alarm="errors")
    assert events[2] == fmt.Thinking(text="Errors first.") and events[3] == fmt.Said(text="Reading the metrics.")
    assert events[4] == fmt.ToolCall(call="t1", tool="get_metrics", args={"minutes": 5})
    result = events[5]
    assert isinstance(result, fmt.ToolResult) and result.call == "t1" and result.source == "m1"
    assert result.text.startswith("time  req  err%") and result.text.endswith("remaining]")  # the label off, the budget kept
    assert result.chars == len(result.text) and not result.truncated and not result.error
    assert events[7] == fmt.ToolCall(call="t2", tool="get_recent_logs", args={})
    calls = [e for e in events if isinstance(e, fmt.ModelCall)]
    assert [c.n for c in calls] == [1, 2, 3]
    assert [c.cost_usd for c in calls] == [round(m.cost_usd, 6) for m in report.model_calls]
    assert [c.input_tokens for c in calls] == [m.prompt_tokens for m in report.model_calls]
    assert events[-1] == fmt.Answered(confidence=0.9)
    times = [at for _, at, _ in table.events()]
    assert times == sorted(times)


def test_what_the_model_is_sent_is_the_same_with_the_trace_on() -> None:
    plain, traced = scripted(), scripted()

    run(plain)
    run(traced, tracing(Table()))

    assert [r["body"] for r in traced.requests] == [r["body"] for r in plain.requests]


def test_each_item_expires_in_7_days_unless_its_the_recorded_run() -> None:
    table = Table()
    writer = Writer(table, "sentinelops-trace", INCIDENT, background=False)
    kept = Writer(table, "sentinelops-trace", INCIDENT, expires=False, background=False)

    writer.put([(T0, fmt.HandedOff())])
    kept.put([(T0, fmt.HandedOff())])

    assert table.items[0]["expires_at"] == {"N": str(int(T0.timestamp()) + 7 * 24 * 3600)}
    assert "expires_at" not in table.items[1]
    assert table.items[0]["at"] == {"S": "2026-11-26T18:36:25.594Z"}


def test_the_writer_writes_from_its_own_thread_in_order_and_close_waits_for_it() -> None:
    table = Table(delay_s=0.01)
    writer = Writer(table, "sentinelops-trace", INCIDENT)

    started = time.monotonic()
    writer.put([(T0 + timedelta(seconds=n), fmt.ModelCall(n=n, input_tokens=1, output_tokens=1, cost_usd=0.0)) for n in range(1, 6)])
    queued_in = time.monotonic() - started
    counts = writer.close()

    assert queued_in < 0.01  # put never waits on a write
    assert table.threads == {"trace-writer"}
    assert [e.n for _, _, e in table.events()] == [1, 2, 3, 4, 5]  # type: ignore[attr-defined]
    assert counts == {"written": 5, "failed": 0, "skipped": 0, "queued": 5}


def test_close_waits_no_longer_than_its_timeout() -> None:
    table = Table(delay_s=0.2)
    writer = Writer(table, "sentinelops-trace", INCIDENT)
    writer.put([(T0, fmt.HandedOff())] * 20)

    started = time.monotonic()
    counts = writer.close(timeout=0.3)

    assert time.monotonic() - started < 1.0
    assert counts["written"] < 20


# --- best-effort: the trace never touches the run -----------------------------------


class Throttled(Exception):
    response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "arn:aws:dynamodb:x"}}


def test_failed_writes_never_stop_the_run_and_three_in_a_row_stop_the_writing(caplog: pytest.LogCaptureFixture) -> None:
    table = Table(fail=Throttled())

    with caplog.at_level(logging.INFO, logger="sentinelops.trace"):
        report = run(scripted(), tracing(table))

    assert report.hypothesis is not None and report.failure is None
    assert len(table.calls) == 3  # then it stops trying
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.trace"]
    failed = [line for line in lines if line["event"] == "trace_write_failed"]
    assert [line["error"] for line in failed] == ["ProvisionedThroughputExceededException"] * 3
    assert "arn:aws" not in caplog.text  # the code, never the message
    assert lines[-1] == {"event": "trace_closed", "incident": INCIDENT, "written": 0, "failed": 3, "skipped": 8, "queued": 11}


def test_a_step_that_fails_to_build_never_stops_the_run(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken(*args: Any) -> list[Any]:
        raise KeyError("boom")

    monkeypatch.setattr(Steps, "model_call", broken)
    monkeypatch.setattr(Steps, "tool_result", broken)
    table = Table()

    with caplog.at_level(logging.INFO, logger="sentinelops.trace"):
        report = run(scripted(), tracing(table))

    assert report.hypothesis is not None and report.failure is None
    assert table.kinds() == ["run_started", "tool_call", "tool_call", "answered"]
    failed = [json.loads(r.getMessage()) for r in caplog.records if "trace_failed" in r.getMessage()]
    assert {(line["step"], line["error"]) for line in failed} == {("model_call", "KeyError"), ("tool_result", "KeyError")}


# --- the texts: capped and scanned -----------------------------------------------------


def test_a_text_with_a_finding_is_withheld_whole_and_names_only_its_rules() -> None:
    cleaner = Cleaner(FAKE_ID)
    account = f"role in {FAKE_ID} failed"
    arn = "see arn:" + "aws:iam::role/x"

    assert cleaner.text("t", account) == ("[withheld: account-id]", False)
    assert cleaner.text("t", arn) == ("[withheld: arn]", False)
    assert cleaner.text("t", f"{account}\n{arn}") == ("[withheld: account-id, arn]", False)
    assert cleaner.text("t", "pool wait p99 120 ms") == ("pool wait p99 120 ms", False)
    assert cleaner.withheld == 3


def test_a_text_past_its_cap_is_cut_and_flagged() -> None:
    cleaner = Cleaner(FAKE_ID)

    shown, truncated = cleaner.text("t", "x" * 4_001)

    assert shown == "x" * 4_000 and truncated
    assert cleaner.text("t", "x" * 4_000) == ("x" * 4_000, False)


def test_a_tool_result_with_a_finding_is_withheld_and_its_size_still_shown() -> None:
    steps = Steps(Cleaner(FAKE_ID), HAIKU)
    steps.tool_call(T0, {"name": "get_recent_logs", "toolUseId": "u1", "input": {"route": f"/x/{FAKE_ID}"}})
    body = '{"msg": "denied for arn:' + 'aws:iam::role/r"}'

    [(_, result)] = steps.tool_result(T0, {"toolUseId": "u1", "status": "success", "content": [{"text": f"[source l1]\n{body}"}]})

    assert result == fmt.ToolResult(call="t1", source="l1", chars=len(body), text="[withheld: arn]", truncated=False, error=False)


def test_an_argument_is_capped_and_scanned_and_the_answer_tool_is_no_step() -> None:
    steps = Steps(Cleaner(FAKE_ID), HAIKU)

    [(_, call)] = steps.tool_call(T0, {"name": "search_runbooks", "toolUseId": "u1", "input": {"query": "q" * 300}})
    [(_, secret)] = steps.tool_call(T0, {"name": "get_recent_logs", "toolUseId": "u2", "input": {"route": f"a {FAKE_ID}", "limit": 5}})
    later = T0 + timedelta(seconds=3)

    assert steps.tool_call(later, {"name": "Hypothesis", "toolUseId": "u3", "input": {}}) == []
    assert call == fmt.ToolCall(call="t1", tool="search_runbooks", args={"query": "q" * 200})
    assert secret == fmt.ToolCall(call="t2", tool="get_recent_logs", args={"route": "[withheld: account-id]", "limit": 5})
    assert steps.answered_at == later
    assert steps.tool_call(T0, {"name": "delete_everything", "toolUseId": "u4", "input": {}}) == []


# --- how a run ends --------------------------------------------------------------------


def hypothesis(**changes: Any) -> Hypothesis:
    return Hypothesis.model_validate({**DIAGNOSIS, **changes})


def test_every_halt_is_shown_by_its_name_on_the_page() -> None:
    assert set(HALTS) == set(StopReason)
    for reason, shown in HALTS.items():
        halted = Hypothesis.model_validate({**DIAGNOSIS, "halted_by": reason.value, "evidence": [], "confidence": 0.0})
        assert Steps(Cleaner(FAKE_ID), HAIKU).finished(T0, halted, None, None) == [(T0, fmt.Halted(by=shown))]


def test_a_kept_citation_and_patch_are_checks_that_passed() -> None:
    answered = hypothesis(runbook_cited="bad-deploy.md", remediation=CHECKED_PATCH, running_version="1.43.0")
    steps = Steps(Cleaner(FAKE_ID), HAIKU)
    steps.answered_at = T0

    events = [event for _, event in steps.finished(T0 + timedelta(seconds=1), answered, None, None)]

    assert events == [
        fmt.Answered(confidence=0.9),
        fmt.Check(check="citation", kept=True, detail="bad-deploy.md was shown in full in this run, so the citation stands."),
        fmt.Check(
            check="patch",
            kept=True,
            detail="1 edit to checkout/pricing.py at 1.43.0: +2 -0 lines, applies exactly once, compiles.",
        ),
    ]


def test_a_dropped_citation_and_patch_say_so_from_fixed_words() -> None:
    steps = Steps(Cleaner(FAKE_ID), HAIKU)

    events = [
        event
        for _, event in steps.finished(
            T0, hypothesis(), {"cited": "<script>x</script>.md", "reason": "only listed"}, {"reason": "no result c9"}
        )
    ]

    assert events == [
        fmt.Answered(confidence=0.9),
        fmt.Check(check="citation", kept=False, detail=CITATION_DROPPED["only listed"]),
        fmt.Check(check="patch", kept=False, detail=PATCH_DROPPED),
    ]


def test_a_failed_run_ends_its_trace_with_nothing_more() -> None:
    assert Steps(Cleaner(FAKE_ID), HAIKU).finished(T0, None, None, None) == []


def test_only_the_three_alarms_start_a_trace() -> None:
    steps = Steps(Cleaner(FAKE_ID), HAIKU)

    assert steps.started(T0, "sentinelops-checkout-pool-wait") == [(T0, fmt.RunStarted(alarm="pool_wait"))]
    assert steps.started(T0, "sentinelops-checkout-latency") == [(T0, fmt.RunStarted(alarm="latency"))]
    assert steps.started(T0, "someone-elses-alarm") == []


# --- wiring: the investigation and the app ------------------------------------------------


def test_an_investigation_with_a_trace_writes_it_start_to_end_on_the_real_tools() -> None:
    session, fake = replay_session(), investigating()
    gateway = FakeGateway(ToolEnv.from_session(session))
    table = Table()
    trace = tracing(table)

    outcome = asyncio.run(
        investigation.investigate_alarm(
            ERRORS,
            datetime(2026, 10, 27, 19, 6, tzinfo=UTC),
            gateway_url=URL,
            settings=FEDERATED,
            price=HAIKU,
            session=session,
            http_client=fake.client(),
            token_http_client=fake.token_client(),
            gateway_transport=gateway.transport(),
            trace=trace,
        )
    )
    trace.close()

    assert isinstance(outcome, investigation.Investigation)
    assert table.kinds()[0] == "run_started" and table.kinds()[-1] == "answered"
    results = [e for _, _, e in table.events() if isinstance(e, fmt.ToolResult)]
    assert [r.source for r in results] == ["m1", "l1", "d1"]
    assert all(not r.text.startswith("[withheld") for r in results)  # the recorded results are clean


def test_without_the_table_or_the_account_a_run_has_no_trace(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sentinelops.trace"):
        assert open_trace({fmt.TABLE_ENV: "sentinelops-trace"}, object(), INCIDENT, HAIKU) is None
        assert open_trace({"SENTINELOPS_ACCOUNT_ID": FAKE_ID}, object(), INCIDENT, HAIKU) is None

    assert [json.loads(r.getMessage())["event"] for r in caplog.records] == ["trace_off", "trace_off"]


def test_the_trace_client_has_short_timeouts_and_no_retry() -> None:
    made: list[tuple[str, Any]] = []

    class Session:
        def client(self, service: str, config: Any = None) -> Table:
            made.append((service, config))
            return Table()

    trace = open_trace({fmt.TABLE_ENV: "sentinelops-trace", "SENTINELOPS_ACCOUNT_ID": FAKE_ID}, Session(), INCIDENT, HAIKU)

    assert trace is not None
    [(service, config)] = made
    assert service == "dynamodb"
    assert (config.connect_timeout, config.read_timeout) == (1.0, 2.0)
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    trace.close()


# --- the format ----------------------------------------------------------------------


def test_keys_sort_the_agents_steps_before_the_flows() -> None:
    keys = [fmt.agent_key(n) for n in (1, 2, 10, 9999)] + [fmt.flow_key(s) for s in fmt.FLOW_SLOTS]

    assert keys == sorted(keys)
    assert len(set(fmt.FLOW_SLOTS.values())) == len(fmt.FLOW_SLOTS)
    assert fmt.flow_key("load") == "flow#00" and fmt.flow_key("checks-15") == "flow#17"


def test_an_item_reads_back_as_its_event_and_a_bad_one_is_refused() -> None:
    event = fmt.PrSameFix(number=2, commented=False)
    item = fmt.item(INCIDENT, fmt.flow_key("open"), T0, event)

    assert fmt.read(item) == ("flow#02", "2026-11-26T18:36:25.594Z", event)
    with pytest.raises(ValueError):
        fmt.item("not an incident", "flow#02", T0, event)
    with pytest.raises(ValueError):
        fmt.item(INCIDENT, "agent#1", T0, event)
    with pytest.raises(ValueError):
        fmt.Thinking(text="x" * 4_001)
    with pytest.raises(ValueError):
        fmt.EVENT.validate_json('{"kind": "said", "text": "hi", "html": "<b>"}')



# --- the Runtime's grant ---------------------------------------------------------------


def test_the_runtime_may_only_put_items_on_checkout_apis_incidents_traces() -> None:
    infra = Path(__file__).resolve().parents[2] / "infra"
    tf = (infra / "agent_runtime.tf").read_text(encoding="utf-8")
    policy = tf[tf.index('data "aws_iam_policy_document" "agent_runtime" {') :]
    policy = policy[: policy.index("\n}\n")]
    start = policy.index('sid       = "WriteTheTrace"')
    trace = policy[start : policy.find("\n  statement {", start) if "\n  statement {" in policy[start:] else len(policy)]

    assert 'actions   = ["dynamodb:PutItem"]' in trace and "resources = [aws_dynamodb_table.trace.arn]" in trace
    assert 'test     = "ForAllValues:StringLike"\n      variable = "dynamodb:LeadingKeys"\n      values   = ["checkout-api-*"]' in trace
    assert 'test     = "Null"\n      variable = "dynamodb:LeadingKeys"\n      values   = ["false"]' in trace
    assert "SENTINELOPS_TRACE_TABLE                  = aws_dynamodb_table.trace.name" in tf
    assert "SENTINELOPS_ACCOUNT_ID                   = data.aws_caller_identity.current.account_id" in tf
    assert fmt.INCIDENT_ID.fullmatch(INCIDENT) and INCIDENT.startswith("checkout-api-")
