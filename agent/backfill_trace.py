"""Write the recorded run's trace (Phase 5, decision 8) from what AWS kept of it.

    uv run python backfill_trace.py --incident checkout-api-20261126T183624Z         # from agent/: a dry run
    uv run python backfill_trace.py --incident checkout-api-20261126T183624Z --go    # writes it

The live trace (run_trace.py) came after the 2026-11-26 run, so its steps are
rebuilt from three sources: the incident's stored record (every message and
tool result, as the trigger stored the Runtime's answer), the Runtime's log
lines (when each model call ended and each tool started), and the flow's
durable history (when each step ran). The logs and the history are kept 7
days. run_trace.Steps builds the events, as it does a live run's, so they're
capped and scanned the same way. They're written with no expiry, so the
recorded run stays on the page.

No source says when a round's last tool returned. Its time is estimated as its
start plus that tool's duration earlier in the run, or 1 s if the tool hadn't
run yet, and never later than the next model call's end. The dry run lists
each estimate. It prints each event's key, time, kind, and sizes, never its
text.

It reads AWS with the sentinelops profile. --go also needs PutItem on
sentinelops-trace, which the deploy creates. Writing is idempotent: each event
has a fixed key, so a second run rewrites the same items. Not shipped in the
Runtime (build_runtime.NOT_SHIPPED).
"""

import argparse
import gzip
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
# commit_scan, for run_trace's scan: on pytest's pythonpath, staged in the Runtime, and put here by hand.
sys.path.insert(0, str(REPO / ".githooks"))

import commit_scan  # noqa: E402

from pricing import price_for  # noqa: E402
from run_trace import ANSWER_TOOL, Cleaner, Stamped, Steps, Writer  # noqa: E402
from sentinelops_core import trace as fmt  # noqa: E402
from sentinelops_core.models import Hypothesis  # noqa: E402

REGION = "us-east-1"
SERVICE = "checkout-api"
INCIDENTS_TABLE = "sentinelops-incidents"
TRACE_TABLE = "sentinelops-trace"
FLOW_FUNCTION = "sentinelops-remediation"
RUNTIME_LOGS = "/aws/bedrock-agentcore/runtimes/sentinelops_agent"
CHECK = "test (services/checkout-api)"  # the check the flow waits on (remediation body.CHECK)
FIRST_RUN_S = 1.0
JUST_BEFORE = timedelta(milliseconds=1)
CONCLUDED = ("ready", "checks_failed", "checks_timeout")


@dataclass(frozen=True)
class Sources:
    """What AWS kept of one run, read once."""

    record: Mapping[str, Any]  # the Runtime's answer, as the trigger stored it
    invoked_at: datetime
    model_ends: list[datetime]  # each "model_call" line: when that call's answer arrived
    tool_starts: list[datetime]  # each "tool_call" line: when that tool started
    finished_at: datetime  # the "invocation" line: the answer sent back
    flow_steps: Mapping[str, tuple[datetime, datetime]]  # each step's start and end
    remediation: Mapping[str, Any]  # the flow's map on the incident
    estimates: list[str] = field(default_factory=list)


def agent_events(sources: Sources, steps: Steps) -> list[Stamped]:
    """The run's steps, in the order its hooks would have seen them, timed from the log lines."""
    record = sources.record
    answers = [m for m in record["messages"] if m["role"] == "assistant"]
    if len(answers) != len(sources.model_ends):
        raise SystemExit(f"{len(answers)} model answers but {len(sources.model_ends)} model_call lines: logs short?")
    results = {
        block["toolResult"]["toolUseId"]: block["toolResult"]
        for message in record["messages"]
        if message["role"] == "user"
        for block in message["content"]
        if "toolResult" in block
    }
    starts = list(sources.tool_starts)
    durations: dict[str, float] = {}
    out = steps.started(sources.invoked_at, record["alarm"]["name"])
    for n, (message, end) in enumerate(zip(answers, sources.model_ends, strict=True)):
        out += steps.model_call(end, message)
        next_end = sources.model_ends[n + 1] if n + 1 < len(sources.model_ends) else None
        uses = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        tools = [use for use in uses if use["name"] != ANSWER_TOOL]
        if len(tools) > len(starts):
            raise SystemExit("more tool calls than tool_call lines: logs short?")
        times, starts = starts[: len(tools)], starts[len(tools) :]
        for use in uses:
            if use["name"] == ANSWER_TOOL:
                steps.tool_call(end, use)  # the answer's time; no step of its own
        for i, (use, start) in enumerate(zip(tools, times, strict=True)):
            out += steps.tool_call(start, use)
            if i + 1 < len(tools):
                landed = times[i + 1] - JUST_BEFORE  # tools run one at a time: it returned as the next began
                durations.setdefault(use["name"], (landed - start).total_seconds())
            else:
                gap = durations.get(use["name"], FIRST_RUN_S)
                landed = start + timedelta(seconds=gap)
                if next_end is not None and landed >= next_end:
                    landed = next_end - JUST_BEFORE
                sources.estimates.append(f"{use['name']} started {fmt.iso(start)}: returned at +{gap:.3f} s, estimated")
            out += steps.tool_result(landed, results[use["toolUseId"]])
    if starts:
        raise SystemExit(f"{len(starts)} tool_call lines with no tool call in the record: another run's?")
    hypothesis = Hypothesis.model_validate(record["hypothesis"]) if record.get("hypothesis") else None
    out += steps.finished(sources.finished_at, hypothesis, record.get("citation_dropped"), record.get("remediation_dropped"))
    return out


