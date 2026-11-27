"""Lambda entry point for the remediation flow, sentinelops-remediation, invoked through its live alias.

The trigger starts it asynchronously, named by the incident's ID; the steps
are in sentinelops_remediation/flow.py. Each invocation (the first, and one
after every wait) gets its own GitHub App client, made only when a step that
runs needs GitHub, and its installation token is revoked when the invocation
ends (flow.invocation): a token never outlives the invocation that asked for
it, and never enters a checkpoint.

KILL SWITCH (README, "Kill switch"): reserved concurrency 0 stops every start
and holds a waiting execution; stop-durable-execution ends one for good.
Terraform ignores the function's reserved concurrency, so a deploy never
lifts it.

The trace table (SENTINELOPS_TRACE_TABLE) gets each step's event for the page:
one try with short timeouts, so a slow table never holds a step up.
"""

import os
from typing import Any

import boto3
from aws_durable_execution_sdk_python import DurableContext, durable_execution
from botocore.config import Config

from sentinelops_remediation import flow
from sentinelops_remediation.github import GitHubApp
from sentinelops_remediation.incidents import Incidents

_AWS = Config(connect_timeout=3, read_timeout=10, retries={"mode": "standard", "max_attempts": 3})
_INCIDENTS = Incidents(boto3.client("dynamodb", config=_AWS), os.environ["INCIDENTS_TABLE"])
_KMS = boto3.client("kms", config=_AWS)
_CLIENT_ID = os.environ["GITHUB_APP_CLIENT_ID"]
_INSTALLATION_ID = int(os.environ["GITHUB_APP_INSTALLATION_ID"])
# For the commit hooks' account rule, which the flow's scan applies to everything it posts.
_ACCOUNT_ID = os.environ["SENTINELOPS_ACCOUNT_ID"]
_TRACE_CLIENT = Config(connect_timeout=1, read_timeout=2, retries={"mode": "standard", "total_max_attempts": 1})
_TRACE = (
    flow.TraceTable(boto3.client("dynamodb", config=_TRACE_CLIENT), os.environ["SENTINELOPS_TRACE_TABLE"])
    if os.environ.get("SENTINELOPS_TRACE_TABLE")
    else None
)


def _github() -> GitHubApp:
    return GitHubApp(_CLIENT_ID, _INSTALLATION_ID, _KMS)


@durable_execution
def handler(event: Any, context: DurableContext) -> dict[str, Any]:
    return flow.invocation(event, context, _INCIDENTS, _github, _ACCOUNT_ID, _TRACE)
