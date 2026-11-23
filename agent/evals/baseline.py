"""The CI gate's baseline: a graded sweep's answers, what the model was sent, and the checker's verdicts.

Phase 3's step 2, closed as scoped (the user's call, 2026-11-18). CI calls no
model (decision 1), so the gate holds three things against the committed
baseline in `evals/gate/` (`tests/test_baseline_gate.py`):

- **What the model is sent.** It's the first request a run makes, captured
  through a mock transport that answers nothing and reaches no network: the
  system prompt and the alarm message, the tool specs, the answer tool's schema
  (as sent, strict), and the model settings (the rest of the request: model,
  max_tokens, thinking, and so on). A change to any of them fails CI until a new
  sweep's baseline lands with it. Strands builds the request, so an upgrade that
  changes it fails too. The order: commit the change; run a sweep on that commit,
  on the user's go (a tuning sweep, so the test alarms aren't re-run); build the
  baseline from it and commit that; push both commits together, since CI runs on
  the pushed head only.
  Not covered here: what a tool prints (pinned by `tools/tests/test_recorded_output.py`)
  and the guard's budget line on tool results.
- **The rubric on the committed answers.** Every answer's rubric items,
  recomputed with the current rubric and checker, must be as pinned, and every
  diagnosis right.
- **The checker's flags, pinned as a floor.** Every item's verdict must be as
  pinned, and each flagged item carries its hand call. At the test sweep's 26%
  strict recall (`evals/reports/2026-11-18-test-sweeps.md`), the flags are a floor
  under the typed slips, not a count of slips: the hand-audited rate is the
  measure.

Usage, from agent/ (no model, no AWS):

    uv run python -m evals.baseline check
    uv run python -m evals.baseline build scratch/evals/<sweep> [--flags evals/audits/<sweep>.flags.json]

`build` refuses a sweep that ran with uncommitted changes, one where anything
that shapes a run (`RUN_SHAPING`) differs now from its commit, so its runs
wouldn't be what a run sends and does now, and, strict for now (the user's
calls, 2026-11-18), one that isn't whole or isn't right: any run whose last
attempt didn't answer (halted, errored, refused, or no Hypothesis), any scenario
of its split it holds no run of, or any wrong diagnosis. A run retried after an
error counts by its last attempt, as the sweep scores it. What only scores a run
(`SCORING_ONLY`: the rubric, the checker) may change after the sweep (the
user's call, 2026-11-23): `build` checks and scores every run with the current
ones, and pins what they give.
"""

import argparse
import asyncio
import gzip
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from checker.__main__ import RunCheck, check_run
from checker.trial import final_records
from evals import audit as audits
from evals.arms import ARMS, DEFAULT_ARM, Arm
from evals.repo import REPO, Refusal, check_committed, unchanged
from evals.rubrics import score as rubric_score
from evals.scenarios import BY_ID, Split
from evals.score import load_sweep

GATE = Path(__file__).with_name("gate")
BASELINE = "baseline.json"
REQUEST = "request.json"
RUNS = "runs.json.gz"
ANSWER_TOOL = "Hypothesis"
PARTS = ("prompt", "tool_specs", "schema", "model_settings")

# A fixed alarm, so the captured alarm message is the template's alone.
GATE_ALARM = {
    "name": "sentinelops-checkout-errors",
    "fired_at": datetime(2026, 11, 16, 19, 17, 46, 805000, tzinfo=UTC),
    "reason": "Threshold Crossed: 2 out of the last 3 datapoints were greater than the threshold (0.05).",
    "description": "Over 5% of checkout-api requests fail with a 500",
    "condition": "error Average > 0.05 over 10 s periods, in 2 of 3 periods.",
}
FLAGGED = ("contradicted", "outside_read", "unresolved", "miscited")