def flow_events(sources: Sources) -> list[tuple[str, datetime, fmt.EventModel]]:
    """The flow's steps, as its _trace calls would have written them: (step, time, event)."""
    rem, ran = sources.remediation, sources.flow_steps
    if rem.get("status") not in CONCLUDED or rem.get("pr") not in ("opened", "commented"):
        raise SystemExit("only a flow that opened or found a PR and concluded is backfilled")
    number = int(rem["pr_number"])
    opened: fmt.EventModel = (
        fmt.PrOpened(number=number)
        if rem["pr"] == "opened"
        else fmt.PrSameFix(number=number, commented=bool(rem.get("commented", True)))  # before the cap, it commented
    )
    out: list[tuple[str, datetime, fmt.EventModel]] = [("load", ran["load"][0], fmt.HandedOff()), ("open", ran["open"][1], opened)]
    polls = sorted(int(name.removeprefix("checks-")) for name in ran if re.fullmatch(r"checks-[1-9][0-9]?", name))
    for n in polls:
        state = rem.get("checks", "pending") if n == polls[-1] else "pending"
        out.append((f"checks-{n}", ran[f"checks-{n}"][1], fmt.PrChecks(check=CHECK, state=state, poll=n)))
    if rem["status"] == "ready":
        out.append(("conclude", ran["conclude"][1], fmt.PrReady(number=number)))
    return out


# --- reading AWS -----------------------------------------------------------------------


def read(session: Any, incident_id: str) -> Sources:
    ddb, logs, lam = session.client("dynamodb"), session.client("logs"), session.client("lambda")
    found = ddb.get_item(TableName=INCIDENTS_TABLE, Key={"pk": {"S": SERVICE}, "sk": {"S": f"incident#{incident_id}"}})
    item = found.get("Item")
    if item is None or "record_gz" not in item or "remediation" not in item:
        raise SystemExit(f"{incident_id}: no incident with a stored record and a remediation map")
    record = json.loads(gzip.decompress(item["record_gz"]["B"]))
    invoked, stored = _time(item["invoked_at"]["S"]), _time(item["finished_at"]["S"])

    groups = [g["logGroupName"] for g in logs.describe_log_groups(logGroupNamePrefix=RUNTIME_LOGS)["logGroups"]]
    if len(groups) != 1:
        raise SystemExit(f"{len(groups)} Runtime log groups; expected 1")
    lines = list(_log_lines(logs, groups[0], invoked - timedelta(seconds=1), stored + timedelta(seconds=2)))
    model_ends = [at for at, line in lines if line.get("event") == "model_call"]
    tool_starts = [at for at, line in lines if line.get("event") == "tool_call"]
    invocations = [at for at, line in lines if line.get("event") == "invocation"]
    if len(model_ends) != len(record["model_calls"]) or len(tool_starts) != len(record["tool_calls"]):
        raise SystemExit("the log lines don't match the record's calls: logs expired, or short (FilterLogEvents)?")
    if len(invocations) != 1:
        raise SystemExit(f"{len(invocations)} invocation lines in the run's window; expected 1")

    executions = lam.list_durable_executions_by_function(FunctionName=FLOW_FUNCTION, DurableExecutionName=incident_id)
    [execution] = executions["DurableExecutions"]
    flow_steps: dict[str, list[datetime]] = {}
    for event in _history(lam, execution["DurableExecutionArn"]):
        if event["EventType"] in ("StepStarted", "StepSucceeded"):
            flow_steps.setdefault(event["Name"], []).append(event["EventTimestamp"].astimezone(UTC))
    return Sources(
        record=record,
        invoked_at=invoked,
        model_ends=model_ends,
        tool_starts=tool_starts,
        finished_at=invocations[0],
        flow_steps={name: (times[0], times[-1]) for name, times in flow_steps.items()},
        remediation={k: _plain(v) for k, v in item["remediation"]["M"].items()},
    )


