"""The eval harness's commands (from agent/; see evals/__init__.py).

    uv run python -m evals plan [--runs 3]
    uv run python -m evals sweep --go [--runs 3] [--scenario ID ...] [--resume DIR] [--pause 10]
    uv run python -m evals grade DIR --audit LABELS.json [--recheck RECHECK.json]

plan calls nothing: it replays each scenario's alarm from its window and prints
the runs and the cost bound. sweep without --go does the same and stops; with
it, every run is a real model call, billed to Anthropic. grade refuses until the
grader redesign's step 5 rebuilds scoring on the checker (`evals.score`); until
then, `python -m checker` checks a sweep's runs.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from alarm import AlarmEvent
from evals.scenarios import BY_ID, SCENARIOS, Scenario, load_window
from evals.sweep import PER_RUN_USD, SWEEP_CAP_USD, SWEEPS, plan, run_id, run_sweep

EXPECTED_RUN_USD = 0.0071  # the 12 saved runs' mean (max 0.0100)
EXIT_OK, EXIT_FAILED, EXIT_STOPPED = 0, 1, 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--runs", type=int, default=3, help="runs per scenario (default 3)")
        p.add_argument("--scenario", action="append", choices=sorted(BY_ID), help="only these (repeatable)")
    sweep = sub.choices["sweep"]
    sweep.add_argument("--go", action="store_true", help="make the model calls; without it, only the plan")
    sweep.add_argument("--resume", type=Path, help="a sweep's folder: run what it hasn't, retrying its errors")
    sweep.add_argument("--pause", type=float, default=10.0, help="seconds between runs (default 10)")
    sweep.add_argument("--profile", default="sentinelops", help="the AWS profile for the sign-in")
    grade = sub.add_parser("grade")
    grade.add_argument("sweep", type=Path)
    grade.add_argument("--audit", type=Path, required=True, help="the hand audit, committed")
    grade.add_argument("--recheck", type=Path, help="the hand re-check of flags no label matched")
    return parser.parse_args(argv)


def chosen(args: argparse.Namespace) -> list[Scenario]:
    return [BY_ID[i] for i in args.scenario] if args.scenario else list(SCENARIOS)


def show_plan(scenarios: list[Scenario], runs: int) -> int:
    """Replays every alarm from its window, offline, so a sweep can't start on a broken one."""
    windows = {}
    for s in scenarios:
        window = windows.setdefault(s.window, load_window(s.window))
        alarm = AlarmEvent.as_of(window.client("cloudwatch"), s.alarm, s.fired_at)
        if alarm.fired_at != s.fired_at:
            print(f"{s.id}: the window replays {alarm.fired_at}, not {s.fired_at}", file=sys.stderr)
            return EXIT_FAILED
        print(f"{s.id:<18} {s.fault.value:<19} {'seen' if s.seen else 'unseen':<6} {s.alarm} at {s.fired_at:%Y-%m-%d %H:%M:%S}Z")
    todo = plan(scenarios, runs)
    bound = min(SWEEP_CAP_USD, len(todo) * PER_RUN_USD)
    print(
        f"\n{len(todo)} runs ({len(scenarios)} scenarios x {runs}), one at a time. "
        f"Expected about ${len(todo) * EXPECTED_RUN_USD:.2f}; at most ${bound:.2f} "
        f"(${PER_RUN_USD:.2f} a run, ${SWEEP_CAP_USD:.2f} a sweep). No AWS cost: the windows answer every read."
    )
    return EXIT_OK


def sweep(args: argparse.Namespace) -> int:
    scenarios = chosen(args)
    if show_plan(scenarios, args.runs) != EXIT_OK:
        return EXIT_FAILED
    if not args.go:
        print("\nNothing ran: add --go to make the model calls.")
        return EXIT_STOPPED

    import boto3
    import httpx

    from pricing import price_for
    from providers import DEFAULT_MODEL_IDS, Settings

    settings = Settings.from_env()
    if settings.model_id != DEFAULT_MODEL_IDS["anthropic"]:
        print(f"Refused: the sweep measures {DEFAULT_MODEL_IDS['anthropic']}, and the settings ask for {settings.model_id}.", file=sys.stderr)
        return EXIT_FAILED
    price = price_for(settings.provider, settings.model_id)
    out_dir = args.resume or SWEEPS / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(out_dir / "sweep.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("sentinelops")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    print(f"\nsweep {out_dir}")
    records = asyncio.run(
        run_sweep(
            scenarios,
            args.runs,
            settings=settings,
            price=price,
            sign_in=boto3.Session(profile_name=args.profile, region_name=settings.region),
            out_dir=out_dir,
            api_transport=lambda: httpx.AsyncHTTPTransport(),
            pause_s=args.pause,
        )
    )
    wanted = {run_id(s, n) for s, n in plan(scenarios, args.runs)}
    scored = {r.run for r in records if r.scored}
    errors = [r for r in records if not r.scored]
    print(
        f"\n{len(scored & wanted)} of {len(wanted)} runs have an outcome; {len(errors)} error(s) filed; "
        f"${sum(r.cost_usd for r in records):.4f} spent. Transcripts: {out_dir}"
    )
    return EXIT_OK if scored >= wanted else EXIT_FAILED


def grade(args: argparse.Namespace) -> int:
    from evals.score import RETIRED

    print(f"Refused: {RETIRED}. To check a sweep's runs now: uv run python -m checker {args.sweep}/*.json", file=sys.stderr)
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if args.command == "plan":
        return show_plan(chosen(args), args.runs)
    if args.command == "sweep":
        return sweep(args)
    return grade(args)


if __name__ == "__main__":
    raise SystemExit(main())
