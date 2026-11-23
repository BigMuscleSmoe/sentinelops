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

from strands.models.anthropic import AnthropicModel

from sentinelops_core.models import Hypothesis

PROVIDERS = ("anthropic", "bedrock")
DEFAULT_MODEL_IDS = {"anthropic": "claude-haiku-5-5"}
MAX_OUTPUT_TOKENS = 16_000
# Haiku 5.5 only thinks adaptively. Summarized makes the reasoning readable in
# the transcript and changes nothing about what the call costs.
THINKING = {"type": "adaptive", "display": "summarized"}

# The answer tool goes out strict, so the API samples its input from the
# schema: every required field, the right types, no other keys. Without it
# Haiku 5.5 closed root_cause with </root_cause> in 15 answers over two
# sweeps, losing the field after it (BUILD_LOG, 2026-11-14). Strict schemas
# can't use these keywords (a 400); Pydantic still checks them on our side.
# And every field goes out required: with 14 optional ones the API refused the
# schema as too complex to compile, since each optional field multiplies the
# grammar. A field the answer may leave unset is written as null instead.
ANSWER_TOOL = Hypothesis.__name__
# The answer's schema as Pydantic writes it: which fields take null.
ANSWER_SCHEMA = Hypothesis.model_json_schema()
STRICT_UNSUPPORTED = frozenset(
    {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
)
# Where a schema maps names to subschemas: the names are never keywords.
SCHEMA_MAPS = ("properties", "$defs")

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


def strict_schema(node: Any, model: Any = None) -> Any:
    """A copy of a JSON schema as strict tool use takes it and can compile.

    It drops the keywords strict tool use rejects, and puts additionalProperties
    false on every object, which it requires (Strands leaves that off the top
    level; the models forbid extra keys). And it makes every property required,
    so the answer writes each one, as null where it would have left it out.
    model is the same schema as Pydantic writes it, if given: Strands makes every
    optional field nullable, so where the model takes no null (a list that
    defaults to empty, which Pydantic would refuse as null), the null is dropped
    and the answer writes [] instead.
    """
    return _strict(node, model, (model or {}).get("$defs", {}))


def _strict(node: Any, model: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_strict(item, None, defs) for item in node]
    if not isinstance(node, dict):
        return node
    model = _resolved(model, defs) if isinstance(model, dict) else {}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in SCHEMA_MAPS and isinstance(value, dict):
            subs = model.get(key) if isinstance(model.get(key), dict) else {}
            out[key] = {name: _strict(sub, subs.get(name), defs) for name, sub in value.items()}
        elif key == "items":
            out[key] = _strict(value, model.get("items"), defs)
        elif key not in STRICT_UNSUPPORTED:
            out[key] = _strict(value, None, defs)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        if "properties" in out:
            props = out["properties"]
            known = model.get("properties", {})
            for name in [name for name in props if name not in out.get("required", [])]:
                if name in known and not _takes_null(known[name], defs):
                    props[name] = _without_null(props[name])
            out["required"] = list(props)
    return out


def _resolved(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    return node


def _takes_null(node: dict[str, Any], defs: dict[str, Any]) -> bool:
    node = _resolved(node, defs)
    kind = node.get("type")
    return (
        kind == "null"
        or (isinstance(kind, list) and "null" in kind)
        or any(_takes_null(branch, defs) for branch in node.get("anyOf", []))
    )


def _without_null(node: dict[str, Any]) -> dict[str, Any]:
    out = dict(node)
    kind = out.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"]
        out["type"] = kinds[0] if len(kinds) == 1 else kinds
    if "anyOf" in out:
        branches = [branch for branch in out.pop("anyOf") if branch != {"type": "null"}]
        if len(branches) == 1:
            out.update(branches[0])
        else:
            out["anyOf"] = branches
    return out


class StrictAnswerModel(AnthropicModel):
    """Strands' Anthropic model, sending the answer tool with strict: true and
    every other tool as it was. Strands 1.62.0 sends a tool's name, description,
    and schema only. hooks.count_request calls format_request too, so the count
    covers the same request."""

    def format_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = super().format_request(*args, **kwargs)
        for tool in request["tools"]:
            if tool.get("name") == ANSWER_TOOL:
                tool["input_schema"] = strict_schema(tool["input_schema"], ANSWER_SCHEMA)
                tool["strict"] = True
        return request

    def _structured_output_mechanism(self, *args: Any, **kwargs: Any) -> Any:
        """How Strands asks for the answer when a run ends in text: never as native JSON.

        Sonnet 5.5 and Opus 5.5 answer a forced tool with a 400, and Strands
        1.62.0 then sends the request again with the next mechanism it knows,
        native JSON (output_config.format, tool_choice none): text it validates
        itself, with no trim and no resend, on Pydantic's schema, not the strict
        one above. Here that mechanism is skipped for the one after it: the same
        request with tool_choice auto, so the answer still comes as a call to
        the strict tool and gets the trim, the validation, and the resends every
        answer gets. A model that takes a forced tool is asked as before.
        hooks.count_request asks this too, so a count covers the request sent.
        """
        mechanism = super()._structured_output_mechanism(*args, **kwargs)
        return "auto" if mechanism == "native" else mechanism


def build_model(settings: Settings, http_client: Any = None, *, credentials: Any = None) -> Any:
    """The Strands model for these settings, signed in with the federated
    credentials. http_client is for tests: a fake API."""
    if settings.provider == "bedrock":
        # Waits for the quota: the model ID, its prices, and how thinking is
        # passed get checked then (PROJECT_STATUS, December reminder).
        raise SettingsError("Bedrock isn't wired up yet; its quota is still 0.")
    if credentials is None:
        raise SettingsError("The anthropic provider signs in by federation: it needs federated credentials.")

    # The credentials go to the client as an argument, so it never reads
    # ANTHROPIC_API_KEY or the SDK's own federation variables.
    client_args: dict[str, Any] = {"max_retries": 2, "timeout": 60.0, "credentials": credentials}
    if http_client is not None:
        client_args["http_client"] = http_client
    return StrictAnswerModel(
        client_args=client_args,
        model_id=settings.model_id,
        max_tokens=settings.max_output_tokens,
        params={"thinking": THINKING},
        # Strands' estimate stays local and unused by the cost cap: hooks.py counts
        # each whole request itself, where this flag's count covers only the
        # messages since the last reply, without thinking, and guesses on errors.
        use_native_token_count=False,
    )
