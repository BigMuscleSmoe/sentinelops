"""The provider setting, the key read from Secrets Manager, and the model built from them."""

import json

import pytest
from botocore.exceptions import EndpointConnectionError

from providers import (
    API_KEY_SECRET,
    KeyUnavailable,
    Settings,
    SettingsError,
    build_model,
    read_api_key,
)
from stubs import FakeSession

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
