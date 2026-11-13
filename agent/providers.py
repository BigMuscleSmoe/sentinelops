"""Which model the agent talks to, and how it signs in.

The provider is a setting: the Anthropic API until Bedrock's quota is granted,
then Bedrock. Either way the model must have a price in pricing.PRICES.

The Anthropic API is signed in to by workload identity federation only: STS
signs a JWT naming this process's AWS identity, and Anthropic trades it for a
token that lasts minutes. Nothing holds a key; there is no key to fall back to.
"""

import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROVIDERS = ("anthropic", "bedrock")
DEFAULT_MODEL_IDS = {"anthropic": "claude-haiku-5-5"}
MAX_OUTPUT_TOKENS = 16_000
# Haiku 5.5 only thinks adaptively. Summarized makes the reasoning readable in
# the transcript and changes nothing about what the call costs.
THINKING = {"type": "adaptive", "display": "summarized"}

# The Claude Console's IDs for federation, by field. The anthropic provider needs all three.
FEDERATION_ENV = {
    "organization_id": "SENTINELOPS_ANTHROPIC_ORGANIZATION_ID",
    "service_account_id": "SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID",
    "rule_id": "SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID",
}
FEDERATION_ID_PREFIXES = {"service_account_id": "svac_", "rule_id": "fdrl_"}
# Anthropic's federation rules name IAM roles only, so a user (sentinelops-dev,
# on this machine) asks STS for the token as a role it assumes for just that:
# sentinelops-local (infra/local_sign_in.tf). Unset on the runtime, a role already.
FEDERATION_ROLE_ENV = "SENTINELOPS_ANTHROPIC_ROLE_ARN"
ROLE_ARN = re.compile(r"arn:aws:iam::\d{12}:role/[\w+=,.@-]+")
ROLE_SESSION_NAME = "sentinelops-local"
# STS's minimum, and longer than a run: each JWT is asked for under fresh keys.
ROLE_SESSION_SECONDS = 900
# What the agent asks STS for. The runtime's role may ask for nothing else
# (infra/agent_runtime.tf, SignInToAnthropic), and the rules match the audience.
TOKEN_AUDIENCE = "https://api.anthropic.com"
TOKEN_SIGNING_ALGORITHM = "RS256"
# STS's default. Anthropic's token lives at most twice what's left of the JWT
# it came from, so a 300 s JWT can give up to 600 s: a whole run on one token.
TOKEN_SECONDS = 300


class SettingsError(Exception):
    pass


class FederationFailed(Exception):
    pass


@dataclass(frozen=True)
class Federation:
    """The Claude Console's IDs for signing in by workload identity federation.

    Not credentials: without a JWT that AWS signed for a principal the rule
    names, they grant nothing. They're account-specific, though, so they come
    from the environment, never the repo. role_arn, set only off the runtime,
    is the role to ask STS as.
    """

    organization_id: str
    service_account_id: str
    rule_id: str
    role_arn: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Federation | None":
        """The IDs, or None when none is set. Half a setup is an error."""
        values = {field: environ.get(name, "").strip() for field, name in FEDERATION_ENV.items()}
        role_arn = environ.get(FEDERATION_ROLE_ENV, "").strip() or None
        if not any(values.values()):
            if role_arn is not None:
                raise SettingsError(f"{FEDERATION_ROLE_ENV} is set, but the federation IDs aren't.")
            return None
        # Never the value in the message: an ARN names the account.
        if role_arn is not None and not ROLE_ARN.fullmatch(role_arn):
            raise SettingsError(f"{FEDERATION_ROLE_ENV} must be an IAM role's ARN.")
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
        return cls(**values, role_arn=role_arn)


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
    # How the Anthropic API is signed in to. from_env refuses the anthropic
    # provider without it; Bedrock, once wired, will sign with AWS credentials.
    federation: Federation | None = None

    @property
    def auth(self) -> str | None:
        return "federation" if self.federation is not None else None

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
        federation = Federation.from_env(environ)
        if provider == "anthropic" and federation is None:
            raise SettingsError(
                f"The Anthropic API is signed in to by federation only: set {', '.join(FEDERATION_ENV.values())}."
            )
        return cls(
            provider=provider,
            model_id=model_id,
            region=environ.get("AWS_REGION", "us-east-1"),
            federation=federation,
        )


def federated_credentials(
    federation: Federation, session: Any, http_client: Any = None
) -> "PrimedCredentials":
    """Credentials for the Anthropic API from this process's AWS identity.

    STS signs a JWT naming the caller's role (the runtime's, or on this machine
    the one federation.role_arn names, assumed afresh for each JWT), and
    Anthropic's token endpoint trades it, under the federation rule, for a
    token that lasts minutes. The first token is minted
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

    def token_sts() -> Any:
        if federation.role_arn is None:
            return sts
        keys = sts.assume_role(
            RoleArn=federation.role_arn, RoleSessionName=ROLE_SESSION_NAME, DurationSeconds=ROLE_SESSION_SECONDS
        )["Credentials"]
        return session.client(
            "sts",
            config=config,
            aws_access_key_id=keys["AccessKeyId"],
            aws_secret_access_key=keys["SecretAccessKey"],
            aws_session_token=keys["SessionToken"],
        )

    def web_identity_token() -> str:
        # A fresh JWT for every exchange: Anthropic accepts each JWT once.
        return token_sts().get_web_identity_token(
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


def build_model(settings: Settings, http_client: Any = None, *, credentials: Any = None) -> Any:
    """The Strands model for these settings, signed in with the federated
    credentials. http_client is for tests: a fake API."""
    if settings.provider == "bedrock":
        # Waits for the quota: the model ID, its prices, and how thinking is
        # passed get checked then (PROJECT_STATUS, December reminder).
        raise SettingsError("Bedrock isn't wired up yet; its quota is still 0.")
    if credentials is None:
        raise SettingsError("The anthropic provider signs in by federation: it needs federated credentials.")

    from strands.models.anthropic import AnthropicModel

    # The credentials go to the client as an argument, so it never reads
    # ANTHROPIC_API_KEY or the SDK's own federation variables.
    client_args: dict[str, Any] = {"max_retries": 2, "timeout": 60.0, "credentials": credentials}
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
