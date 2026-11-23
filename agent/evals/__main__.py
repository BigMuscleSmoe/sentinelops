"""The eval harness's commands (from agent/; see evals/__init__.py).

    uv run python -m evals plan [--runs N] [--arm haiku|sonnet] [--split tuning|test]
    uv run python -m evals sweep --go [--runs N] [--arm haiku] [--split tuning] [--scenario ID ...] [--resume DIR] [--pause 10]
    uv run python -m evals report DIR
    uv run python -m evals packet DIR --out PACKET.md [--run RUN ...]
    uv run python -m evals audit DIR --labels LABELS.json [--some]
    uv run python -m evals flags DIR --out FLAGS.json
    uv run python -m evals grade DIR --audit LABELS.json [--flags FLAGS.json]
    uv run python -m evals compare DIR --audit MINE.json --labels THEIRS.json

plan calls nothing: it replays each scenario's alarm from its window and prints
the runs and the cost bound. sweep without --go does the same and stops; with
it, the live runbook knowledge base is checked first (knowledge_base.connect),
and every run is a real model call, billed to Anthropic. Both take one split's
scenarios: the tuning split unless --split test, and a test sweep starts only
once the pass bar is frozen and the tree could be graded as it stands. The rest
call no model and no AWS: report gives a tuning sweep's outcomes, costs, rubric
failures, resends and trims, and runbook citations, never as a result; packet
writes what a labeler reads, with a blank label file beside it; audit checks a label file against the sweep, refusing a label that points
at nothing; flags writes every checker flag for the hand check; grade scores the
sweep against its committed audit and the pass bar; compare sets two labelers'
committed files side by side, by ID (R8).
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from alarm import AlarmEvent
from evals.arms import ARMS, DEFAULT_ARM, Arm
from evals.scenarios import BY_ID, SCENARIOS, Scenario, Split, load_window
from evals.sweep import SWEEPS, plan, run_id, run_sweep
from knowledge_base import NAME as KNOWLEDGE_BASE
from knowledge_base import KnowledgeBaseError
from knowledge_base import connect as connect_knowledge_base

EXIT_OK, EXIT_FAILED, EXIT_STOPPED = 0, 1, 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "sweep"):
        p = sub.add_parser(name)
        p.add_argument(
            "--runs",
            type=int,
            help="runs per scenario (default the arm's: "
            + ", ".join(f"{a.name} {a.runs_per_scenario}" for a in ARMS.values())
            + ")",
        )
        p.add_argument("--scenario", action="append", choices=sorted(BY_ID), help="only these (repeatable)")
        p.add_argument("--arm", choices=sorted(ARMS), default=DEFAULT_ARM, help=f"the agent's model and caps (default {DEFAULT_ARM})")
        p.add_argument(
            "--split",
            choices=[s.value for s in Split],
            default=Split.TUNING.value,
            help="which scenarios: tuning (default), or test, the held-out alarms a result is measured on",
        )
    sweep = sub.choices["sweep"]
    sweep.add_argument("--go", action="store_true", help="make the model calls; without it, only the plan")
    sweep.add_argument("--resume", type=Path, help="a sweep's folder: run what it hasn't, retrying its errors")
    sweep.add_argument("--pause", type=float, default=10.0, help="seconds between runs (default 10)")
    sweep.add_argument("--profile", default="sentinelops", help="the AWS profile for the sign-in")
    tuning = sub.add_parser("report")
    tuning.add_argument("sweep", type=Path)
    packet = sub.add_parser("packet")
    packet.add_argument("sweep", type=Path)
    packet.add_argument("--out", type=Path, required=True, help="the packet (.md); the blank labels go beside it (.labels.json)")
    packet.add_argument("--run", action="append", help="only these runs (repeatable); default every scored run")
    check = sub.add_parser("audit")
    check.add_argument("sweep", type=Path)
    check.add_argument("--labels", type=Path, required=True)
    check.add_argument("--some", action="store_true", help="labels for some runs only (calibration), not every one")
    flags = sub.add_parser("flags")
    flags.add_argument("sweep", type=Path)
    flags.add_argument("--out", type=Path, required=True, help="where to write every flag, each with a blank call")
    grade = sub.add_parser("grade")
    grade.add_argument("sweep", type=Path)
    grade.add_argument("--audit", type=Path, required=True, help="the blind hand audit, committed")
    grade.add_argument("--flags", type=Path, help="the hand check of every flag, committed; precision waits for it")
    compare = sub.add_parser("compare")
    compare.add_argument("sweep", type=Path)
    compare.add_argument("--audit", type=Path, required=True, help="my blind audit, committed")
    compare.add_argument("--labels", type=Path, required=True, help="the user's blind labels, committed")
    return parser.parse_args(argv)


def chosen(args: argparse.Namespace) -> list[Scenario]:
    """The split's scenarios, or those named, which must all be in it: a sweep is one split's."""
    split = Split(args.split)
    if not args.scenario:
        return [s for s in SCENARIOS if s.split is split]
    picked = [BY_ID[i] for i in args.scenario]
    other = [s.id for s in picked if s.split is not split]
    if other:
        raise ValueError(f"not in the {split.value} split: {', '.join(other)} (a sweep is one split's; --split picks it)")
    return picked


def show_plan(scenarios: list[Scenario], runs: int, arm: Arm = ARMS[DEFAULT_ARM], split: Split = Split.TUNING) -> int:
    """Replays every alarm from its window, offline, so a sweep can't start on a broken one."""
    if not scenarios:
        print(f"The {split.value} split has no scenarios yet.", file=sys.stderr)
        return EXIT_FAILED
    windows = {}
    for s in scenarios:
        window = windows.setdefault(s.window, load_window(s.window))
        alarm = AlarmEvent.as_of(window.client("cloudwatch"), s.alarm, s.fired_at)
        if alarm.fired_at != s.fired_at:
            print(f"{s.id}: the window replays {alarm.fired_at}, not {s.fired_at}", file=sys.stderr)
            return EXIT_FAILED
        print(
            f"{s.id:<21} {s.fault.value:<19} {s.split.value:<6} {'seen' if s.seen else 'unseen':<6} "
            f"{s.alarm} at {s.fired_at:%Y-%m-%d %H:%M:%S}Z"
        )
    todo = plan(scenarios, runs)
    bound = min(arm.cap_usd, len(todo) * arm.per_run_usd)
    print(
        f"\n{len(todo)} runs ({len(scenarios)} scenarios x {runs}) of the {split.value} split, one at a time, "
        f"on the {arm.name} arm ({arm.model_id}). "
        f"Expected about ${len(todo) * arm.expected_run_usd:.2f}; at most ${bound:.2f} "
        f"(${arm.per_run_usd:.2f} a run, ${arm.cap_usd:.2f} a sweep). The windows answer every read but the runbook "
        "search, which reads the live knowledge base: a fraction of a cent for the whole sweep (COSTS.md)."
    )
    if arm.blocked:
        print(f"The {arm.name} arm can't run yet: {arm.blocked}.")
    return EXIT_OK


