"""Lambda entry point for the trigger. EventBridge's rule sentinelops-alarms
invokes it asynchronously, never retried, with each of checkout-api's alarms
going into ALARM or back to OK. It hands each answered incident to the
remediation flow. See trigger.py.
"""

import os
from typing import Any

import boto3  # provided by the Lambda runtime
from botocore.config import Config

from store import Store
from trigger import (
    HANDOFF_ATTEMPTS,
    HANDOFF_CONNECT_TIMEOUT_S,
    HANDOFF_READ_TIMEOUT_S,
    INVOKE_READ_TIMEOUT_S,
    AgentRuntime,
    RemediationFlow,
    Trigger,
)

_trigger = Trigger(
    Store(
        boto3.client("dynamodb", config=Config(connect_timeout=3, read_timeout=10)),
        os.environ["INCIDENTS_TABLE"],
    ),
    AgentRuntime(
        # One attempt: botocore retries a read timeout by default, and a second
        # invoke would start a second investigation.
        boto3.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=10,
                read_timeout=INVOKE_READ_TIMEOUT_S,
                retries={"total_max_attempts": 1},
            ),
        ),
        os.environ["AGENT_RUNTIME_ARN"],
    ),
    RemediationFlow(
        # Retried: a second start with the same name and payload is the same
        # execution, so a retry can't start a second flow.
        boto3.client(
            "lambda",
            config=Config(
                connect_timeout=HANDOFF_CONNECT_TIMEOUT_S,
                read_timeout=HANDOFF_READ_TIMEOUT_S,
                retries={"total_max_attempts": HANDOFF_ATTEMPTS},
            ),
        ),
        os.environ["REMEDIATION_FUNCTION_ARN"],
    ),
)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _trigger.handle(event)
