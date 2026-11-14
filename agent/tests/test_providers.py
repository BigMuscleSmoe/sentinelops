"""The provider setting, federation, and the model built from them."""

import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError

from fake_anthropic import ACCESS_TOKEN, FEDERATION_IDS, FakeAnthropic, signed_in
from providers import (
    FEDERATION_ENV,
    FEDERATION_ROLE_ENV,
    TOKEN_AUDIENCE,
    TOKEN_SECONDS,
    TOKEN_SIGNING_ALGORITHM,
    Federation,
    FederationFailed,
    Settings,
    SettingsError,
    StrictAnswerModel,
    build_model,
    federated_credentials,
    strict_schema,
)
from stubs import LOCAL_ROLE_ARN, ROLE_KEYS, WEB_IDENTITY_TOKEN, FakeSession

INFRA = Path(__file__).resolve().parents[2] / "infra"
AGENT_RUNTIME_TF = INFRA / "agent_runtime.tf"
LOCAL_SIGN_IN_TF = INFRA / "local_sign_in.tf"
FEDERATION = Federation(
    organization_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_ORGANIZATION_ID"],
    service_account_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID"],
    rule_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID"],
)
LOCAL_FEDERATION = replace(FEDERATION, role_arn=LOCAL_ROLE_ARN)


def test_the_defaults_are_haiku_on_the_anthropic_api() -> None:
    settings = Settings.from_env(FEDERATION_IDS)

    assert settings == Settings(
        provider="anthropic",
        model_id="claude-haiku-5-5",
        max_output_tokens=16_000,
        region="us-east-1",
        federation=FEDERATION,
    )


def test_the_model_and_region_come_from_the_environment() -> None:
    settings = Settings.from_env(
        {**FEDERATION_IDS, "SENTINELOPS_MODEL_ID": "claude-sonnet-5-5", "AWS_REGION": "us-west-2"}
    )

    assert (settings.model_id, settings.region) == ("claude-sonnet-5-5", "us-west-2")


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(SettingsError, match="one of anthropic, bedrock"):
        Settings.from_env({"SENTINELOPS_MODEL_PROVIDER": "openai"})


def test_bedrock_needs_a_model_id_and_is_not_wired_yet() -> None:
    with pytest.raises(SettingsError, match="bedrock has no default model"):
        Settings.from_env({"SENTINELOPS_MODEL_PROVIDER": "bedrock"})

    settings = Settings.from_env(
        {"SENTINELOPS_MODEL_PROVIDER": "bedrock", "SENTINELOPS_MODEL_ID": "anthropic.claude-haiku-5-5"}
    )
    with pytest.raises(SettingsError, match="quota"):
        build_model(settings)


def test_the_model_gets_the_settings_and_the_credentials_directly() -> None:
    credentials = signed_in()

    model = build_model(Settings(federation=FEDERATION), credentials=credentials)

    assert model.client.credentials is credentials
    assert model.client.max_retries == 2
    assert model.config["model_id"] == "claude-haiku-5-5"
    assert model.config["max_tokens"] == 16_000
    assert model.config["params"] == {"thinking": {"type": "adaptive", "display": "summarized"}}


def test_no_credentials_no_model() -> None:
    with pytest.raises(SettingsError, match="needs federated credentials"):
        build_model(Settings(federation=FEDERATION))


def test_the_anthropic_api_needs_the_federation_ids() -> None:
    with pytest.raises(SettingsError, match="by federation only: set SENTINELOPS_ANTHROPIC_ORGANIZATION_ID"):
        Settings.from_env({})

    settings = Settings.from_env({**FEDERATION_IDS, "AWS_REGION": "us-east-1"})

    assert settings.federation == FEDERATION
    assert settings.auth == "federation"


