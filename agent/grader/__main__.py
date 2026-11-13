"""Grade saved transcripts: `uv run python -m grader <transcript.json> ...` (from agent/).

Prints each run's flagged claims, then a summary line per run. Reads files only:
no model, no AWS.
"""

import argparse
import json
import sys
from pathlib import Path

from grader import grade
from grader.checks import Verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m grader")
    parser.add_argument("transcripts", nargs="+", type=Path)
    parser.add_argument("--all", action="store_true", help="print every finding, not only the flagged and loose")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for path in args.transcripts:
        transcript = json.loads(path.read_text(encoding="utf-8"))
        result = grade(transcript)
        print(f"== {path.name}")
        for f in result.findings:
            if args.all or f.verdict != Verdict.SUPPORTED:
                print(f"  {f.verdict:<12} {f.check:<10} {f.where:<18} {f.text!r}: {f.detail}")
        for u in result.unparsed:
            print(f"  {'unparsed':<12} {'':<10} {u.where:<18} {u.text[:160]!r}")
        rows.append((path.name, result))
    print()
    print(f"{'run':<55} {'claims':>6} {'flagged':>7} {'loose':>5} {'unparsed':>8}")
    for name, result in rows:
        print(f"{name:<55} {len(result.findings):>6} {len(result.flagged):>7} {len(result.loose):>5} {len(result.unparsed):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
