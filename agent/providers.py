"""Which model the agent talks to, and how it signs in.

The provider is a setting: the Anthropic API until Bedrock's quota is granted,
then Bedrock. Either way the model must have a price in pricing.PRICES.

The Anthropic API takes a key from Secrets Manager, or, once the federation
IDs are set, a token from workload identity federation: STS signs a JWT naming
this process's AWS identity, and Anthropic trades it for a token that lasts
minutes. Nothing then holds a key.
"""

import json
import os
import uuid
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

# The Claude Console's IDs for federation, by field. All set, or none.
FEDERATION_ENV = {
    "organization_id": "SENTINELOPS_ANTHROPIC_ORGANIZATION_ID",
    "service_account_id": "SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID",
    "rule_id": "SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID",
}
FEDERATION_ID_PREFIXES = {"service_account_id": "svac_", "rule_id": "fdrl_"}
# What the agent asks STS for. The runtime's role may ask for nothing else
# (infra/agent_runtime.tf, SignInToAnthropic), and the rules match the audience.
TOKEN_AUDIENCE = "https://api.anthropic.com"
TOKEN_SIGNING_ALGORITHM = "RS256"
# STS's default. Anthropic's token lives at most twice what's left of the JWT
# it came from, so a 300 s JWT can give up to 600 s: a whole run on one token.
TOKEN_SECONDS = 300


class SettingsError(Exception):
    pass


class KeyUnavailable(Exception):
    pass


class FederationFailed(Exception):
    pass


@dataclass(frozen=True)
class Federation:
    """The Claude Console's IDs for signing in by workload identity federation.

    Not credentials: without a JWT that AWS signed for a principal the rule
    names, they grant nothing. They're account-specific, though, so they come
    from the environment, never the repo.
    """

    organization_id: str
    service_account_id: str
    rule_id: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Federation | None":
        """The IDs, or None when none is set. Half a setup is an error, never a fallback to the key."""
        values = {field: environ.get(name, "").strip() for field, name in FEDERATION_ENV.items()}
        if not any(values.values()):
            return None
        missing = [FEDERATION_ENV[field] for field, value in values.items() if not value]
        if missing:
            raise SettingsError(
                f"Federation is half set up: set {', '.join(missing)} as well, or none of them."
            )
        for field, prefix in FEDERATION_ID_PREFIXES.items():
            if not values[field].startswith(prefix):
                raise SettingsError(f"{FEDERATION_ENV[field]} must start with {prefix}.")
        if not _is_uuid(values["organization_id"]):
            raise SettingsError(f"{FEDERATION_ENV['organization_id']} must be the organization's UUID.")
        return cls(**values)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Settings:
    provider: str = "anthropic"
    model_id: str = DEFAULT_MODEL_IDS["anthropic"]
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    region: str = "us-east-1"
    # Set, the Anthropic API is signed in to by federation; None, with the key.
    federation: Federation | None = None

    @property
    def auth(self) -> str:
        return "federation" if self.federation is not None else "api_key"

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
        return cls(
            provider=provider,
            model_id=model_id,
            region=environ.get("AWS_REGION", "us-east-1"),
            federation=Federation.from_env(environ),
        )


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


def federated_credentials(
    federation: Federation, session: Any, http_client: Any = None
) -> "PrimedCredentials":
    """Credentials for the Anthropic API from this process's AWS identity.

    STS signs a JWT naming the caller (the runtime's role, or sentinelops-dev
    on this machine), and Anthropic's token endpoint trades it, under the
    federation rule, for a token that lasts minutes. The first token is minted
    here, before the run, so a broken setup is a refusal rather than a failed
    first model call. Raises FederationFailed, which names the AWS error code
    or the HTTP status, never a token or an ARN, and carries no other error.
    http_client is for tests: a fake token endpoint.
    """
    from anthropic import WorkloadIdentityCredentials, WorkloadIdentityError
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    # Seconds, not minutes, as the tools' clients: the run waits on it.
    config = Config(connect_timeout=3, read_timeout=10, retries={"mode": "standard", "max_attempts": 3})
    sts = session.client("sts", config=config)

    def web_identity_token() -> str:
        # A fresh JWT for every exchange: Anthropic accepts each JWT once.
        return sts.get_web_identity_token(
            Audience=[TOKEN_AUDIENCE], SigningAlgorithm=TOKEN_SIGNING_ALGORITHM, DurationSeconds=TOKEN_SECONDS
        )["WebIdentityToken"]

    exchange = WorkloadIdentityCredentials(
        identity_token_provider=web_identity_token,
        federation_rule_id=federation.rule_id,
        organization_id=federation.organization_id,
        service_account_id=federation.service_account_id,
        http_client=http_client,
    )
    failure: str | None = None
    try:
        first = exchange()
    except ClientError as e:
        failure = f"STS returned {e.response.get('Error', {}).get('Code', 'unknown')}"
    except BotoCoreError as e:
        failure = f"STS: {type(e).__name__}"
    except WorkloadIdentityError as e:
        # Never its message, which can quote the endpoint's answer. A 401 is
        # deliberately opaque anyway: its reason is in the Console's
        # authentication history, which the request ID finds. No status means
        # no answer: the endpoint couldn't be reached.
        answer = f"HTTP {e.status_code}" if e.status_code is not None else "no response"
        if e.request_id:
            answer += f", request {e.request_id}"
        failure = f"the token exchange failed ({answer})"
    if failure is not None:
        exchange.close()
        raise FederationFailed(f"Could not sign in to the Anthropic API by federation: {failure}.")
    return PrimedCredentials(exchange, first)


class PrimedCredentials:
    """The token minted before the run, then a new exchange whenever the client asks.

    The client's token cache calls this when it holds no token, when its token
    is within two minutes of expiring, and, with force_refresh, after a 401.
    """

    def __init__(self, exchange: Any, first: Any) -> None:
        self._exchange = exchange
        self._first = first

    def __call__(self, *, force_refresh: bool = False) -> Any:
        first, self._first = self._first, None
        if first is not None and not force_refresh:
            return first
        return self._exchange()

    def close(self) -> None:
        self._exchange.close()


def build_model(
    settings: Settings, api_key: str | None = None, http_client: Any = None, *, credentials: Any = None
) -> Any:
    """The Strands model for these settings, signed in with the key or with federated
    credentials, never both. http_client is for tests: a fake API."""
    if settings.provider == "bedrock":
        # Waits for the quota: the model ID, its prices, and how thinking is
        # passed get checked then (PROJECT_STATUS, December reminder).
        raise SettingsError("Bedrock isn't wired up yet; its quota is still 0.")
    if credentials is not None and api_key:
        raise SettingsError("Sign in with the API key or by federation, not both.")
    if credentials is None and not api_key:
        raise KeyUnavailable("The anthropic provider needs an API key or federated credentials.")

    from strands.models.anthropic import AnthropicModel

    # Either way, the credential goes to the client as an argument: the client
    # then never reads ANTHROPIC_API_KEY or the SDK's federation variables.
    client_args: dict[str, Any] = {"max_retries": 2, "timeout": 60.0}
    if credentials is not None:
        client_args["credentials"] = credentials
    else:
        client_args["api_key"] = api_key
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
