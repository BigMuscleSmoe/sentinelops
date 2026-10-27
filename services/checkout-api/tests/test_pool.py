import logging
import random

import pytest

from checkout.config import Config
from sim.backend import Backend
from sim.pool import (
    BUCKETS,
    MAX_ATTEMPTS,
    DynamoLedger,
    PoolState,
    Reservation,
    applied,
    holds,
    plan_reservation,
    reserved,
)

NOW = 1_800_000_000.25
SECOND = int(NOW)


def plan(pool=PoolState(), max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0, now=NOW):
    return plan_reservation(pool, now, max_size, timeout_s, hold_s, after_release_s, random.Random(0))


def busy(*free_at: float) -> PoolState:
    return PoolState(slots=dict(enumerate(free_at)))


# Planning


def test_an_idle_slot_is_taken_at_once():
    p = plan(busy(NOW + 1.0, NOW - 1.0))
    assert (p.slot, p.queue, p.release_at) == (1, False, NOW + 0.1)
    r = reserved(p, None)
    assert (r.wait_s, r.timed_out, r.active_connections) == (0.0, False, 2)


def test_a_slot_never_used_counts_as_idle():
    assert plan(busy(NOW + 1.0)).slot == 1


def test_with_every_slot_busy_it_queues_on_the_first_to_free():
    p = plan(busy(NOW + 0.5, NOW + 0.3))
    assert (p.slot, p.queue) == (1, True)
    assert p.release_at == pytest.approx(NOW + 0.4)
    r = reserved(p, None)
    assert r.wait_s == pytest.approx(0.3)
    assert r.active_connections == 2


def test_it_gives_up_when_even_the_first_slot_to_free_is_too_late():
    p = plan(busy(NOW + 2.0, NOW + 1.6))
    assert p.slot is None
    r = reserved(p, None)
    assert (r.timed_out, r.wait_s, r.release_at, r.end_at) == (True, 1.5, None, NOW + 1.5)


def test_a_backlog_exactly_at_the_timeout_still_waits():
    p = plan(busy(NOW + 1.5, NOW + 1.6))
    assert (p.slot, p.queue) == (0, True)


def test_slots_beyond_the_pool_size_are_ignored():
    # The pool was 20 and is now 2: slot 5 is idle but no longer exists.
    p = plan(PoolState(slots={0: NOW + 0.5, 1: NOW + 0.3, 5: NOW - 9.0}))
    assert (p.slot, p.queue) == (1, True)


def test_counts_requests_in_flight_from_the_last_few_seconds():
    pool = PoolState(
        ends={
            SECOND % BUCKETS: (SECOND, (NOW + 1.0, NOW - 0.1)),  # one still running
            (SECOND - 3) % BUCKETS: (SECOND - 3, (NOW + 0.5,)),
            (SECOND - 5) % BUCKETS: (SECOND - BUCKETS - 5, (NOW + 9.0,)),  # a stale lap of the ring
        }
    )
    p = plan(pool)
    assert p.concurrent_requests == 3  # two others, and this one
    assert not p.fresh_bucket


def test_the_first_request_in_a_second_starts_its_bucket_over():
    stale = PoolState(ends={SECOND % BUCKETS: (SECOND - BUCKETS, (NOW - 7.0,))})
    assert plan(stale).fresh_bucket
    assert plan(PoolState()).fresh_bucket


def test_a_request_with_an_upstream_call_stays_in_flight_after_its_release():
    p = plan(busy(NOW + 0.3, NOW + 0.5), after_release_s=0.08)
    assert p.end_at == pytest.approx(NOW + 0.3 + 0.1 + 0.08)


# The write condition, and what the write does


def test_an_idle_claim_loses_if_somebody_took_the_slot():
    p = plan(busy(NOW + 1.0, NOW - 1.0))
    assert holds(p, busy(NOW + 1.0, NOW - 1.0))
    assert not holds(p, busy(NOW + 1.0, NOW + 0.2))


def test_a_queued_request_still_joins_a_grown_backlog_that_fits():
    p = plan(busy(NOW + 0.5, NOW + 0.3))
    assert holds(p, busy(NOW + 0.5, NOW + 1.4))
    assert not holds(p, busy(NOW + 0.5, NOW + 1.6))


def test_the_bucket_must_still_be_as_the_request_saw_it():
    fresh = plan(PoolState())
    started = PoolState(ends={SECOND % BUCKETS: (SECOND, (NOW + 1.0,))})
    assert holds(fresh, PoolState())
    assert not holds(fresh, started)  # somebody else started this second's bucket first
    assert not holds(plan(started), PoolState())


def test_queueing_adds_to_the_slot_as_it_is_now():
    p = plan(busy(NOW + 0.5, NOW + 0.3))
    after, release_at = applied(p, busy(NOW + 0.5, NOW + 0.45))
    assert release_at == pytest.approx(NOW + 0.55)
    assert after.slots[1] == pytest.approx(NOW + 0.55)
    assert reserved(p, release_at).wait_s == pytest.approx(0.45)