def _log_lines(logs: Any, group: str, start: datetime, end: datetime) -> Iterable[tuple[datetime, dict[str, Any]]]:
    request = {"logGroupName": group, "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)}
    while True:
        page = logs.filter_log_events(**request)
        for event in page.get("events", []):
            yield datetime.fromtimestamp(event["timestamp"] / 1000, UTC), _line(event["message"])
        if "nextToken" not in page:
            return
        request["nextToken"] = page["nextToken"]


def _line(message: str) -> dict[str, Any]:
    """A guard or app line: JSON, or JSON inside the JSON log format's message."""
    try:
        line = json.loads(message)
    except json.JSONDecodeError:
        return {}
    inner = line.get("message") if isinstance(line, dict) else None
    if isinstance(inner, str) and inner.startswith("{"):
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
    return line if isinstance(line, dict) else {}


def _history(lam: Any, arn: str) -> Iterable[dict[str, Any]]:
    request: dict[str, Any] = {"DurableExecutionArn": arn, "IncludeExecutionData": False}
    while True:
        page = lam.get_durable_execution_history(**request)
        yield from page.get("Events", [])
        if not page.get("NextMarker"):
            return
        request["Marker"] = page["NextMarker"]


def _plain(value: Mapping[str, Any]) -> Any:
    """A DynamoDB scalar as Python: the remediation map holds only these (remediation incidents._value)."""
    [(kind, inner)] = value.items()
    if kind == "N":
        return float(inner)
    if kind in ("S", "BOOL"):
        return inner
    return None


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


# --- the command ---------------------------------------------------------------------


def show(agent: list[Stamped], flow: list[tuple[str, datetime, fmt.EventModel]], sources: Sources, cleaner: Cleaner) -> None:
    for n, (at, event) in enumerate(agent, 1):
        print(f"{fmt.agent_key(n)}  {fmt.iso(at)}  {event.kind:<12} {_sizes(event)}")  # type: ignore[attr-defined]
    for step, at, event in flow:
        print(f"{fmt.flow_key(step)}     {fmt.iso(at)}  {event.kind:<12} {_sizes(event)}")  # type: ignore[attr-defined]
    print(f"{len(agent)} agent events, {len(flow)} flow events; {cleaner.withheld} texts withheld by the scan")
    for estimate in sources.estimates:
        print(f"  estimate: {estimate}")


def _sizes(event: fmt.EventModel) -> str:
    """Each field's size or value, never a text: numbers, flags, names."""
    shown = []
    for name, value in event.model_dump().items():
        if name == "kind":
            continue
        if isinstance(value, str) and name in ("text", "detail"):
            shown.append(f"{name}={len(value)} chars" + (" (withheld)" if value.startswith("[withheld") else ""))
        elif isinstance(value, dict):
            shown.append(f"{name}={sorted(value)}")
        else:
            shown.append(f"{name}={value}")
    return " ".join(shown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--incident", required=True, help="the incident's ID, like checkout-api-20261126T183624Z")
    parser.add_argument("--go", action="store_true", help="write the events; without it, a dry run")
    parser.add_argument("--profile", default="sentinelops")
    args = parser.parse_args(argv)
    if not fmt.INCIDENT_ID.fullmatch(args.incident) or not args.incident.startswith(f"{SERVICE}-"):
        parser.error("--incident must be a checkout-api incident's ID")
    account_id = commit_scan.account_id(str(REPO))
    if not account_id:
        raise SystemExit("No account ID in local git config: set it as the commit hooks' setup does (.githooks).")

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    sources = read(session, args.incident)
    model = sources.record["model"]
    cleaner = Cleaner(account_id)
    steps = Steps(cleaner, price_for(model["provider"], model["model_id"]))
    agent = agent_events(sources, steps)
    flow = flow_events(sources)
    show(agent, flow, sources, cleaner)
    if not args.go:
        print("Dry run: nothing written. Add --go to write these to sentinelops-trace, with no expiry.")
        return 0

    client = session.client("dynamodb")
    writer = Writer(client, TRACE_TABLE, args.incident, expires=False, background=False)
    writer.put(agent)
    counts = writer.close()
    for step, at, event in flow:
        fmt.put(client, TRACE_TABLE, args.incident, fmt.flow_key(step), at, event, expires=False)
    print(f"written: {counts['written']} agent events ({counts['failed']} failed, {counts['skipped']} skipped), {len(flow)} flow events")
    return 0 if counts["written"] == len(agent) else 1


if __name__ == "__main__":
    sys.exit(main())
