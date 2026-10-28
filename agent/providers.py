"""Which model the agent talks to, and the key it uses.

The provider is a setting: the Anthropic API until Bedrock's quota is granted,
then Bedrock. Either way the model must have a price in pricing.PRICES.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROVIDERS = ("anthropic", "bedrock")
DEFAULT_MODEL_IDS = {"anthropic": "claude-haiku-5-5"}
API_KEY_SECRET = "sentinelops/anthropic-api-key"
API_KEY_PREFIX = "sk-ant-"
MAX_OUTPUT_TOKENS = 16_000
# Haiku 5.5 only thinks adaptively. Summarized makes the reasoning readable in
# the transcript and changes nothing about what the call costs.
THINKING = {"type": "adaptive", "display": "summarized"}


class SettingsError(Exception):
    pass


class KeyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    provider: str = "anthropic"
    model_id: str = DEFAULT_MODEL_IDS["anthropic"]
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    region: str = "us-east-1"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Settings":
        provider = environ.get("SENTINELOPS_MODEL_PROVIDER", "anthropic")
        if provider not in PROVIDERS:
            raise SettingsError(
                f"SENTINELOPS_MODEL_PROVIDER is {provider!r}; it must be one of {', '.join(PROVIDERS)}."
            )
        model_id = environ.get("SENTINELOPS_MODEL_ID") or DEFAULT_MODEL_IDS.get(provider)
        if not model_id:
            raise SettingsError(f"Set SENTINELOPS_MODEL_ID: {provider} has no default model.")
        return cls(provider=provider, model_id=model_id, region=environ.get("AWS_REGION", "us-east-1"))


def read_api_key(secrets: Any, secret_id: str = API_KEY_SECRET) -> str:
    """The Anthropic API key, from Secrets Manager.

    It goes from here straight into the model client: never into the
    environment, a log line, the transcript, or an error message. Errors name
    the secret and the AWS error code, never the value, and every raise is
    outside an except block, so no error carries another one: a JSON error
    keeps the whole document it parsed.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    failure: str | None = None
    try:
        value = secrets.get_secret_value(SecretId=secret_id).get("SecretString") or ""
    except ClientError as e:
        failure = f"AWS returned {e.response.get('Error', {}).get('Code', 'unknown')}"
    except BotoCoreError as e:
        failure = type(e).__name__
    if failure is not None:
        raise KeyUnavailable(f"Could not read {secret_id}: {failure}.")

    # The console stores a plain string, or a JSON object of key/value pairs.
    if value.lstrip().startswith("{"):
        strings = _string_values(value)
        if strings is None:
            raise KeyUnavailable(f"{secret_id} starts like JSON but doesn't parse.")
        if len(strings) != 1:
            raise KeyUnavailable(f"{secret_id} is a JSON object without exactly one string value.")
        value = strings[0]
    value = value.strip()
    if not value.startswith(API_KEY_PREFIX):
        raise KeyUnavailable(f"{secret_id} doesn't hold an Anthropic API key.")
    return value


def _string_values(text: str) -> list[str] | None:
    """The string values of a JSON object, or None if the text isn't one."""
    try:
        pairs = json.loads(text)
    except ValueError:
        return None
    if not isinstance(pairs, dict):
        return None
    return [v for v in pairs.values() if isinstance(v, str)]


def build_model(settings: Settings, api_key: str | None = None, http_client: Any = None) -> Any:
    """The Strands model for these settings. http_client is for tests: a fake API."""
    if settings.provider == "bedrock":
        # Waits for the quota: the model ID, its prices, and how thinking is
        # passed get checked then (PROJECT_STATUS, December reminder).
        raise SettingsError("Bedrock isn't wired up yet; its quota is still 0.")
    if not api_key:
        raise KeyUnavailable("The anthropic provider needs an API key.")

    from strands.models.anthropic import AnthropicModel

    client_args: dict[str, Any] = {"api_key": api_key, "max_retries": 2, "timeout": 60.0}
    if http_client is not None:
        client_args["http_client"] = http_client
    return AnthropicModel(
        client_args=client_args,
        model_id=settings.model_id,
        max_tokens=settings.max_output_tokens,
        params={"thinking": THINKING},
        # Strands' estimate stays local and unused by the cost cap: hooks.py counts
        # each whole request itself, where this flag's count covers only the
        # messages since the last reply, without thinking, and guesses on errors.
        use_native_token_count=False,
    )