def test_two_requests_racing_for_one_idle_slot_never_share_it():
    seen = busy(NOW + 1.0, NOW - 1.0)
    first, second = plan(seen), plan(seen)
    pool, _ = applied(first, seen)
    assert not holds(second, pool)
    retry = plan(pool)  # planned again from the item the failed update returned
    assert holds(retry, pool)
    pool, release_at = applied(retry, pool)
    assert retry.queue and release_at == pytest.approx(NOW + 0.2)


def test_end_times_go_into_the_bucket_for_the_second():
    pool, _ = applied(plan(PoolState()), PoolState(ends={SECOND % BUCKETS: (SECOND - BUCKETS, (1.0,))}))
    assert pool.ends[SECOND % BUCKETS] == (SECOND, (NOW + 0.1,))
    pool, _ = applied(plan(pool), pool)
    assert pool.ends[SECOND % BUCKETS] == (SECOND, (NOW + 0.1, NOW + 0.1))


# DynamoLedger, against a stub client


class ClientError(Exception):
    """Shaped like botocore's: the error code and, with ALL_OLD, the item."""

    def __init__(self, code: str, item: dict | None = None) -> None:
        super().__init__(code)
        self.response: dict = {"Error": {"Code": code}}
        if item is not None:
            self.response["Item"] = item


class StubDynamo:
    def __init__(self, item: dict | None = None, updates: list | None = None) -> None:
        self.item = item
        self.updates = list(updates or [])  # each an exception to raise or a slot value to return
        self.calls: list[tuple[str, dict]] = []

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        if isinstance(self.item, Exception):
            raise self.item
        return {} if self.item is None else {"Item": self.item}

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        return {}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        outcome = self.updates.pop(0) if self.updates else None
        if isinstance(outcome, Exception):
            raise outcome
        attributes = {}
        if "#slots" in kwargs["ExpressionAttributeNames"]:
            slot = kwargs["ExpressionAttributeNames"]["#i"]
            attributes["slots"] = {"M": {slot: {"N": str(outcome)}}}
        return {"Attributes": attributes}

    def named(self, name: str) -> list[dict]:
        return [kwargs for call, kwargs in self.calls if call == name]


def item(slots: dict[int, float], ends: dict[int, tuple[int, list[float]]] | None = None) -> dict:
    return {
        "pool": {"S": "checkout-api"},
        "slots": {"M": {str(slot): {"N": f"{at:.3f}"} for slot, at in slots.items()}},
        "ends": {
            "M": {
                str(bucket): {"M": {"s": {"N": str(second)}, "e": {"L": [{"N": f"{t:.3f}"} for t in times]}}}
                for bucket, (second, times) in (ends or {}).items()
            }
        },
    }


def ledger(stub: StubDynamo) -> DynamoLedger:
    return DynamoLedger("sentinelops-checkout-pool", stub, lambda: NOW, random.Random(0))


def test_first_use_creates_the_item_then_takes_an_idle_slot():
    stub = StubDynamo(updates=[NOW + 0.1])
    r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)

    assert [call for call, _ in stub.calls] == ["get_item", "put_item", "update_item"]
    (get,) = stub.named("get_item")
    assert get["ConsistentRead"] is True
    (put,) = stub.named("put_item")
    assert put["Item"]["slots"] == {"M": {}} and put["Item"]["ends"] == {"M": {}}
    assert put["ConditionExpression"] == "attribute_not_exists(#p)"
    (update,) = stub.named("update_item")
    assert update["UpdateExpression"] == "SET #ends.#b = :bucket, #slots.#i = :release"
    assert update["ConditionExpression"] == (
        "(attribute_not_exists(#ends.#b) OR #ends.#b.#s <> :second)"
        " AND (attribute_not_exists(#slots.#i) OR #slots.#i <= :now)"
    )
    assert update["ReturnValues"] == "UPDATED_NEW"
    assert update["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"
    assert update["ExpressionAttributeValues"][":release"] == {"N": "1800000000.350"}
    assert update["ExpressionAttributeValues"][":second"] == {"N": str(SECOND)}
    assert (r.planned_at, r.timed_out, r.release_at, r.end_at) == (NOW, False, NOW + 0.1, NOW + 0.1)
    assert r.wait_s == pytest.approx(0.0, abs=1e-6)
    assert (r.active_connections, r.concurrent_requests) == (1, 1)


def test_queues_on_a_busy_slot_with_an_atomic_add():
    stub = StubDynamo(
        item=item({0: NOW + 0.4, 1: NOW + 0.2}, {SECOND % BUCKETS: (SECOND, [NOW + 0.3])}),
        updates=[NOW + 0.45],  # somebody else queued on slot 1 in the meantime
    )
    r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)

    (update,) = stub.named("update_item")
    assert update["UpdateExpression"] == (
        "SET #ends.#b.#e = list_append(#ends.#b.#e, :end), #slots.#i = #slots.#i + :hold"
    )
    assert update["ConditionExpression"] == "#ends.#b.#s = :second AND #slots.#i <= :deadline"
    assert update["ExpressionAttributeNames"]["#i"] == "1"
    assert update["ExpressionAttributeValues"][":deadline"] == {"N": "1800000001.750"}
    assert r.release_at == NOW + 0.45  # the slot's value after the add, not the plan's
    assert r.wait_s == pytest.approx(0.35)
    assert (r.active_connections, r.concurrent_requests) == (2, 2)


