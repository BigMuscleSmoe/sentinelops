"""The connection pool and its shared ledger.

Lambda runs one request per execution environment, so a pool kept in process
memory never fills: 50 concurrent requests would get 50 private pools of 20.
Instead, the pool lives in one DynamoDB item that every instance shares. For
each connection slot it holds the time the slot next comes free: when its
holder, and everyone queued behind it, will be done with it.

A request reads the item and picks a slot: any idle one, or else the one that
frees first. It takes the slot with a single atomic update. An idle slot is
set to the request's release time, on condition it is still idle. A busy one
has the request's hold added to it, on condition its backlog still fits in the
acquire timeout. If the condition fails, the update returns the item as it is
now and the request picks again. Joining the slot that frees first gives each
request the place a single first-in-first-out queue would; a request racing
another may end up on a slightly worse slot, but two requests can never hold
the same slot at once, and nobody waits on anyone else's round trip.

Two simpler ledgers failed in the harness (sim/pool_sim.py). An unconditional
read-then-write lets requests that arrive within a round trip of each other
take the same connection, which at burst rates hides the fault entirely. A
compare-and-swap of the whole item is exact, but every request then waits out
the others' round trips, and the ledger, not the pool, becomes the bottleneck.

The item also keeps, for the last few seconds, when each request in flight
will finish, which is what concurrent_requests counts. Nothing is ever
decremented, so a crashed request can't leak a connection: its times pass.
"""

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("checkout.pool")

POOL_ID = "checkout-api"
BUCKETS = 8  # seconds of end times kept, in a ring; no request lasts more than about 5 s
MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class PoolState:
    """The ledger item."""

    slots: Mapping[int, float] = field(default_factory=dict)  # slot -> when it next comes free
    # bucket (second % BUCKETS) -> (that second, end times of the requests that started in it)
    ends: Mapping[int, tuple[int, tuple[float, ...]]] = field(default_factory=dict)

    def free_at(self, slot: int) -> float:
        return self.slots.get(slot, 0.0)


@dataclass(frozen=True)
class Plan:
    """What a request writes, given the pool as it saw it at `now`."""

    now: float
    hold_s: float
    deadline: float  # it gives up waiting for a connection at this time
    slot: int | None  # None: even the first slot to free is too late; it takes none
    queue: bool  # joins a busy slot, rather than taking an idle one
    release_at: float | None  # as planned; a queued request learns its real one from the write
    end_at: float
    fresh_bucket: bool  # its second's bucket still holds an older second
    active_connections: int
    concurrent_requests: int

    @property
    def second(self) -> int:
        return int(self.now)

    @property
    def bucket(self) -> int:
        return self.second % BUCKETS


@dataclass(frozen=True)
class Reservation:
    """A request's place in the pool."""

    planned_at: float
    wait_s: float  # the acquire timeout, if timed_out
    timed_out: bool
    release_at: float | None  # when its connection goes back; None if it never got one
    end_at: float
    active_connections: int  # connections in use once this request has its own
    concurrent_requests: int  # requests in flight, this one included


class Ledger(Protocol):
    def reserve(
        self, max_size: int, timeout_s: float, hold_s: float, after_release_s: float
    ) -> Reservation: ...


def plan_reservation(
    pool: PoolState,
    now: float,
    max_size: int,
    timeout_s: float,
    hold_s: float,
    after_release_s: float,
    rng: random.Random,
) -> Plan:
    """Where a request goes in the pool it found at `now`. It holds a connection for
    hold_s, then stays in flight for after_release_s, waiting on an upstream."""
    free = [pool.free_at(slot) for slot in range(max_size)]
    idle = [slot for slot, at in enumerate(free) if at <= now]
    second = int(now)
    stamp = pool.ends.get(second % BUCKETS, (None, ()))[0]
    in_flight = sum(
        1
        for started, ends in pool.ends.values()
        if second - BUCKETS < started <= second
        for end in ends
        if end > now
    )
    common = {
        "now": now,
        "hold_s": hold_s,
        "deadline": now + timeout_s,
        "fresh_bucket": stamp != second,
        "active_connections": min(max_size - len(idle) + 1, max_size),
        "concurrent_requests": in_flight + 1,
    }
    if idle:  # random, so requests racing for an idle slot rarely pick the same one
        release_at = now + hold_s
        slot, queue = rng.choice(idle), False
    else:
        slot = min(range(max_size), key=free.__getitem__)
        if free[slot] > now + timeout_s:
            return Plan(slot=None, queue=False, release_at=None, end_at=now + timeout_s, **common)
        release_at = free[slot] + hold_s
        queue = True
    return Plan(
        slot=slot, queue=queue, release_at=release_at, end_at=release_at + after_release_s, **common
    )