@pytest.mark.parametrize("unset", sorted(FEDERATION_ENV.values()))
def test_half_a_federation_is_refused(unset: str) -> None:
    ids = {name: value for name, value in FEDERATION_IDS.items() if name != unset}

    with pytest.raises(SettingsError, match=f"half set up: set {unset} as well"):
        Settings.from_env(ids)


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID", "svac_01Swapped", "must start with fdrl_"),
        ("SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID", "fdrl_01Swapped", "must start with svac_"),
        ("SENTINELOPS_ANTHROPIC_ORGANIZATION_ID", "my-org", "the organization's UUID"),
    ],
    ids=["rule", "service-account", "organization"],
)
def test_an_id_in_the_wrong_place_is_refused_before_any_call(name: str, value: str, reason: str) -> None:
    with pytest.raises(SettingsError, match=reason):
        Settings.from_env({**FEDERATION_IDS, name: value})


def test_a_role_to_sign_in_as_comes_with_the_ids() -> None:
    assert Settings.from_env(FEDERATION_IDS).federation.role_arn is None, "the runtime asks as itself"
    assert Settings.from_env({**FEDERATION_IDS, FEDERATION_ROLE_ENV: LOCAL_ROLE_ARN}).federation == LOCAL_FEDERATION

    with pytest.raises(SettingsError, match="the federation IDs aren't"):
        Settings.from_env({FEDERATION_ROLE_ENV: LOCAL_ROLE_ARN})


def test_a_role_arn_that_is_not_a_role_is_refused_without_showing_it() -> None:
    user = "arn:aws:iam::000011112222:user/sentinelops-dev"

    with pytest.raises(SettingsError, match="must be an IAM role's ARN") as refused:
        Settings.from_env({**FEDERATION_IDS, FEDERATION_ROLE_ENV: user})

    assert "000011112222" not in str(refused.value)


def test_the_first_token_is_minted_before_the_run_and_used_first() -> None:
    session, fake = FakeSession(), FakeAnthropic([])
    session.expect_web_identity_token()

    credentials = federated_credentials(FEDERATION, session, http_client=fake.token_client())

    session.assert_all_called()
    assert [r["body"] for r in fake.token_requests] == [
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": WEB_IDENTITY_TOKEN,
            "federation_rule_id": FEDERATION.rule_id,
            "organization_id": FEDERATION.organization_id,
            "service_account_id": FEDERATION.service_account_id,
        }
    ]
    assert credentials().token == ACCESS_TOKEN
    assert len(fake.token_requests) == 1, "the client's first token is the one minted up front"


@pytest.mark.parametrize("force_refresh", [False, True], ids=["refresh", "after-a-401"])
def test_every_later_token_is_a_new_jwt_and_a_new_exchange(force_refresh: bool) -> None:
    session, fake = FakeSession(), FakeAnthropic([])
    second = WEB_IDENTITY_TOKEN.replace("N0tA", "Next")
    session.expect_web_identity_token()
    session.expect_web_identity_token(second)
    credentials = federated_credentials(FEDERATION, session, http_client=fake.token_client())

    if force_refresh:
        # After a 401 the client's cache asks with force_refresh: the token minted up front is skipped.
        credentials(force_refresh=True)
    else:
        credentials()
        credentials()

    session.assert_all_called()
    assert [r["body"]["assertion"] for r in fake.token_requests] == [WEB_IDENTITY_TOKEN, second]


def test_a_local_run_asks_sts_for_each_jwt_as_the_role_it_assumes_afresh() -> None:
    session, fake = FakeSession(), FakeAnthropic([])
    second = WEB_IDENTITY_TOKEN.replace("N0tA", "Next")
    for token in (WEB_IDENTITY_TOKEN, second):
        session.expect_assume_role()
        session.expect_web_identity_token(token)

    credentials = federated_credentials(LOCAL_FEDERATION, session, http_client=fake.token_client())
    credentials(force_refresh=True)

    session.assert_all_called()
    assert [r["body"]["assertion"] for r in fake.token_requests] == [WEB_IDENTITY_TOKEN, second]
    role_clients = [key for service, key in session.made if service == "sts" and key is not None]
    assert role_clients == [ROLE_KEYS["AccessKeyId"]] * 2, "each JWT asked for under the role's keys"


