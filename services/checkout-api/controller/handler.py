"""Lambda entry point for the fault controller.

From the CLI, with the payload in a file:

    aws lambda invoke --function-name sentinelops-fault-controller \
        --cli-binary-format raw-in-base64-out --payload file://break.json out.json

where the payload is {"action": "break", "mode": "db_pool_exhaustion"} (or
"bad_deploy"), {"action": "clear"}, or {"action": "status"}.
"""

import json
import os
import random
import time
from typing import Any

import boto3  # provided by the Lambda runtime

from controller.actions import FaultController

controller = FaultController(
    boto3.client("dynamodb"),
    os.environ["FLAGS_TABLE"],
    os.environ["DEPLOY_HISTORY_TABLE"],
    time.time,
    random.Random(),
)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    result = controller.handle(event)
    print(json.dumps({"message": "fault controller", "action": event.get("action"), "result": result}), flush=True)
    return result
