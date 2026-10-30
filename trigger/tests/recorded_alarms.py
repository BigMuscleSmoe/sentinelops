"""Every change of checkout-api's alarms in two recorded windows
(tools/tests/fixtures/windows/), as triage receives them."""

from datetime import UTC, datetime

from triage import ALARM, OK, Change

POOL = "sentinelops-checkout-pool-wait"
LATENCY = "sentinelops-checkout-latency"
ERRORS = "sentinelops-checkout-errors"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# windows/2026-10-27: the pool fault from 18:56, then bad_deploy, broken at
# 19:05:22, 3 minutes after the pool fault's alarms had all cleared, with its
# errors alarm re-firing at 19:12:57.
OCT_27 = [
    Change(POOL, ALARM, at("2026-10-27T18:56:46.774")),
    Change(LATENCY, ALARM, at("2026-10-27T19:00:27.088")),
    Change(ERRORS, ALARM, at("2026-10-27T19:01:07.026")),
    Change(POOL, OK, at("2026-10-27T19:02:46.773")),
    Change(ERRORS, OK, at("2026-10-27T19:02:47.027")),
    Change(LATENCY, OK, at("2026-10-27T19:02:47.091")),
    Change(ERRORS, ALARM, at("2026-10-27T19:05:57.026")),
    Change(ERRORS, OK, at("2026-10-27T19:12:17.025")),
    Change(ERRORS, ALARM, at("2026-10-27T19:12:57.025")),
    Change(ERRORS, OK, at("2026-10-27T19:13:07.025")),
]

# windows/2026-10-29-live, the live bad_deploy test: the errors alarm, and its
# re-fire after the burst ended.
LIVE = [
    Change(ERRORS, ALARM, at("2026-10-29T01:56:07.026")),
    Change(ERRORS, OK, at("2026-10-29T02:02:07.024")),
    Change(ERRORS, ALARM, at("2026-10-29T02:03:07.025")),
    Change(ERRORS, OK, at("2026-10-29T02:03:17.025")),
]