def test_a_lost_race_plans_again_from_the_item_the_update_returned():
    taken = item({0: NOW + 0.4, 1: NOW + 0.3})
    stub = StubDynamo(
        item=item({0: NOW + 0.4, 1: NOW - 1.0}),
        updates=[ClientError("ConditionalCheckFailedException", taken), NOW + 0.4],
    )
    r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)

    first, second = stub.named("update_item")
    assert first["ExpressionAttributeNames"]["#i"] == "1"
    assert first["UpdateExpression"].endswith("#slots.#i = :release")
    assert second["ExpressionAttributeNames"]["#i"] == "1"
    assert second["UpdateExpression"].endswith("#slots.#i = #slots.#i + :hold")
    assert len(stub.named("get_item")) == 1  # the failed update's ALL_OLD item stood in for a read
    assert r.wait_s == pytest.approx(0.3)


def test_a_request_that_gives_up_records_only_its_end():
    stub = StubDynamo(item=item({0: NOW + 2.0, 1: NOW + 1.6}))
    r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)

    (update,) = stub.named("update_item")
    assert "#slots" not in update["ExpressionAttributeNames"]
    assert update["UpdateExpression"] == "SET #ends.#b = :bucket"
    assert update["ExpressionAttributeValues"][":bucket"]["M"]["e"] == {"L": [{"N": "1800000001.750"}]}
    assert (r.timed_out, r.wait_s) == (True, 1.5)


def test_fails_open_when_the_item_cannot_be_read(caplog):
    stub = StubDynamo(item=ClientError("ProvisionedThroughputExceededException"))
    with caplog.at_level(logging.WARNING, logger="checkout.pool"):
        r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)
    assert (r.wait_s, r.timed_out) == (0.0, False)
    assert not stub.named("update_item")
    assert "read failed (ClientError); treating the pool as idle" in caplog.text
    assert not any(record.exc_info for record in caplog.records)  # no traceback, so no file paths


def test_goes_ahead_unrecorded_when_the_update_fails(caplog):
    stub = StubDynamo(item=item({0: NOW + 0.4, 1: NOW + 0.2}), updates=[ClientError("InternalServerError")])
    with caplog.at_level(logging.WARNING, logger="checkout.pool"):
        r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)
    assert len(stub.named("update_item")) == 1
    assert r.wait_s == pytest.approx(0.2)  # as planned
    assert "write failed (ClientError); going ahead unrecorded" in caplog.text
    assert not any(record.exc_info for record in caplog.records)


def test_stops_after_max_attempts(caplog):
    taken = item({0: NOW + 0.4, 1: NOW + 0.2})
    stub = StubDynamo(item=taken, updates=[ClientError("ConditionalCheckFailedException", taken)] * MAX_ATTEMPTS)
    with caplog.at_level(logging.WARNING, logger="checkout.pool"):
        r = ledger(stub).reserve(max_size=2, timeout_s=1.5, hold_s=0.1, after_release_s=0.0)
    assert len(stub.named("update_item")) == MAX_ATTEMPTS
    assert not r.timed_out
    assert "lost a race" in caplog.text


# Backend


class RecordingLedger:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float, float, float]] = []

    def reserve(self, max_size, timeout_s, hold_s, after_release_s):
        self.calls.append((max_size, timeout_s, hold_s, after_release_s))
        return Reservation(NOW, 0.0, False, NOW + hold_s, NOW + hold_s + after_release_s, 1, 1)


def test_a_checkout_stays_in_flight_until_payments_answers():
    led = RecordingLedger()
    backend = Backend(led, random.Random(3))
    product = backend.connect("GET /products/{id}", Config())
    checkout = backend.connect("POST /checkout", Config())

    assert product.upstream_s is None and led.calls[0][3] == 0.0
    assert checkout.upstream_s is not None and led.calls[1][3] == checkout.upstream_s
    assert led.calls[1][:2] == (20, 1.5)


def test_a_checkout_whose_payment_times_out_is_in_flight_until_the_timeout():
    led = RecordingLedger()
    checkout = Backend(led, random.Random(3)).connect("POST /checkout", Config(payments_timeout_s=0.001))
    assert checkout.upstream_s > 0.001
    assert led.calls[0][3] == 0.001