def test_a_role_that_cannot_be_assumed_is_reported_by_its_code_only() -> None:
    session, fake = FakeSession(), FakeAnthropic([])
    session.fail("assume_role", "AccessDenied", f"User is not authorized to perform sts:AssumeRole on {LOCAL_ROLE_ARN}")

    with pytest.raises(FederationFailed) as refused:
        federated_credentials(LOCAL_FEDERATION, session, http_client=fake.token_client())

    assert str(refused.value) == "Could not sign in to the Anthropic API by federation: STS returned AccessDenied."
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"
    assert fake.token_requests == []


@pytest.mark.parametrize(
    "code", ["AccessDenied", "OutboundWebIdentityFederationDisabledException"], ids=["denied", "switched-off"]
)
def test_an_sts_failure_is_reported_by_its_code_only(code: str) -> None:
    session, fake = FakeSession(), FakeAnthropic([])
    arn = "arn:aws:sts::000011112222:assumed-role/sentinelops-agent-runtime/s-1"
    session.fail("get_web_identity_token", code, f"User: {arn} is not authorized")

    with pytest.raises(FederationFailed) as refused:
        federated_credentials(FEDERATION, session, http_client=fake.token_client())

    assert str(refused.value) == f"Could not sign in to the Anthropic API by federation: STS returned {code}."
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"
    assert fake.token_requests == []


def test_a_refused_exchange_is_reported_by_its_status_and_request_id_never_the_jwt() -> None:
    session = FakeSession()
    session.expect_web_identity_token()
    denied = httpx.Response(
        401,
        headers={"request-id": "req_011TestOnly"},
        json={"type": "error", "error": {"type": "authentication_error", "message": "Authentication failed"}},
    )
    fake = FakeAnthropic([], tokens=[denied])

    with pytest.raises(FederationFailed) as refused:
        federated_credentials(FEDERATION, session, http_client=fake.token_client())

    assert str(refused.value) == (
        "Could not sign in to the Anthropic API by federation: "
        "the token exchange failed (HTTP 401, request req_011TestOnly)."
    )
    assert WEB_IDENTITY_TOKEN[20:50] not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"
    assert len(fake.token_requests) == 1, "no retry before the run: the refusal says why"


def test_an_unreachable_token_endpoint_is_reported_as_no_response() -> None:
    session = FakeSession()
    session.expect_web_identity_token()
    fake = FakeAnthropic([], tokens=[httpx.ConnectError("[Errno 11001] getaddrinfo failed")])

    with pytest.raises(FederationFailed) as refused:
        federated_credentials(FEDERATION, session, http_client=fake.token_client())

    assert str(refused.value) == (
        "Could not sign in to the Anthropic API by federation: the token exchange failed (no response)."
    )
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"


def test_an_unreachable_sts_is_reported_by_the_error_type_only() -> None:
    class Unreachable:
        def client(self, service: str, **_: object) -> object:
            return self

        def get_web_identity_token(self, **_: object) -> dict:
            raise EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com/")

    fake = FakeAnthropic([])

    with pytest.raises(FederationFailed) as refused:
        federated_credentials(FEDERATION, Unreachable(), http_client=fake.token_client())

    assert str(refused.value) == (
        "Could not sign in to the Anthropic API by federation: STS: EndpointConnectionError."
    )
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"
    assert fake.token_requests == []


def test_the_model_signs_in_with_the_credentials_and_reads_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-left-over-from-a-shell")
    session, fake = FakeSession(), FakeAnthropic([])
    session.expect_web_identity_token()
    credentials = federated_credentials(FEDERATION, session, http_client=fake.token_client())

    model = build_model(Settings(federation=FEDERATION), credentials=credentials)

    assert model.client.credentials is credentials
    assert model.client.api_key is None, "a key in the environment never shadows federation"


