"""break, clear, and status, against the flags table and deploy history.

A break is one transaction: the flag, only if no fault is active; the fault's
cause in deploy history, dated now; one decoy, dated 1 to 6 hours ago; and the
rollback, dated when the flag expires, so the history matches what the
service does even if nobody clears it. clear deletes the flag and moves the
rollback to now. Every history write is conditional, so none overwrites
another.
"""

import math
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from controller.faults import DECOYS, DURATION_S, FAULTS, SERVICE, Change

FLAG_KEY = {"flag": {"S": "active"}}
DECOY_AGE_S = (3600.0, 6 * 3600.0)


def timestamp(epoch_s: float) -> str:
    """A deploy-history sort key: UTC to the millisecond, so the keys sort as text."""
    moment = datetime.fromtimestamp(epoch_s, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record(change: Change, ts: str) -> dict[str, Any]:
    """A deploy-history item."""
    return {
        "service": {"S": SERVICE},
        "ts": {"S": ts},
        "kind": {"S": change.kind},
        "version": {"S": change.version},
        "commit": {"S": change.commit},
        "author": {"S": change.author},
        "message": {"S": change.message},
        "changes": {
            "L": [
                {"M": {"key": {"S": key}, "old": {"S": old}, "new": {"S": new}}}
                for key, old, new in change.changes
            ]
        },
    }


class FaultController:
    def __init__(
        self,
        client: Any,
        flags_table: str,
        history_table: str,
        clock: Callable[[], float],
        rng: random.Random,
    ) -> None:
        self._client = client
        self._flags_table = flags_table
        self._history_table = history_table
        self._clock = clock
        self._rng = rng

    def handle(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action == "break":
            return self.break_(payload.get("mode"))
        if action == "clear":
            return self.clear()
        if action == "status":
            return self.status()
        return {"ok": False, "error": f"unknown action {action!r}; use break, clear, or status"}

    def break_(self, mode: Any) -> dict[str, Any]:
        fault = FAULTS.get(mode) if isinstance(mode, str) else None
        if fault is None:
            return {"ok": False, "error": f"unknown mode {mode!r}; use one of {', '.join(FAULTS)}"}
        now = self._clock()
        expires_at = math.ceil(now) + DURATION_S  # whole seconds, as TTL wants
        flag = {
            **FLAG_KEY,
            "mode": {"S": mode},
            "overrides": {"M": fault.overrides},
            "set_at": {"N": f"{now:.3f}"},
            "expires_at": {"N": str(expires_at)},
            "rollback_ts": {"S": timestamp(expires_at)},
        }
        decoy = self._rng.choice(DECOYS)
        decoy_at = now - self._rng.uniform(*DECOY_AGE_S)
        writes = [
            {
                "Put": {
                    "TableName": self._flags_table,
                    "Item": flag,
                    # An expired flag no longer counts, even before TTL deletes it.
                    "ConditionExpression": "attribute_not_exists(#flag) OR #expires_at <= :now",
                    "ExpressionAttributeNames": {"#flag": "flag", "#expires_at": "expires_at"},
                    "ExpressionAttributeValues": {":now": {"N": f"{now:.3f}"}},
                }
            },
            self._put_record(fault.cause, now),
            self._put_record(decoy, decoy_at),
            self._put_record(fault.rollback, expires_at),
        ]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            if _condition_failed(exc, 0):
                return {**self.status(), "ok": False, "error": "a fault is already active"}
            raise
        return {"ok": True, "active": True, "mode": mode, "expires_at": timestamp(expires_at)}

    def clear(self) -> dict[str, Any]:
        item = self._read_flag()
        if item is None:
            return {"ok": True, "cleared": None}
        now = self._clock()
        active = now < float(item["expires_at"]["N"])
        writes: list[dict[str, Any]] = [
            {
                "Delete": {
                    "TableName": self._flags_table,
                    "Key": FLAG_KEY,
                    "ConditionExpression": "#set_at = :set_at",  # still the flag that was read
                    "ExpressionAttributeNames": {"#set_at": "set_at"},
                    "ExpressionAttributeValues": {":set_at": item["set_at"]},
                }
            }
        ]
        fault = FAULTS.get(item["mode"]["S"])
        if active and fault is not None:  # the rollback hasn't happened yet: it happens now
            writes += [
                {
                    "Delete": {
                        "TableName": self._history_table,
                        "Key": {"service": {"S": SERVICE}, "ts": item["rollback_ts"]},
                    }
                },
                self._put_record(fault.rollback, now),
            ]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            if _condition_failed(exc, 0):
                return {"ok": False, "error": "the flag changed while clearing it; check status"}
            raise
        return {"ok": True, "cleared": item["mode"]["S"] if active else None}

    def status(self) -> dict[str, Any]:
        item = self._read_flag()
        now = self._clock()
        if item is None or now >= float(item["expires_at"]["N"]):
            return {"ok": True, "active": False}
        expires_at = float(item["expires_at"]["N"])
        return {
            "ok": True,
            "active": True,
            "mode": item["mode"]["S"],
            "set_at": timestamp(float(item["set_at"]["N"])),
            "expires_at": timestamp(expires_at),
            "remaining_s": round(expires_at - now),
        }

    def _read_flag(self) -> dict[str, Any] | None:
        response = self._client.get_item(TableName=self._flags_table, Key=FLAG_KEY, ConsistentRead=True)
        return response.get("Item")

    def _put_record(self, change: Change, at: float) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._history_table,
                "Item": record(change, timestamp(at)),
                "ConditionExpression": "attribute_not_exists(#ts)",
                "ExpressionAttributeNames": {"#ts": "ts"},
            }
        }


def _condition_failed(exc: Exception, index: int) -> bool:
    """Whether a transaction was cancelled because write `index`'s condition failed."""
    response = getattr(exc, "response", None) or {}
    if response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons") or []
    return len(reasons) > index and reasons[index].get("Code") == "ConditionalCheckFailed"