def sweep(args: argparse.Namespace) -> int:
    scenarios, split = args.scenarios, Split(args.split)
    arm = ARMS[args.arm]
    if show_plan(scenarios, args.runs, arm, split) != EXIT_OK:
        return EXIT_FAILED
    if not args.go:
        print("\nNothing ran: add --go to make the model calls.")
        return EXIT_STOPPED
    if arm.blocked:
        print(f"Refused: the {arm.name} arm is blocked.", file=sys.stderr)
        return EXIT_FAILED
    if split is Split.TEST:
        # Before anything is spent: a test sweep that couldn't be graded as it would run now.
        from evals import score

        try:
            score.check_before_test_sweep()
        except score.Refusal as e:
            print(f"Refused: {e}", file=sys.stderr)
            return EXIT_FAILED

    import os

    import boto3
    import httpx

    from pricing import UnknownModelPrice, price_for
    from providers import Settings

    asked = os.environ.get("SENTINELOPS_MODEL_ID")
    if asked and asked != arm.model_id:
        print(f"Refused: the {arm.name} arm measures {arm.model_id}, and SENTINELOPS_MODEL_ID asks for {asked}.", file=sys.stderr)
        return EXIT_FAILED
    settings = arm.settings(Settings.from_env())
    try:
        price = price_for(settings.provider, settings.model_id)
    except UnknownModelPrice as e:
        print(f"Refused: {e}", file=sys.stderr)
        return EXIT_FAILED
    sign_in = boto3.Session(profile_name=args.profile, region_name=settings.region)
    try:
        # Before any folder or model call: the live runbooks must be this checkout's.
        runbooks = connect_knowledge_base(sign_in)
    except KnowledgeBaseError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return EXIT_FAILED
    print()
    print(f"runbooks: the knowledge base {KNOWLEDGE_BASE} holds this checkout's, corpus {runbooks.corpus}")
    out_dir = args.resume or SWEEPS / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ("" if arm.name == DEFAULT_ARM else f"-{arm.name}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(out_dir / "sweep.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("sentinelops")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    print(f"\nsweep {out_dir}")
    try:
        records = asyncio.run(
            run_sweep(
                scenarios,
                args.runs,
                settings=settings,
                price=price,
                sign_in=sign_in,
                out_dir=out_dir,
                api_transport=lambda: httpx.AsyncHTTPTransport(),
                arm=arm,
                knowledge_base=runbooks,
                pause_s=args.pause,
            )
        )
    except ValueError as e:  # a sweep resumed as another arm's or split's, or on another commit
        print(f"Refused: {e}", file=sys.stderr)
        return EXIT_FAILED
    wanted = {run_id(s, n) for s, n in plan(scenarios, args.runs)}
    scored = {r.run for r in records if r.scored}
    errors = [r for r in records if not r.scored]
    print(
        f"\n{len(scored & wanted)} of {len(wanted)} runs have an outcome; {len(errors)} error(s) filed; "
        f"${sum(r.cost_usd for r in records):.4f} spent. Transcripts: {out_dir}"
    )
    return EXIT_OK if scored >= wanted else EXIT_FAILED


def packet(args: argparse.Namespace) -> int:
    import json

    from evals import audit
    from evals import packet as packets
    from evals.score import load_sweep

    loaded = load_sweep(args.sweep)
    runs = args.run or [r["run"] for r in loaded.records]
    unknown = [run for run in runs if run not in loaded.transcripts]
    if unknown:
        print(f"Refused: not scored runs of {loaded.name}: {', '.join(unknown)}", file=sys.stderr)
        return EXIT_FAILED
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(packets.render(loaded.name, {run: loaded.transcripts[run] for run in runs}, audit.GUIDE), encoding="utf-8", newline="\n")
    blank = args.out.with_suffix(".labels.json")
    blank.write_text(json.dumps(audit.blank(loaded.name, runs), indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(runs)} runs: {args.out} and the blank labels, {blank}")
    return EXIT_OK


def check_labels(args: argparse.Namespace) -> int:
    from evals import audit
    from evals.score import load_sweep

    loaded = load_sweep(args.sweep)
    labels = audit.read(args.labels)
    wrong = audit.problems(labels, loaded.name, loaded.answers, every_run=not args.some)
    for line in wrong:
        print(line)
    print(f"{len(wrong)} problem(s) in {args.labels}" if wrong else f"{args.labels} fits sweep {loaded.name}")
    return EXIT_FAILED if wrong else EXIT_OK


def flag_list(args: argparse.Namespace) -> int:
    import json

    from evals import audit, repo
    from evals.score import load_sweep

    loaded = load_sweep(args.sweep)
    flags = {
        run: [{"id": c.id, "verdict": c.verdict.value, "reason": c.reason, "item": loaded.answers[run]["evidence"][int(c.id[1:]) - 1]} for c in check.flagged]
        for run, check in loaded.checks.items()
        if check
    }
    template = audit.flag_template(loaded.name, repo.head(), flags)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(template, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"{sum(len(f) for f in flags.values())} flags on {sum(bool(f) for f in flags.values())} runs: {args.out}")
    return EXIT_OK


def grade(args: argparse.Namespace) -> int:
    from evals import score

    try:
        report = score.score_sweep(args.sweep, args.audit, args.flags)
    except score.Refusal as e:
        print(f"Refused: {e}", file=sys.stderr)
        return EXIT_FAILED
    print(score.render(report))
    return EXIT_FAILED if report.verdict == "FAIL" else EXIT_OK


def tuning_report(args: argparse.Namespace) -> int:
    from evals import tuning

    if not (args.sweep / "sweep.json").is_file():
        print(f"Refused: {args.sweep} holds no sweep.json", file=sys.stderr)
        return EXIT_FAILED
    print(tuning.render(tuning.report(args.sweep)))
    return EXIT_OK


def compare(args: argparse.Namespace) -> int:
    from evals import audit, repo
    from evals.score import load_sweep

    loaded = load_sweep(args.sweep)
    try:
        for path in (args.audit, args.labels):
            repo.check_committed(path)
    except repo.Refusal as e:
        print(f"Refused: {e}", file=sys.stderr)
        return EXIT_FAILED
    mine, theirs = audit.read(args.audit), audit.read(args.labels)
    wrong = audit.problems(mine, loaded.name, loaded.answers) + audit.problems(theirs, loaded.name, loaded.answers, every_run=False)
    if wrong:
        print("Refused: the label files don't fit the sweep:\n  " + "\n  ".join(wrong), file=sys.stderr)
        return EXIT_FAILED
    print(audit.render_comparison(audit.compare(mine, theirs), "mine", "the user's"))
    return EXIT_OK


COMMANDS = {"sweep": sweep, "report": tuning_report, "packet": packet, "audit": check_labels, "flags": flag_list, "grade": grade, "compare": compare}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if args.command in ("plan", "sweep"):
        if args.runs is None:
            args.runs = ARMS[args.arm].runs_per_scenario
        try:
            args.scenarios = chosen(args)
        except ValueError as e:
            print(f"Refused: {e}", file=sys.stderr)
            return EXIT_FAILED
    if args.command == "plan":
        return show_plan(args.scenarios, args.runs, ARMS[args.arm], Split(args.split))
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