# What shapes a run, from the repo's root: what the model is sent, the tools and
# what they read, and what stops a run. A sweep is a baseline only while these
# are as it ran on. Every pass-bar frozen path is here or in SCORING_ONLY, and so
# is every agent module a sweep's run imports (tests/test_baseline_gate.py).
RUN_SHAPING = (
    "agent/alarm.py",  # the alarm message
    "agent/evals/__init__.py",
    "agent/evals/__main__.py",  # the sweep command: its sign-in and the knowledge base's check
    "agent/evals/arms.py",  # the model, its settings and caps
    "agent/evals/scenarios.py",  # each run's alarm and window
    "agent/evals/sweep.py",
    "agent/evals/witness.py",  # the transport the model's traffic goes through
    "agent/gateway.py",  # the tools' client through Gateway, off a sweep
    "agent/guards.py",
    "agent/hooks.py",
    "agent/investigation.py",
    "agent/knowledge_base.py",
    "agent/models.py",  # the answer's schema
    "agent/pricing.py",  # the cost guard's prices
    "agent/prompts",
    "agent/providers.py",  # the model's settings and the strict answer tool
    "agent/pyproject.toml",
    "agent/runner.py",
    "agent/sources.py",  # the labels on tool results
    "agent/uv.lock",
    "runbooks",  # what a search returns
    "tools/build_runbooks.py",  # how the runbooks are cut into the knowledge base's sections
    "tools/ingest_runbooks.py",
    "tools/pyproject.toml",
    "tools/sentinelops_tools",  # the tools: their specs, reads, and text
    "tools/tests/fixtures/windows",  # what a sweep's tools read
    "tools/tests/window.py",
    "tools/uv.lock",
)
# Never what a run is sent or does: build checks and scores with the current ones.
SCORING_ONLY = (
    "agent/checker",
    "agent/evals/audit.py",
    "agent/evals/packet.py",
    "agent/evals/repo.py",  # the sweep's commit and uncommitted paths: its record, not its run
    "agent/evals/rubrics.py",
    "agent/evals/score.py",
)


def capture(arm: Arm = ARMS[DEFAULT_ARM]) -> dict[str, Any]:
    """What a run on this arm sends the model first, in its four parts; nothing is answered or billed."""
    # The run fails at its first call by design; its run_failed warning would only be noise here.
    runner_log = logging.getLogger("sentinelops.runner")
    level = runner_log.level
    runner_log.setLevel(logging.ERROR)
    try:
        return asyncio.run(_capture(arm))
    finally:
        runner_log.setLevel(level)


async def _capture(arm: Arm) -> dict[str, Any]:
    from anthropic.lib.credentials import StaticToken

    from alarm import AlarmEvent
    from guards import Limits
    from pricing import price_for
    from providers import Settings, build_model
    from runner import investigate
    from sentinelops_tools.env import ToolEnv
    from sentinelops_tools.toolset import make_tools

    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": len(request.content)})
        sent.append(json.loads(request.content))
        # A 400, which the SDK doesn't retry: the run ends at its first call, unanswered.
        return httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "the gate answers nothing"}}
        )

    settings = arm.settings(Settings())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    model = build_model(settings, http_client=client, credentials=StaticToken("the-gate-signs-in-to-nothing"))
    try:
        await investigate(
            AlarmEvent(**GATE_ALARM).prompt(),
            model=model,
            tools=make_tools(ToolEnv(logs=None, cloudwatch=None, dynamodb=None)),
            price=price_for(settings.provider, settings.model_id),
            max_output_tokens=settings.max_output_tokens,
            limits=Limits(max_cost_usd=arm.per_run_usd),
        )
    finally:
        await client.aclose()
    if not sent:
        raise RuntimeError("the run sent the model nothing")
    body = sent[0]
    tools = sorted(body.get("tools") or [], key=lambda t: t["name"])
    answer = [t for t in tools if t["name"] == ANSWER_TOOL]
    if len(answer) != 1:
        raise RuntimeError(f"the first request carries {len(answer)} {ANSWER_TOOL} tools, not 1")
    return {
        "prompt": {"system": body.get("system"), "messages": body.get("messages")},
        "tool_specs": [t for t in tools if t["name"] != ANSWER_TOOL],
        "schema": answer[0],
        "model_settings": {k: v for k, v in sorted(body.items()) if k not in ("system", "messages", "tools")},
    }


