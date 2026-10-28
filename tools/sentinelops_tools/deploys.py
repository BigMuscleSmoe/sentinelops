"""checkout-api's deploys and config changes, newest first, up to the investigation's now.

`ts <= now` keeps a replay honest: the rollback a break schedules, and anything
written after the alarm, stay out of view.
"""

import json
from typing import Any

from sentinelops_tools.env import ToolEnv, aws_errors_as_text, iso_ms

MAX_RECORDS = 20
FIELDS = ("ts", "kind", "version", "commit", "author", "message", "changes")


@aws_errors_as_text("the deploy history")
def history(env: ToolEnv, limit: int = 10) -> str:
    notes = []
    if limit > MAX_RECORDS:
        notes.append(f"At most {MAX_RECORDS} records per call.")
    limit = min(max(1, limit), MAX_RECORDS)
    now = iso_ms(env.now())

    names = {f"#{name}": name for name in FIELDS}
    response = env.dynamodb.query(
        TableName=env.history_table,
        KeyConditionExpression="#service = :service AND #ts <= :now",
        ProjectionExpression=", ".join(names),
        ExpressionAttributeNames={"#service": "service", **names},
        ExpressionAttributeValues={":service": {"S": env.service}, ":now": {"S": now}},
        ScanIndexForward=False,
        Limit=limit,
    )
    records = [_record(item) for item in response.get("Items", [])]
    if not records:
        return f"No deploys or config changes to {env.service} recorded up to {now}."

    lines = [
        f"The {len(records)} most recent deploys and config changes to {env.service} up to "
        f"{now}, newest first.",
        *notes,
    ]
    if "LastEvaluatedKey" in response:
        lines.append(f"Older records exist; ask for a larger limit (at most {MAX_RECORDS}).")
    lines += [json.dumps(r, separators=(",", ":")) for r in records]
    return "\n".join(lines)


def _record(item: dict[str, Any]) -> dict[str, Any]:
    record = {name: _value(item[name]) for name in FIELDS if name in item}
    if isinstance(record.get("changes"), list):
        # DynamoDB keeps a map's keys in no order; read as key, old, new.
        record["changes"] = [
            {k: c[k] for k in ("key", "old", "new") if k in c} if isinstance(c, dict) else c
            for c in record["changes"]
        ]
    # Empty fields say nothing; leave them out rather than spend tokens on them.
    return {k: v for k, v in record.items() if v not in ("", [], None)}


def _value(attribute: dict[str, Any]) -> Any:
    (kind, value), = attribute.items()
    match kind:
        case "S" | "N":
            return value
        case "BOOL":
            return value
        case "NULL":
            return None
        case "L":
            return [_value(v) for v in value]
        case "M":
            return {k: _value(v) for k, v in value.items()}
    return str(value)
