"""The flow's view of the trigger's table: an incident's answer, read, and the flow's result, written.

The item is the trigger's (trigger/store.py): pk the service, sk
incident#<id>, with the incident's ID, when it opened, the alarm that opened
it, the investigation's status, and the Hypothesis as JSON. The flow reads
only those, and writes one attribute, remediation, as a whole map (a nested
SET on a map that isn't there fails), and only on an item that exists. Its
role may touch checkout-api's items alone (dynamodb:LeadingKeys).

AWS errors leave as their code, never their message, which can name an ARN.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

READ = ("incident", "opened_at", "opened_by", "status", "hypothesis")


@dataclass(frozen=True)
class Answered:
    """An incident as the trigger stored it."""

    incident: str
    opened_at: str
    opened_by: str
    status: str
    hypothesis: str | None  # the JSON the Runtime answered with


class Incidents:
    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table

    def get(self, service: str, incident_id: str) -> Answered | None:
        names = {f"#a{i}": name for i, name in enumerate(READ)}
        found = self._client.get_item(
            TableName=self._table,
            Key=_key(service, incident_id),
            ConsistentRead=True,
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        ).get("Item")
        if found is None:
            return None
        text = {name: found[name]["S"] for name in READ if "S" in found.get(name, {})}
        if any(name not in text for name in READ[:4]):
            return None
        return Answered(**{name: text.get(name) for name in READ})  # type: ignore[arg-type]

    def record(self, service: str, incident_id: str, result: Mapping[str, Any]) -> None:
        """Set the incident's remediation map: strings, numbers, booleans, and nulls."""
        self._client.update_item(
            TableName=self._table,
            Key=_key(service, incident_id),
            UpdateExpression="SET #r = :r",
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeNames={"#r": "remediation"},
            ExpressionAttributeValues={":r": {"M": {k: _value(v) for k, v in result.items()}}},
        )


def _key(service: str, incident_id: str) -> dict[str, Any]:
    return {"pk": {"S": service}, "sk": {"S": f"incident#{incident_id}"}}


def _value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int | float):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"can't store a {type(value).__name__}")