def holds(plan: Plan, pool: PoolState) -> bool:
    """Whether the plan's write condition holds against the pool as it is now.
    The same condition DynamoLedger sends with the update."""
    stamp = pool.ends.get(plan.bucket, (None, ()))[0]
    if (stamp != plan.second) != plan.fresh_bucket:
        return False
    if plan.slot is None:
        return True
    if plan.queue:
        return plan.slot in pool.slots and pool.slots[plan.slot] <= plan.deadline
    return pool.free_at(plan.slot) <= plan.now


def applied(plan: Plan, pool: PoolState) -> tuple[PoolState, float | None]:
    """The pool after the plan's update, and the release time the update gave it."""
    slots = dict(pool.slots)
    release_at = None
    if plan.slot is not None:
        release_at = pool.free_at(plan.slot) + plan.hold_s if plan.queue else plan.release_at
        slots[plan.slot] = release_at
    ends = dict(pool.ends)
    previous = () if plan.fresh_bucket else ends.get(plan.bucket, (plan.second, ()))[1]
    ends[plan.bucket] = (plan.second, (*previous, plan.end_at))
    return PoolState(slots, ends), release_at


def reserved(plan: Plan, release_at: float | None) -> Reservation:
    """The reservation a plan made, given the release time its update returned
    (None: the plan was never recorded, so it stands as planned)."""
    if plan.slot is None:
        return Reservation(
            planned_at=plan.now,
            wait_s=plan.deadline - plan.now,
            timed_out=True,
            release_at=None,
            end_at=plan.end_at,
            active_connections=plan.active_connections,
            concurrent_requests=plan.concurrent_requests,
        )
    release = plan.release_at if release_at is None else release_at
    return Reservation(
        planned_at=plan.now,
        wait_s=max(0.0, release - plan.hold_s - plan.now),
        timed_out=False,
        release_at=release,
        end_at=plan.end_at,
        active_connections=plan.active_connections,
        concurrent_requests=plan.concurrent_requests,
    )


