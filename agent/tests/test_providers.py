"""The provider setting, the key read from Secrets Manager, federation, and the model built from them."""

import json
import re
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError

from fake_anthropic import ACCESS_TOKEN, FEDERATION_IDS, FakeAnthropic
from providers import (
    API_KEY_SECRET,
    FEDERATION_ENV,
    TOKEN_AUDIENCE,
    TOKEN_SECONDS,
    TOKEN_SIGNING_ALGORITHM,
    Federation,
    FederationFailed,
    KeyUnavailable,
    Settings,
    SettingsError,
    build_model,
    federated_credentials,
    read_api_key,
)
from stubs import WEB_IDENTITY_TOKEN, FakeSession

AGENT_RUNTIME_TF = Path(__file__).resolve().parents[2] / "infra" / "agent_runtime.tf"
FEDERATION = Federation(
    organization_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_ORGANIZATION_ID"],
    service_account_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID"],
    rule_id=FEDERATION_IDS["SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID"],
)

KEY = "sk-ant-api03-" + "TestOnly0" * 9
SECRET = {"SecretId": API_KEY_SECRET}


def secrets_returning(value: str) -> object:
    session = FakeSession()
    session.expect("get_secret_value", SECRET, {"Name": API_KEY_SECRET, "SecretString": value})
    return session.client("secretsmanager")


def test_the_defaults_are_haiku_on_the_anthropic_api() -> None:
    settings = Settings.from_env({})

    assert settings == Settings(
        provider="anthropic", model_id="claude-haiku-5-5", max_output_tokens=16_000, region="us-east-1"
    )


def test_the_model_and_region_come_from_the_environment() -> None:
    settings = Settings.from_env({"SENTINELOPS_MODEL_ID": "claude-sonnet-5-5", "AWS_REGION": "us-west-2"})

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


@pytest.mark.parametrize(
    "stored",
    [KEY, f"  {KEY}\n", json.dumps({"ANTHROPIC_API_KEY": KEY}), json.dumps({"key": KEY, "rotated": 3})],
    ids=["plain", "padded", "json", "json-with-a-number"],
)
def test_the_key_is_read_as_the_console_stores_it(stored: str) -> None:
    assert read_api_key(secrets_returning(stored)) == KEY


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ("not-a-key-at-all", "doesn't hold an Anthropic API key"),
        (json.dumps({"a": KEY, "b": KEY}), "without exactly one string value"),
        ("{" + KEY, "doesn't parse"),
        ("   ", "doesn't hold an Anthropic API key"),
    ],
    ids=["wrong-value", "two-values", "broken-json", "blank"],
)
def test_a_secret_that_is_not_one_key_is_refused_without_showing_it(stored: str, reason: str) -> None:
    with pytest.raises(KeyUnavailable, match=reason) as refused:
        read_api_key(secrets_returning(stored))

    assert KEY not in str(refused.value) and "not-a-key" not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"


def test_an_aws_error_is_reported_by_its_code_only() -> None:
    session = FakeSession()
    arn = "arn:aws:iam::000011112222:user/someone"
    session.fail("get_secret_value", "AccessDeniedException", f"User: {arn} is not authorized")

    with pytest.raises(KeyUnavailable) as refused:
        read_api_key(session.client("secretsmanager"))

    assert str(refused.value) == f"Could not read {API_KEY_SECRET}: AWS returned AccessDeniedException."
    assert refused.value.__cause__ is None and refused.value.__context__ is None, "it carries nothing else"


def test_a_network_error_is_reported_by_its_type_only() -> None:
    class Unreachable:
        def get_secret_value(self, **_: object) -> dict:
            raise EndpointConnectionError(endpoint_url="https://secretsmanager.us-east-1.amazonaws.com/")

    with pytest.raises(KeyUnavailable, match="^Could not read .*: EndpointConnectionError.$"):
        read_api_key(Unreachable())


def test_the_model_gets_the_key_directly_and_the_environment_never_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    model = build_model(Settings(), KEY)

    assert model.client.api_key == KEY
    assert model.client.max_retries == 2
    assert model.config["model_id"] == "claude-haiku-5-5"
    assert model.config["max_tokens"] == 16_000
    assert model.config["params"] == {"thinking": {"type": "adaptive", "display": "summarized"}}
    assert "ANTHROPIC_API_KEY" not in __import__("os").environ


def test_no_key_no_model() -> None:
    with pytest.raises(KeyUnavailable):
        build_model(Settings(), None)


def test_federation_is_off_until_its_ids_are_set() -> None:
    assert Settings.from_env({}).federation is None
    assert Settings.from_env({}).auth == "api_key"

    settings = Settings.from_env({**FEDERATION_IDS, "AWS_REGION": "us-east-1"})

    assert settings.federation == FEDERATION
    assert settings.auth == "federation"


@pytest.mark.parametrize("unset", sorted(FEDERATION_ENV.values()))
def test_half_a_federation_is_refused_never_a_fallback_to_the_key(unset: str) -> None:
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


def test_a_key_and_credentials_together_are_refused() -> None:
    with pytest.raises(SettingsError, match="not both"):
        build_model(Settings(), KEY, credentials=object())


def test_the_runtime_role_may_ask_sts_for_exactly_the_token_the_agent_asks_for() -> None:
    statements = AGENT_RUNTIME_TF.read_text(encoding="utf-8").split("statement {")
    (sign_in,) = [s for s in statements if '"SignInToAnthropic"' in s]

    assert '"sts:GetWebIdentityToken"' in sign_in
    for variable, value in (
        ("sts:IdentityTokenAudience", TOKEN_AUDIENCE),
        ("sts:SigningAlgorithm", TOKEN_SIGNING_ALGORITHM),
        ("sts:DurationSeconds", str(TOKEN_SECONDS)),
    ):
        pattern = rf'variable\s*=\s*"{re.escape(variable)}"\s*values\s*=\s*\["{re.escape(value)}"\]'
        assert re.search(pattern, sign_in), f"{variable} isn't pinned to {value}"
