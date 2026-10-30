"""The trigger's table, sentinelops-incidents: each service's triage state, its
alarm changes, its incidents with their investigations, and the global lock and
hourly cap.

    pk            sk                     item
    checkout-api  state                  triage's state (JSON) and its version
    checkout-api  change#<at>#<alarm>    one alarm change and its verdict
    checkout-api  incident#<id>          one incident: what opened it, and its investigation
    global        lock                   the investigation running, until it ends or the lock expires
    global        hourly-cap             the start times of the last hour's investigations

Triage's writes for one change are one transaction, conditional on the state's
version: of two changes judged at once, one lands, and the other is judged
again against the state it left.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from triage import Change, Incident, ServiceState, Triage, Verdict, classify, iso

GLOBAL = "global"
# Past the runtime's 360 s and this function's 400 s, so a lock outlives any run
# that holds it, and a crashed run's lock still expires.
LOCK_TTL_S = 420
CAP_PER_HOUR = 20
HOUR_S = 3600
TRIAGE_TRIES = 5


class TriageConflict(Exception):
    """The service's state changed under every try."""


class Store:
    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table

    def triage(self, service: str, change: Change) -> Triage:
        """Judge the change and record it: the state, the change, and for NEW the incident."""
        for _ in range(TRIAGE_TRIES):
            state, version = self._state(service)
            result = classify(state, change, service)
            if result.verdict is Verdict.SEEN or self._save(service, change, result, version):
                return result
        raise TriageConflict(f"{service}'s state changed under {TRIAGE_TRIES} tries")

    def lock(self, holder: str, now: float) -> bool:
        """Take the lock unless another investigation holds it. Expired, it's free."""
        return self._put_if(
            {
                **_key(GLOBAL, "lock"),
                "holder": {"S": holder},
                "acquired_at": {"N": str(int(now))},
                # Also the table's TTL attribute, which only deletes it later.
                "expires_at": {"N": str(int(now) + LOCK_TTL_S)},
            },
            ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
            ExpressionAttributeValues={":now": {"N": str(int(now))}},
        )

    def unlock(self, holder: str) -> bool:
        """Release the lock if it's still ours: false if it expired and someone else took it."""
        try:
            self._client.delete_item(
                TableName=self._table,
                Key=_key(GLOBAL, "lock"),
                ConditionExpression="holder = :holder",
                ExpressionAttributeValues={":holder": {"S": holder}},
            )
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def take_cap_slot(self, now: float) -> bool:
        """Count one investigation against the last hour's, or refuse at the cap.

        Called under the lock, so nothing else writes it meanwhile; if something
        did, the version check refuses, so the cap fails closed.
        """
        item = self._client.get_item(
            TableName=self._table, Key=_key(GLOBAL, "hourly-cap"), ConsistentRead=True
        ).get("Item")
        version = int(item["version"]["N"]) if item else 0
        # Whole seconds, which DynamoDB returns as written (it drops a trailing zero).
        starts = [int(n["N"]) for n in (item or {}).get("starts", {}).get("L", [])]
        recent = [s for s in starts if now - s < HOUR_S]
        if len(recent) >= CAP_PER_HOUR:
            return False
        return self._put_if(
            {
                **_key(GLOBAL, "hourly-cap"),
                "starts": {"L": [{"N": str(s)} for s in [*recent, int(now)]]},
                "version": {"N": str(version + 1)},
            },
            **_if_version(version),
        )

    def update_incident(self, service: str, incident_id: str, fields: Mapping[str, Any]) -> None:
        """Set fields on an incident: strings, numbers, booleans, bytes, or lists of strings."""
        names = {f"#f{i}": name for i, name in enumerate(fields)}
        values = {f":v{i}": _value(v) for i, v in enumerate(fields.values())}
        self._client.update_item(
            TableName=self._table,
            Key=_key(service, f"incident#{incident_id}"),
            UpdateExpression="SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def _put_if(self, item: dict[str, Any], **condition: Any) -> bool:
        """Put the item if the condition holds. When it doesn't, true only if the item
        there is this one: botocore retries a write that timed out, and the first
        try may have landed."""
        try:
            self._client.put_item(
                TableName=self._table, Item=item, ReturnValuesOnConditionCheckFailure="ALL_OLD", **condition
            )
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                return e.response.get("Item") == item
            raise
        return True

    def _state(self, service: str) -> tuple[ServiceState, int]:
        item = self._client.get_item(
            TableName=self._table, Key=_key(service, "state"), ConsistentRead=True
        ).get("Item")
        if item is None:
            return ServiceState(), 0
        return ServiceState.from_dict(json.loads(item["state"]["S"])), int(item["version"]["N"])

    def _save(self, service: str, change: Change, result: Triage, version: int) -> bool:
        """Write one change's triage, unless the state moved on since it was read."""
        writes = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **_key(service, "state"),
                        "state": {"S": json.dumps(result.state.to_dict(), separators=(",", ":"))},
                        "version": {"N": str(version + 1)},
                    },
                    **_if_version(version),
                }
            },
            {"Put": {"TableName": self._table, "Item": _change_item(service, change, result)}},
        ]
        if result.verdict is Verdict.NEW:
            writes.append({"Put": {"TableName": self._table, "Item": _incident_item(service, result.incident)}})
        try:
            self._client.transact_write_items(TransactItems=writes)
        except ClientError as e:
            if _code(e) == "TransactionCanceledException" and _reason(e, 0) == "ConditionalCheckFailed":
                return False
            raise
        return True


def _change_item(service: str, change: Change, result: Triage) -> dict[str, Any]:
    item = {
        **_key(service, f"change#{iso(change.at)}#{change.alarm}"),
        "alarm": {"S": change.alarm},
        "state": {"S": change.state},
        "at": {"S": iso(change.at)},
        "verdict": {"S": result.verdict.value},
    }
    if result.incident is not None:
        item["incident"] = {"S": result.incident.id}
    return item


def _incident_item(service: str, incident: Incident | None) -> dict[str, Any]:
    assert incident is not None
    return {
        **_key(service, f"incident#{incident.id}"),
        "incident": {"S": incident.id},
        "opened_at": {"S": iso(incident.opened_at)},
        "opened_by": {"S": incident.opened_by},
        "status": {"S": "opened"},
    }


def _key(pk: str, sk: str) -> dict[str, Any]:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _if_version(version: int) -> dict[str, Any]:
    """The condition that the item is still at `version`; 0 means it doesn't exist yet."""
    if version == 0:
        return {"ConditionExpression": "attribute_not_exists(pk)"}
    return {
        "ConditionExpression": "#version = :version",
        "ExpressionAttributeNames": {"#version": "version"},
        "ExpressionAttributeValues": {":version": {"N": str(version)}},
    }


def _value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, float):
        # Plain notation: str() gives 1e-05 for a small cost.
        return {"N": format(Decimal(repr(value)), "f")}
    if isinstance(value, bytes):
        return {"B": value}
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return {"L": [{"S": v} for v in value]}
    raise TypeError(f"Can't store {type(value).__name__} in an incident")


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def _reason(e: ClientError, index: int) -> str | None:
    """Why write `index` of a cancelled transaction failed."""
    reasons = e.response.get("CancellationReasons") or []
    return reasons[index].get("Code") if len(reasons) > index else None
