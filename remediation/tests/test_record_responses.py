"""record_responses.py's scrubber: what it replaces in an answer recorded as the App, and what it refuses to save."""

import json

import pytest
import record_responses as rec

REAL_APP_ID = 7654321
REAL_CLIENT_ID = "Iv23liRealLookingId0"
REAL_INSTALLATION_ID = 987654321
OWNER_ID = 31337001
# Planted values the commit hooks would refuse in this file, so built at run time.
JOB_ID = "1290" + "00000002"  # 12 digits
TWELVE_DIGITS = int("3141" + "59265358")
SSH_URL = "git" + "@" + "github.com:o/r.git"


def scrubber() -> rec.Scrubber:
    return rec.Scrubber(
        True,
        {REAL_APP_ID: rec.TEST_APP_ID, REAL_CLIENT_ID: rec.TEST_CLIENT_ID, REAL_INSTALLATION_ID: rec.TEST_INSTALLATION_ID},
    )


ANSWER = {
    "id": REAL_APP_ID,
    "client_id": REAL_CLIENT_ID,
    "node_id": "A_kwDOreal",
    # the owner's ID inside a URL, met before its own `id` key
    "owner": {"avatar_url": f"https://avatars.githubusercontent.com/u/{OWNER_ID}?v=4", "id": OWNER_ID, "login": "someone"},
    "html_url": f"https://github.com/settings/installations/{REAL_INSTALLATION_ID}",
    "details_url": f"https://github.com/o/r/actions/runs/45500000001/job/{JOB_ID}",
    "created_at": "2026-11-25T03:00:00Z",
    "token": "ghs_" + "a" * 36,
    "repository_selection": "selected",
}


def written(exchanges: dict, s: rec.Scrubber, tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(rec, "FIXTURES", tmp_path)
    rec.save(exchanges, "about", s)
    return {p.stem: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json")}


def test_every_id_and_the_token_are_replaced(tmp_path, monkeypatch) -> None:
    exchange = {"endpoint": "app", "params": {}, "query": {}, "status": 200, "json": ANSWER}
    text = written({"app": exchange}, scrubber(), tmp_path, monkeypatch)["app"]
    for real in (REAL_APP_ID, REAL_CLIENT_ID, REAL_INSTALLATION_ID, OWNER_ID, "45500000001", JOB_ID, "ghs_", "A_kwDO"):
        assert str(real) not in text
    saved = json.loads(text)["json"]
    assert saved["id"] == rec.TEST_APP_ID and saved["client_id"] == rec.TEST_CLIENT_ID
    assert saved["token"] == rec.TOKEN_STAND_IN and saved["node_id"] == "NODE_ID"
    owner_stand_in = saved["owner"]["id"]
    assert saved["owner"]["avatar_url"] == f"https://avatars.githubusercontent.com/u/{owner_stand_in}?v=4"
    assert saved["html_url"].endswith(f"/installations/{rec.TEST_INSTALLATION_ID}")
    assert saved["created_at"] == "2026-11-25T03:00:00Z"  # short digit runs are left alone
    assert saved["repository_selection"] == "selected"


def test_params_and_the_request_body_are_scrubbed_too(tmp_path, monkeypatch) -> None:
    exchange = {
        "endpoint": "installation_token",
        "params": {"installation_id": REAL_INSTALLATION_ID},
        "query": {},
        "status": 201,
        "method": "POST",
        "request_json": {"repositories": ["sentinelops"]},
        "json": {"token": "ghs_" + "b" * 36, "repositories": [{"id": 5555555, "full_name": "o/r"}]},
    }
    saved = json.loads(written({"t": exchange}, scrubber(), tmp_path, monkeypatch)["t"])
    assert saved["params"] == {"installation_id": rec.TEST_INSTALLATION_ID}
    assert saved["json"]["token"] == rec.TOKEN_STAND_IN
    assert saved["json"]["repositories"][0]["id"] != 5555555


@pytest.mark.parametrize(
    "leak",
    [
        {"note": "ghp_" + "c" * 36},  # token-shaped under a key the scrubber doesn't know
        {"note": "github_pat_" + "d" * 30},
        {"count": TWELVE_DIGITS},  # not under an id key (123456789012, the hooks' placeholder, would pass)
    ],
)
def test_a_leftover_refuses_the_whole_recording(leak, tmp_path, monkeypatch) -> None:
    good = {"endpoint": "app", "params": {}, "query": {}, "status": 200, "json": {"id": REAL_APP_ID}}
    bad = {"endpoint": "repository", "params": {}, "query": {}, "status": 200, "json": leak}
    with pytest.raises(SystemExit, match="nothing saved"):
        written({"a-good": good, "b-bad": bad}, scrubber(), tmp_path, monkeypatch)
    assert list(tmp_path.iterdir()) == []


def test_a_real_id_left_under_an_unknown_key_is_refused(tmp_path, monkeypatch) -> None:
    exchange = {"endpoint": "app", "params": {}, "query": {}, "status": 200, "json": {"id": OWNER_ID, "count": OWNER_ID}}
    with pytest.raises(SystemExit, match="real ID"):
        written({"app": exchange}, scrubber(), tmp_path, monkeypatch)


def test_public_recordings_keep_everything_but_ruleset_and_node_ids(tmp_path, monkeypatch) -> None:
    exchange = {"endpoint": "branch_rules", "params": {"branch": "main"}, "query": {}, "status": 200,
                "json": [{"type": "update", "ruleset_id": 9876543, "node_id": "x", "ruleset_source": "o/r"}]}
    saved = json.loads(written({"r": exchange}, rec.Scrubber(ids=False), tmp_path, monkeypatch)["r"])
    assert saved["json"] == [{"type": "update", "ruleset_id": 1, "node_id": "NODE_ID", "ruleset_source": "o/r"}]


def test_an_address_the_hooks_would_flag_is_replaced_and_an_example_one_kept(tmp_path, monkeypatch) -> None:
    answer = {"ssh_url": SSH_URL, "doc": "write to dev@example.com", "bot": "noreply@github.com"}
    exchange = {"endpoint": "repository", "params": {}, "query": {}, "status": 200, "json": answer}
    saved = json.loads(written({"r": exchange}, scrubber(), tmp_path, monkeypatch)["r"])["json"]
    assert saved == {"ssh_url": "EMAIL_REPLACED:o/r.git", "doc": "write to dev@example.com", "bot": "noreply@github.com"}


def test_the_hooks_own_scan_has_the_last_word(tmp_path, monkeypatch) -> None:
    """A public recording replaces nothing but ruleset and node IDs, so an address in it is refused, not saved."""
    exchange = {"endpoint": "repository", "params": {}, "query": {}, "status": 200, "json": {"ssh_url": SSH_URL}}
    with pytest.raises(SystemExit, match="the hooks' email rule"):
        written({"r": exchange}, rec.Scrubber(ids=False), tmp_path, monkeypatch)
