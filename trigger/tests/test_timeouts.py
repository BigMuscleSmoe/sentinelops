"""The timeouts nest, nothing retries an investigation, and a deploy leaves the kill
switch alone: each read from where it's set."""

import importlib
import re
import sys
from pathlib import Path

import pytest

from store import LOCK_TTL_S

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/sentinelops_agent-abc123"
FLOW_ARN = "arn:aws:lambda:us-east-1:123456789012:function:sentinelops-remediation:live"
# What the trigger's timeout leaves past everything it waits for, and the lock's
# past the trigger's: 20 s each, as before the handoff added its 10 s.
MARGIN_S = 20


def block(path: str, header: str) -> str:
    """A top-level Terraform block, from its header to its closing brace."""
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(header + " {\n")
    return text[start : text.index("\n}\n", start)]


def setting(text: str, pattern: str) -> float:
    """The number pattern finds, which must appear exactly once."""
    matches = re.findall(pattern, text)
    assert len(matches) == 1, f"{pattern} matched {len(matches)} times"
    return float(matches[0])


@pytest.fixture
def handler(monkeypatch, tmp_path):
    """handler.py as Lambda imports it, with none of this machine's AWS settings."""
    monkeypatch.setenv("INCIDENTS_TABLE", "sentinelops-incidents")
    monkeypatch.setenv("AGENT_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setenv("REMEDIATION_FUNCTION_ARN", FLOW_ARN)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("handler", None)
    yield importlib.import_module("handler")
    sys.modules.pop("handler", None)


def test_each_timeout_outlasts_the_one_inside_it(handler) -> None:
    guard = setting((ROOT / "agent/guards.py").read_text(encoding="utf-8"), r"max_wall_clock_s: float = ([\d.]+)")
    runtime = setting(block("infra/agent_runtime.tf", 'resource "aws_bedrockagentcore_agent_runtime" "agent"'), r"max_lifetime\s*=\s*(\d+)")
    invoke = handler._trigger._runtime._client.meta.config
    handoff = handler._trigger._flow._client.meta.config
    function = setting(block("infra/trigger.tf", 'module "trigger"'), r"\btimeout\s*=\s*(\d+)")

    # The guard ends the run, the runtime its session, the trigger its wait for
    # the answer, Lambda the trigger, and then the lock expires. The handoff
    # comes after the lock is released, so only the function must outlast it.
    assert guard < runtime < invoke.read_timeout
    waiting = invoke.connect_timeout + invoke.read_timeout
    handing_off = handoff.retries["total_max_attempts"] * (handoff.connect_timeout + handoff.read_timeout)
    assert waiting + handing_off + MARGIN_S <= function
    assert function + MARGIN_S <= LOCK_TTL_S


def test_the_invoke_is_sent_once(handler) -> None:
    # botocore retries a read timeout by default, and a second invoke would be a
    # second investigation.
    assert handler._trigger._runtime._client.meta.config.retries["total_max_attempts"] == 1


def test_only_the_handoff_is_retried_and_it_names_the_alias(handler) -> None:
    # A repeated start with the same name is the same execution (trigger.py).
    flow = handler._trigger._flow
    assert flow._client.meta.config.retries["total_max_attempts"] == 2
    assert flow._arn == FLOW_ARN
    tf = block("infra/trigger.tf", 'module "trigger"')
    assert "REMEDIATION_FUNCTION_ARN = aws_lambda_alias.remediation_live.arn" in tf


def test_lambda_never_retries_the_trigger() -> None:
    config = block("infra/trigger.tf", 'resource "aws_lambda_function_event_invoke_config" "trigger"')

    assert setting(config, r"maximum_retry_attempts\s*=\s*(\d+)") == 0


def test_a_deploy_never_turns_the_kill_switch_back_on() -> None:
    rule = block("infra/trigger.tf", 'resource "aws_cloudwatch_event_rule" "alarms"')

    assert re.search(r"ignore_changes\s*=\s*\[[^\]]*\bstate\b", rule)