class DynamoLedger:
    """The ledger as one DynamoDB item: a read, then usually one conditional update.

    Fails open. If the item can't be read or written, the request goes ahead as
    if the pool were idle, so a ledger problem can never pass for pool exhaustion.
    Its warnings name only the exception's type: a traceback would print this
    file's path into the service's logs, and the simulation must never show there.
    """

    def __init__(
        self, table: str, client: Any, clock: Callable[[], float], rng: random.Random
    ) -> None:
        self._table = table
        self._client = client
        self._clock = clock
        self._rng = rng

    @classmethod
    def from_env(cls, env: Mapping[str, str], clock: Callable[[], float]) -> "DynamoLedger":
        import boto3  # provided by the Lambda runtime; tests pass a stub client
        from botocore.config import Config

        client = boto3.client(
            "dynamodb",
            config=Config(
                connect_timeout=1, read_timeout=1, retries={"max_attempts": 2, "mode": "standard"}
            ),
        )
        return cls(env["POOL_TABLE"], client, clock, random.Random())

    def reserve(
        self, max_size: int, timeout_s: float, hold_s: float, after_release_s: float
    ) -> Reservation:
        now = self._clock()
        args = (now, max_size, timeout_s, hold_s, after_release_s, self._rng)
        try:
            pool = self._read()
        except Exception as exc:
            logger.warning("pool ledger read failed (%s); treating the pool as idle", type(exc).__name__)
            return reserved(plan_reservation(PoolState(), *args), None)
        for _ in range(MAX_ATTEMPTS):
            plan = plan_reservation(pool, *args)
            try:
                response = self._client.update_item(**self._update(plan))
            except Exception as exc:
                if _error_code(exc) == "ConditionalCheckFailedException" and "Item" in exc.response:
                    pool = _pool(exc.response["Item"])  # somebody got there first; plan again from what they left
                    continue
                logger.warning("pool ledger write failed (%s); going ahead unrecorded", type(exc).__name__)
                return reserved(plan, None)
            if plan.slot is None:
                return reserved(plan, None)
            slots = response["Attributes"]["slots"]["M"]
            return reserved(plan, float(slots[str(plan.slot)]["N"]))
        logger.warning("pool ledger: %d updates in a row lost a race; going ahead unrecorded", MAX_ATTEMPTS)
        return reserved(plan, None)

    def _read(self) -> PoolState:
        key = {"pool": {"S": POOL_ID}}
        item = self._client.get_item(TableName=self._table, Key=key, ConsistentRead=True).get("Item")
        if item is None:  # first use: nested updates need the parent maps to exist
            try:
                self._client.put_item(
                    TableName=self._table,
                    Item={**key, "slots": {"M": {}}, "ends": {"M": {}}},
                    ConditionExpression="attribute_not_exists(#p)",
                    ExpressionAttributeNames={"#p": "pool"},
                )
            except Exception as exc:
                if _error_code(exc) != "ConditionalCheckFailedException":
                    raise
            return PoolState()
        return _pool(item)

    def _update(self, plan: Plan) -> dict[str, Any]:
        """UpdateItem arguments: the plan's end time and slot, on condition the item
        is still as the plan saw it. holds() is the same condition in Python."""
        names = {"#ends": "ends", "#b": str(plan.bucket), "#s": "s"}
        values: dict[str, Any] = {":second": _number(plan.second)}
        if plan.fresh_bucket:
            sets = ["#ends.#b = :bucket"]
            values[":bucket"] = {"M": {"s": _number(plan.second), "e": {"L": [_number(plan.end_at)]}}}
            conditions = ["(attribute_not_exists(#ends.#b) OR #ends.#b.#s <> :second)"]
        else:
            names["#e"] = "e"
            sets = ["#ends.#b.#e = list_append(#ends.#b.#e, :end)"]
            values[":end"] = {"L": [_number(plan.end_at)]}
            conditions = ["#ends.#b.#s = :second"]
        if plan.slot is not None:
            names.update({"#slots": "slots", "#i": str(plan.slot)})
            if plan.queue:
                sets.append("#slots.#i = #slots.#i + :hold")
                values.update({":hold": _number(plan.hold_s), ":deadline": _number(plan.deadline)})
                conditions.append("#slots.#i <= :deadline")
            else:
                sets.append("#slots.#i = :release")
                values.update({":release": _number(plan.release_at), ":now": _number(plan.now)})
                conditions.append("(attribute_not_exists(#slots.#i) OR #slots.#i <= :now)")
        return {
            "TableName": self._table,
            "Key": {"pool": {"S": POOL_ID}},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ReturnValues": "UPDATED_NEW",
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }


def _pool(item: Mapping[str, Any]) -> PoolState:
    slots = {int(slot): float(value["N"]) for slot, value in item.get("slots", {}).get("M", {}).items()}
    ends = {}
    for bucket, value in item.get("ends", {}).get("M", {}).items():
        stamp = value["M"]
        ends[int(bucket)] = (
            int(stamp["s"]["N"]),
            tuple(float(end["N"]) for end in stamp.get("e", {}).get("L", [])),
        )
    return PoolState(slots, ends)


def _number(value: float | None) -> dict[str, str]:
    return {"N": str(value) if isinstance(value, int) else f"{value:.3f}"}


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    return response.get("Error", {}).get("Code")
