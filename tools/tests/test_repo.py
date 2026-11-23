"""The read tool: one whole file of a release, numbered as stack frames number it, and nothing else.

Every path it doesn't show gets one answer, whether or not such a file exists
anywhere, so the tool can't be used to probe the tree.
"""

import json
from types import SimpleNamespace

import pytest

from sentinelops_core import code
from sentinelops_tools import handlers, repo
from sentinelops_tools.env import ToolEnv
from sentinelops_tools.toolset import make_tools

APP = '"""The app."""\n\nfrom checkout import db\n\n\ndef handle():\n    return db.run()\n'
PRICING = "def apply_promo(subtotal, promo):\n    return subtotal\n"
SNAPSHOT = {
    "1.42.0": {"checkout/app.py": APP, "checkout/pricing.py": PRICING, "checkout/__init__.py": 'VERSION = "1.42.0"\n'},
    "1.43.0": {"checkout/app.py": APP, "checkout/pricing.py": "def apply_promo(subtotal, promo):\n    return 0\n"},
}

HOSTILE = [
    "../sim/flags.py",
    "sim/flags.py",
    "handler.py",
    "checkout/../handler.py",
    "checkout/../sim/flags.py",
    "checkout/%2e%2e/handler.py",
    "checkout%2fapp.py",
    "checkout\\app.py",
    "..\\handler.py",
    "/checkout/app.py",
    "/var/task/code_snapshot.json",
    "C:/checkout/app.py",
    "Checkout/app.py",
    "checkout/App.py",
    "checkout/APP.PY",
    "./checkout/app.py",
    "checkout/./app.py",
    "checkout//app.py",
    "checkout/app.pyc",
    "checkout/app.py/",
    "checkout/app",
    "checkout/",
    "checkout",
    "../../runbooks/bad-deploy.md",
    "../../infra/.build/code-reader/code_snapshot.json",
    "../../.github/workflows/deploy.yml",
    "checkout/app.py\x00",
    "checkout/app.py\x00.md",
    "checkout/app.py\n",
    " checkout/app.py",
    "checkout/app.py ",
    "checkout/" + "a" * 200 + ".py",
    "checkout/missing.py",  # passes the rule, but isn't one of the release's files
    "checkout/__pycache__/app.cpython-312.pyc",
    "checkout/sub/app.py",
    "",
    123,
    ["checkout/app.py"],
    {"path": "checkout/app.py"},
]


@pytest.mark.parametrize("path", HOSTILE, ids=repr)
def test_every_path_it_doesnt_show_gets_one_answer(path: object) -> None:
    assert repo.read(SNAPSHOT, "1.42.0", path) == repo.no_file("1.42.0")


def test_the_one_answer_names_nothing_the_caller_sent() -> None:
    assert "sim" not in repo.no_file("1.42.0") and "flags" not in repo.no_file("1.42.0")


def test_a_file_comes_back_whole_and_numbered_as_a_frame_numbers_it() -> None:
    text = repo.read(SNAPSHOT, "1.42.0", "checkout/app.py")

    assert text.splitlines()[0] == "checkout/app.py at 1.42.0: 7 lines"
    assert text.splitlines()[6] == "     6\tdef handle():"
    assert code.parse_file(text) == code.FileRead("1.42.0", "checkout/app.py", APP)


def test_each_release_shows_its_own_code() -> None:
    assert "return subtotal\n" in code.parse_file(repo.read(SNAPSHOT, "1.42.0", "checkout/pricing.py")).text
    assert "return 0\n" in code.parse_file(repo.read(SNAPSHOT, "1.43.0", "checkout/pricing.py")).text


def test_a_file_one_release_lacks_is_no_file_there() -> None:
    assert repo.read(SNAPSHOT, "1.43.0", "checkout/__init__.py") == repo.no_file("1.43.0")


def test_no_path_lists_the_releases_files_and_isnt_a_read() -> None:
    text = repo.read(SNAPSHOT, "1.42.0")

    assert text.splitlines() == [
        "checkout-api 1.42.0: 3 files",
        "checkout/__init__.py (1 line)",
        "checkout/app.py (7 lines)",
        "checkout/pricing.py (2 lines)",
    ]
    assert code.parse_file(text) is None


@pytest.mark.parametrize("version", ["1.41.0", "1.42", "1.42.0 ", "../1.42.0", "", None, 1.42, ["1.42.0"]], ids=repr)
def test_a_release_with_no_code_says_so_and_echoes_nothing(version: object) -> None:
    assert repo.read(SNAPSHOT, version, "checkout/app.py") == repo.NO_RELEASE
    assert repo.read(SNAPSHOT, version) == repo.NO_RELEASE


def test_with_no_snapshot_it_has_nothing_to_read() -> None:
    assert repo.read(None, "1.42.0", "checkout/app.py") == repo.NO_CODE


def test_the_model_sees_a_version_and_an_optional_path() -> None:
    tool = {t.__name__: t for t in make_tools(ToolEnv(None, None, None, code=SNAPSHOT))}["read_repo_file"]

    assert tool("1.42.0", "checkout/pricing.py") == repo.read(SNAPSHOT, "1.42.0", "checkout/pricing.py")
    assert tool(version="1.42.0") == repo.read(SNAPSHOT, "1.42.0")
    assert "1.43.0" not in (tool.__doc__ or ""), "the spec lists no release: it would hint at one"
    assert "pricing" not in (tool.__doc__ or ""), "nor the bad deploy's file"


def test_gateway_runs_it_as_the_agent_does() -> None:
    env = ToolEnv(None, None, None, code=SNAPSHOT)
    context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "code___read_repo_file"}))

    answer = handlers.handle("read_repo_file", {"version": "1.42.0", "path": "checkout/app.py"}, context, env=env)

    assert answer == {"text": repo.read(SNAPSHOT, "1.42.0", "checkout/app.py")}


def test_its_function_loads_the_snapshot_its_environment_names(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"format": repo.FORMAT, "releases": {v: {"commit": "0" * 40, "files": f} for v, f in SNAPSHOT.items()}}
    (tmp_path / "code_snapshot.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(handlers, "ZIP_ROOT", tmp_path)
    monkeypatch.setattr(handlers, "_clients", None)
    monkeypatch.setenv(repo.CODE_SNAPSHOT_ENV, "code_snapshot.json")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")  # Lambda sets it; boto3 wants one for its clients

    assert handlers._clients_once().code == SNAPSHOT


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"format": 2, "releases": {"1.42.0": {"files": {"checkout/app.py": APP}}}},
        {"format": 1, "releases": {}},
        {"format": 1, "releases": {"latest": {"files": {"checkout/app.py": APP}}}},
        {"format": 1, "releases": {"1.42.0": {"files": {}}}},
        {"format": 1, "releases": {"1.42.0": {"files": {"../handler.py": APP}}}},
        {"format": 1, "releases": {"1.42.0": {"files": {"sim/flags.py": APP}}}},
        {"format": 1, "releases": {"1.42.0": {"files": {"checkout/app.py": 7}}}},
    ],
    ids=range(8),
)
def test_a_snapshot_the_build_wouldnt_write_is_refused(document: object) -> None:
    with pytest.raises(ValueError):
        repo.parse(document)
