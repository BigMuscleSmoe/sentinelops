"""The scenarios: every alarm in the recorded windows, with what caused it.

The cause of each is known because the fault controller made it. `seen` marks
the alarms whose runs the first grader's rules were written on (the 12 saved
runs of step 1, kept at 57455ab): scores there are in-sample, and are reported
apart from the others.

`split` says what a scenario is for. The tuning split is every alarm the
checker, the rubric, and the answer's wording were tuned on: a sweep of it is
never a result. The test split is held out until the freeze (the grader
redesign's step 8) and run only after it. A sweep is one split's, and under a
frozen pass bar only a test-split sweep is graded.

The 11 test alarms of 2026-11-14 to 17 were spent by the test sweeps of
2026-11-18, and moved to tuning on 2026-11-23 (the user's call, 2026-11-22:
BUILD_PLAN.md, Phase 3, "Step 4 as split", decision 12), so the tuning
baseline pins downstream_timeout answers too. The test split is empty until
the evaluation cycle's test breaks.
"""

import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
WINDOW_MODULE = REPO / "tools" / "tests" / "window.py"
ERRORS = "sentinelops-checkout-errors"
LATENCY = "sentinelops-checkout-latency"
POOL_WAIT = "sentinelops-checkout-pool-wait"


class Fault(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    DB_POOL_EXHAUSTION = "db_pool_exhaustion"
    DOWNSTREAM_TIMEOUT = "downstream_timeout"


class Split(StrEnum):
    TUNING = "tuning"
    TEST = "test"


@dataclass(frozen=True)
class Scenario:
    id: str
    window: str
    alarm: str
    fired_at: datetime
    fault: Fault
    # When the change that caused it was recorded, HH:MM in UTC, as deploy history
    # shows it; None for a fault outside this service, which records no change.
    culprit_at: str | None
    seen: bool
    # When the fault began (the break), for a fault with no culprit record.
    onset: datetime | None = None
    # Stated on every row, so a new alarm joins no split by default.
    split: Split = field(kw_only=True)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


SCENARIOS = [
    Scenario("pool-1027-1856", "2026-10-27", POOL_WAIT, _at("2026-10-27T18:56:46.774Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False, split=Split.TUNING),
    Scenario("pool-1027-1900", "2026-10-27", LATENCY, _at("2026-10-27T19:00:27.088Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False, split=Split.TUNING),
    Scenario("pool-1027-1901", "2026-10-27", ERRORS, _at("2026-10-27T19:01:07.026Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False, split=Split.TUNING),
    Scenario("deploy-1027-1905", "2026-10-27", ERRORS, _at("2026-10-27T19:05:57.026Z"), Fault.BAD_DEPLOY, "19:05", seen=True, split=Split.TUNING),
    Scenario("deploy-1029-0156", "2026-10-29-live", ERRORS, _at("2026-10-29T01:56:07.026Z"), Fault.BAD_DEPLOY, "01:55", seen=True, split=Split.TUNING),
    Scenario("deploy-1112-0747", "2026-11-12-0747", ERRORS, _at("2026-11-12T07:47:05.004Z"), Fault.BAD_DEPLOY, "07:46", seen=False, split=Split.TUNING),
    Scenario("deploy-1112-0803", "2026-11-12-0747", ERRORS, _at("2026-11-12T08:03:45.006Z"), Fault.BAD_DEPLOY, "08:03", seen=False, split=Split.TUNING),
    Scenario("deploy-1112-2000", "2026-11-12-2000", ERRORS, _at("2026-11-12T20:00:45.006Z"), Fault.BAD_DEPLOY, "20:00", seen=True, split=Split.TUNING),
    Scenario("deploy-1113-0257", "2026-11-13-0257", ERRORS, _at("2026-11-13T02:57:45.007Z"), Fault.BAD_DEPLOY, "02:57", seen=False, split=Split.TUNING),
    # Held out (the user's call, 2026-11-14) until the test sweeps, then tuning, as the ten after it:
    # its window is downstream_timeout's measuring break, recorded with no replay, sweep, or tuning
    # on it before the freeze. The onset is the break's
    # T0, as the fault controller answered it; payments-api records no change, so no culprit.
    Scenario(
        "downstream-1114-2357", "2026-11-14-2357", LATENCY, _at("2026-11-14T23:57:37.083Z"), Fault.DOWNSTREAM_TIMEOUT, None,
        seen=False, onset=_at("2026-11-14T23:56:55.746Z"), split=Split.TUNING,
    ),
    # Test-break session A (the user's go, 2026-11-16), run and recorded after the freeze: the trigger
    # off, each fault its full 390 s, 100 minutes apart. A2's errors alarm went OK for 20 s and into
    # ALARM again at 21:02:26; its window ends before that, so the repeat is in the alarm history
    # only, as earlier windows keep their re-fires (the user's call).
    Scenario("deploy-1116-1917", "2026-11-16-1917", ERRORS, _at("2026-11-16T19:17:46.805Z"), Fault.BAD_DEPLOY, "19:17", seen=False, split=Split.TUNING),
    Scenario("pool-1116-2058", "2026-11-16-2058", POOL_WAIT, _at("2026-11-16T20:58:11.690Z"), Fault.DB_POOL_EXHAUSTION, "20:57", seen=False, split=Split.TUNING),
    Scenario("pool-1116-2101-lat", "2026-11-16-2058", LATENCY, _at("2026-11-16T21:01:38.818Z"), Fault.DB_POOL_EXHAUSTION, "20:57", seen=False, split=Split.TUNING),
    Scenario("pool-1116-2101-err", "2026-11-16-2058", ERRORS, _at("2026-11-16T21:01:46.804Z"), Fault.DB_POOL_EXHAUSTION, "20:57", seen=False, split=Split.TUNING),
    Scenario("deploy-1116-2238", "2026-11-16-2238", ERRORS, _at("2026-11-16T22:38:26.808Z"), Fault.BAD_DEPLOY, "22:37", seen=False, split=Split.TUNING),
    # Test-break session B (the user's go, 2026-11-17), run and recorded as session A was. B1's errors
    # alarm went OK for 10 s and into ALARM again at 02:35:46; its window ends before that, so the
    # repeat is in the alarm history only, as A2's. B2's onset is the break's T0, as the fault
    # controller answered it; payments-api records no change, so no culprit.
    Scenario("pool-1117-0231", "2026-11-17-0231", POOL_WAIT, _at("2026-11-17T02:31:21.684Z"), Fault.DB_POOL_EXHAUSTION, "02:30", seen=False, split=Split.TUNING),
    Scenario("pool-1117-0234", "2026-11-17-0231", LATENCY, _at("2026-11-17T02:34:48.814Z"), Fault.DB_POOL_EXHAUSTION, "02:30", seen=False, split=Split.TUNING),
    Scenario("pool-1117-0235", "2026-11-17-0231", ERRORS, _at("2026-11-17T02:35:26.805Z"), Fault.DB_POOL_EXHAUSTION, "02:30", seen=False, split=Split.TUNING),
    Scenario(
        "downstream-1117-0411", "2026-11-17-0411", LATENCY, _at("2026-11-17T04:11:48.817Z"), Fault.DOWNSTREAM_TIMEOUT, None,
        seen=False, onset=_at("2026-11-17T04:11:07.924Z"), split=Split.TUNING,
    ),
    Scenario("deploy-1117-0551", "2026-11-17-0551", ERRORS, _at("2026-11-17T05:51:46.805Z"), Fault.BAD_DEPLOY, "05:51", seen=False, split=Split.TUNING),
]
BY_ID = {s.id: s for s in SCENARIOS}

# What a report says beside the scores of any run on these windows (score.render).
WINDOW_NOTES = {
    "2026-11-14-2357": (
        "the rubric isn't blind to this window's distractor. Its earlier-incident rule (04d0479) was written "
        "after the window was recorded, knowing it holds the 22:28 bad_deploy break, 88 minutes before the "
        "onset. Read the rubric's diagnoses here beside the audit's hand diagnoses, which decide."
    ),
}


def window_module() -> Any:
    """tools/tests/window.py, the recorded windows' fakes, loaded by path: it isn't a package."""
    name = "sentinelops_eval_window"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, WINDOW_MODULE)
        if spec is None or spec.loader is None:
            raise ImportError(f"can't load {WINDOW_MODULE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def load_window(name: str) -> Any:
    return window_module().Window(name)