def changed_parts(now: dict[str, Any], pinned: dict[str, Any]) -> list[str]:
    return [part for part in PARTS if now.get(part) != pinned.get(part)]


def strip(transcript: dict[str, Any]) -> dict[str, Any]:
    """A transcript as the gate keeps it: the alarm, the answer, its trims, the eval's identity, and of
    the messages only the tool calls' names and the tool results' text. No thinking, model text, tool
    inputs, or costs (the checker's fixture keeps the same, `tests/checked_runs.py`)."""
    names: dict[str, str] = {}
    messages = []
    for message in transcript.get("messages", []):
        blocks = []
        for block in message.get("content", []):
            if "toolUse" in block:
                use = block["toolUse"]
                names[use["toolUseId"]] = use["name"]
                if use["name"] != ANSWER_TOOL:
                    blocks.append({"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}})
            elif "toolResult" in block:
                result = block["toolResult"]
                if names.get(result["toolUseId"]) == ANSWER_TOOL:
                    continue
                text = [{"text": c["text"]} for c in result.get("content", []) if "text" in c]
                blocks.append({"toolResult": {"toolUseId": result["toolUseId"], "content": text}})
        if blocks:
            messages.append({"role": message["role"], "content": blocks})
    alarm = transcript["alarm"]
    return {
        "alarm": {k: alarm[k] for k in ("name", "fired_at", "reason", "condition") if k in alarm},
        "eval": {k: transcript["eval"][k] for k in ("run", "scenario", "window", "fault")},
        "hypothesis": transcript["hypothesis"],
        "trims": transcript.get("trims") or [],
        "messages": messages,
    }


def verdicts(check: RunCheck) -> dict[str, str]:
    return {c.id: c.verdict.value for c in check.checks}


def rubric_items(run: dict[str, Any], check: RunCheck) -> dict[str, bool]:
    return dict(rubric_score(BY_ID[run["eval"]["scenario"]], run["hypothesis"], check.checks).items)


def diagnosis(items: dict[str, bool]) -> bool:
    return all(items[k] for k in ("names_culprit", "names_mechanism", "blames_no_other", "location"))


def build(folder: Path, flags_path: Path | None = None, out: Path = GATE) -> dict[str, Any]:
    """The baseline from a sweep, written to out; raises Refusal when it wouldn't be a fair one."""
    sweep = load_sweep(folder)
    meta = sweep.meta
    commit = meta.get("commit")
    if not commit:
        raise Refusal("the sweep doesn't record the commit it ran on")
    if meta.get("dirty"):
        raise Refusal(f"the sweep ran with uncommitted changes: {', '.join(meta['dirty'][:5])}")
    if not unchanged(commit, list(RUN_SHAPING)):
        raise Refusal(f"what shapes a run (evals.baseline.RUN_SHAPING) changed since the sweep ran on {commit[:7]}")
    arm = ARMS[meta.get("arm", DEFAULT_ARM)]

    # Whole: every run answered, by its last attempt (load_sweep drops errored runs, so read the records).
    final = final_records(meta)
    unanswered = sorted(f"{r['run']} ({r['status']})" for r in final if r["status"] != "answered")
    if unanswered:
        raise Refusal(f"a baseline holds a sweep whose every run answered; not answered: {', '.join(unanswered)}")
    unread = sorted(r["run"] for r in sweep.records if sweep.checks[r["run"]] is None)
    if unread:
        raise Refusal(f"answered runs with no transcript or no Hypothesis to check: {', '.join(unread)}")
    split = Split(meta.get("split", Split.TUNING.value))
    swept = {r["scenario"] for r in final}
    missing = sorted(s.id for s in BY_ID.values() if s.split is split and s.id not in swept)
    if missing:
        raise Refusal(f"a baseline holds every {split.value} scenario; the sweep has no run of {', '.join(missing)}")

    runs: dict[str, dict[str, Any]] = {}
    pins: dict[str, dict[str, Any]] = {}
    for record in sweep.records:
        name = record["run"]
        kept = strip(sweep.transcripts[name])
        check = check_run(name, kept)
        full = sweep.checks[name]
        assert full is not None
        if verdicts(check) != verdicts(full):
            raise Refusal(f"{name}: the kept transcript checks differently from the whole one")
        runs[name] = kept
        pins[name] = {"scenario": kept["eval"]["scenario"], "rubric": rubric_items(kept, check), "verdicts": verdicts(check)}
    wrong = [name for name, pin in pins.items() if not diagnosis(pin["rubric"])]
    if wrong:
        raise Refusal(f"a baseline holds right diagnoses only; the rubric fails {', '.join(wrong)}")

    flags = {name: {i: v for i, v in pin["verdicts"].items() if v in FLAGGED} for name, pin in pins.items()}
    flags = {name: by_id for name, by_id in flags.items() if by_id}
    calls_rel = None
    if flags_path is not None:
        calls = audits.read(flags_path)
        check_committed(flags_path)
        problems = audits.flag_problems(calls, sweep.name, flags)
        if problems:
            raise Refusal("the flag check doesn't cover the baseline's flags:\n  " + "\n  ".join(problems))
        for name, by_id in flags.items():
            for item_id in by_id:
                entry = calls["flags"][name][item_id]
                pins[name].setdefault("calls", {})[item_id] = {
                    "call": entry["call"],
                    "form_only": entry.get("note", "").startswith("form:"),
                }
        calls_rel = flags_path.resolve().relative_to(REPO).as_posix()

    n_flags = sum(len(by_id) for by_id in flags.values())
    called = [c for pin in pins.values() for c in (pin.get("calls") or {}).values()]
    floor: dict[str, Any] = {"runs": len(pins), "items": sum(len(p["verdicts"]) for p in pins.values()), "flags": n_flags}
    if calls_rel is not None:
        right = sum(c["call"] == "right" for c in called)
        form = sum(c["call"] == "right" and c["form_only"] for c in called)
        floor.update({"right": right, "form_only_right": form, "right_with_form_only_false": right - form})
    baseline = {
        "about": (
            "The CI gate's baseline (evals/baseline.py): the sweep's answered runs, each with its rubric items "
            "and every evidence item's checker verdict pinned, the flagged ones with their hand calls. The floor: "
            "the flags called right are typed slips the checker sees, a floor under the run's slips, not their "
            "count. request.json is what a run on this arm sends the model first; CI fails when it changes, "
            "until a new sweep's baseline is committed with the change."
        ),
        "sweep": sweep.name,
        "arm": arm.name,
        "model": arm.model_id,
        "commit": commit,
        "flags_checked": calls_rel,
        "floor": floor,
        "runs": pins,
    }
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / BASELINE, baseline)
    _write_json(out / REQUEST, capture(arm))
    data = json.dumps({"about": f"sweep {sweep.name}'s answered runs, kept by evals.baseline.strip", "runs": runs}, sort_keys=True)
    (out / RUNS).write_bytes(gzip.compress(data.encode("utf-8"), mtime=0))
    return baseline


def load(gate: Path = GATE) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """The committed baseline, the request it pins, and its kept runs."""
    baseline = json.loads((gate / BASELINE).read_text(encoding="utf-8"))
    request = json.loads((gate / REQUEST).read_text(encoding="utf-8"))
    runs = json.loads(gzip.decompress((gate / RUNS).read_bytes()))["runs"]
    return baseline, request, runs


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=True, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="what the model is sent, against the committed baseline")
    b = sub.add_parser("build", help="write evals/gate/ from a graded sweep")
    b.add_argument("sweep", type=Path)
    b.add_argument("--flags", type=Path, help="the sweep's committed hand check of its flags")
    args = parser.parse_args(argv)
    if args.command == "check":
        baseline, request, _ = load()
        changed = changed_parts(capture(ARMS[baseline["arm"]]), request)
        if changed:
            print(f"changed since sweep {baseline['sweep']}'s baseline: {', '.join(changed)}")
            return 1
        print(f"what the model is sent matches sweep {baseline['sweep']}'s baseline")
        return 0
    try:
        baseline = build(args.sweep, args.flags)
    except Refusal as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"wrote {GATE.relative_to(REPO).as_posix()}/ from sweep {baseline['sweep']}: {json.dumps(baseline['floor'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
