"""The scenarios: every alarm in the recorded windows, with what caused it.

The cause of each is known because the fault controller made it. `seen` marks
the alarms whose runs the first grader's rules were written on (the 12 saved
runs of step 1, kept at 57455ab): scores there are in-sample, and are reported
apart from the others.
"""

import importlib.util
import sys
from dataclasses import dataclass
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


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


SCENARIOS = [
    Scenario("pool-1027-1856", "2026-10-27", POOL_WAIT, _at("2026-10-27T18:56:46.774Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False),
    Scenario("pool-1027-1900", "2026-10-27", LATENCY, _at("2026-10-27T19:00:27.088Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False),
    Scenario("pool-1027-1901", "2026-10-27", ERRORS, _at("2026-10-27T19:01:07.026Z"), Fault.DB_POOL_EXHAUSTION, "18:56", seen=False),
    Scenario("deploy-1027-1905", "2026-10-27", ERRORS, _at("2026-10-27T19:05:57.026Z"), Fault.BAD_DEPLOY, "19:05", seen=True),
    Scenario("deploy-1029-0156", "2026-10-29-live", ERRORS, _at("2026-10-29T01:56:07.026Z"), Fault.BAD_DEPLOY, "01:55", seen=True),
    Scenario("deploy-1112-0747", "2026-11-12-0747", ERRORS, _at("2026-11-12T07:47:05.004Z"), Fault.BAD_DEPLOY, "07:46", seen=False),
    Scenario("deploy-1112-0803", "2026-11-12-0747", ERRORS, _at("2026-11-12T08:03:45.006Z"), Fault.BAD_DEPLOY, "08:03", seen=False),
    Scenario("deploy-1112-2000", "2026-11-12-2000", ERRORS, _at("2026-11-12T20:00:45.006Z"), Fault.BAD_DEPLOY, "20:00", seen=True),
    Scenario("deploy-1113-0257", "2026-11-13-0257", ERRORS, _at("2026-11-13T02:57:45.007Z"), Fault.BAD_DEPLOY, "02:57", seen=False),
]
BY_ID = {s.id: s for s in SCENARIOS}


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