@pytest.mark.parametrize("tf", [AGENT_RUNTIME_TF, LOCAL_SIGN_IN_TF], ids=["runtime", "local"])
def test_each_sign_in_role_may_ask_sts_for_exactly_the_token_the_agent_asks_for(tf: Path) -> None:
    statements = tf.read_text(encoding="utf-8").split("statement {")
    (sign_in,) = [s for s in statements if '"SignInToAnthropic"' in s]

    assert '"sts:GetWebIdentityToken"' in sign_in
    for variable, value in (
        ("sts:IdentityTokenAudience", TOKEN_AUDIENCE),
        ("sts:SigningAlgorithm", TOKEN_SIGNING_ALGORITHM),
        ("sts:DurationSeconds", str(TOKEN_SECONDS)),
    ):
        pattern = rf'variable\s*=\s*"{re.escape(variable)}"\s*values\s*=\s*\["{re.escape(value)}"\]'
        assert re.search(pattern, sign_in), f"{variable} isn't pinned to {value}"


def test_only_sentinelops_dev_may_assume_the_local_role_and_it_may_only_sign_in() -> None:
    text = LOCAL_SIGN_IN_TF.read_text(encoding="utf-8")

    assert re.findall(r'actions\s*=\s*\[([^\]]*)\]', text) == ['"sts:AssumeRole"', '"sts:GetWebIdentityToken"']
    assert re.findall(r"identifiers\s*=\s*\[([^\]]*)\]", text) == [
        '"arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/sentinelops-dev"'
    ]


def test_the_model_sends_the_answer_tool_strict() -> None:
    model = build_model(Settings(federation=FEDERATION), credentials=signed_in())

    assert isinstance(model, StrictAnswerModel)


def test_the_strict_schema_drops_what_strict_tool_use_rejects_and_requires_every_field_at_any_depth() -> None:
    schema = {
        "type": "object",
        "properties": {
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "How sure."},
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object", "properties": {"note": {"type": "string", "minLength": 1}}},
            },
            "count": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
        },
        "required": ["confidence"],
    }

    assert strict_schema(schema) == {
        "type": "object",
        "properties": {
            "confidence": {"type": "number", "description": "How sure."},
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "additionalProperties": False,
                    "required": ["note"],
                },
            },
            "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["confidence", "items", "count"],
        "additionalProperties": False,
    }


def test_the_strict_schema_leaves_null_only_where_the_model_takes_it() -> None:
    # As Strands sends it: every optional field nullable, the nested model inlined.
    sent = {
        "type": "object",
        "properties": {
            "notes": {"type": ["array", "null"], "items": {"type": "string"}},
            "runbook": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"at": {"type": ["string", "null"]}, "tags": {"type": ["array", "null"]}},
                    "required": [],
                },
            },
        },
        "required": ["items"],
    }
    # As Pydantic writes it: a list defaulting to empty takes no null; an optional string does.
    model = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "at": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            }
        },
        "type": "object",
        "properties": {
            "notes": {"type": "array", "items": {"type": "string"}},
            "runbook": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
        },
        "required": ["items"],
    }

    out = strict_schema(sent, model)

    assert out["required"] == ["notes", "runbook", "items"]
    assert out["properties"]["notes"] == {"type": "array", "items": {"type": "string"}}
    assert out["properties"]["runbook"] == {"type": ["string", "null"]}
    item = out["properties"]["items"]["items"]
    assert item["required"] == ["at", "tags"]
    assert item["properties"] == {"at": {"type": ["string", "null"]}, "tags": {"type": "array"}}


def test_the_strict_schema_keeps_a_property_named_like_a_keyword_and_leaves_its_input_alone() -> None:
    schema = {"type": "object", "properties": {"minimum": {"type": "number"}}, "additionalProperties": False}
    before = repr(schema)

    assert strict_schema(schema)["properties"] == {"minimum": {"type": "number"}}
    assert repr(schema) == before
