"""knowledge_base: found by name and checked against this checkout's runbooks, on stubbed AWS calls.

The search's response is built from this checkout's own build, so a runbook
edit here never breaks these tests: the check is what's under test.
"""

import re
from typing import Any

import pytest
from stubs import FakeSession

import knowledge_base
from knowledge_base import NAME, KnowledgeBase, KnowledgeBaseError, connect, script, this_build

KB_ID = "KB12345678"


def summary(name: str = NAME, status: str = "ACTIVE", kb_id: str = KB_ID) -> dict[str, Any]:
    return {"knowledgeBaseId": kb_id, "name": name, "status": status, "updatedAt": "2026-11-23T00:00:00Z"}


def results(documents: list[str], corpus: str) -> dict[str, Any]:
    return {
        "retrievalResults": [
            {
                "content": {"text": "x"},
                "location": {"type": "S3", "s3Location": {"uri": f"s3://sentinelops-runbooks-bucket/sections/{name}"}},
                "metadata": {"corpus": corpus},
                "score": 0.5,
            }
            for name in documents
        ]
    }


def session_for(summaries: list[dict[str, Any]], response: dict[str, Any] | None = None) -> FakeSession:
    session = FakeSession()
    session.expect("list_knowledge_bases", {}, {"knowledgeBaseSummaries": summaries})
    if response is not None:
        ingest = script("ingest_runbooks")
        session.expect(
            "retrieve",
            {
                "knowledgeBaseId": KB_ID,
                "retrievalQuery": {"text": ingest.SEARCH},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": ingest.MAX_RESULTS}},
            },
            response,
        )
    return session


def test_this_build_is_every_section_of_the_checkouts_runbooks() -> None:
    build = this_build()
    corpus = script("build_runbooks").load()
    assert build["corpus"] == corpus.digest and build["sections"] == len(corpus.sections) == len(build["documents"])


def test_a_knowledge_base_holding_this_build_is_connected() -> None:
    build = this_build()
    session = session_for([summary("other-kb", kb_id="KB00000000"), summary()], results(build["documents"], build["corpus"]))

    assert connect(session) == KnowledgeBase(KB_ID, build["corpus"])
    for stubber in session.stubbers.values():
        stubber.assert_no_pending_responses()


def test_a_knowledge_base_holding_other_runbooks_is_refused() -> None:
    build = this_build()
    stale = results(build["documents"], "0123456789ab")
    with pytest.raises(KnowledgeBaseError, match=rf"doesn't hold this checkout's runbooks \(corpus {build['corpus']}\)"):
        connect(session_for([summary()], stale))


def test_a_knowledge_base_missing_a_section_or_holding_an_extra_is_refused() -> None:
    build = this_build()
    for documents in (build["documents"][1:], [*build["documents"], "gone--symptoms.md"]):
        with pytest.raises(KnowledgeBaseError, match="doesn't hold exactly this build"):
            connect(session_for([summary()], results(documents, build["corpus"])))


@pytest.mark.parametrize(
    "summaries, message",
    [
        ([], f"no knowledge base is named {NAME}"),
        ([summary(), summary(kb_id="KB87654321")], f"2 knowledge bases are named {NAME}"),
        ([summary(status="UPDATING")], f"{NAME} is UPDATING, not ACTIVE"),
    ],
)
def test_no_one_active_knowledge_base_by_the_name_is_refused(summaries: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(KnowledgeBaseError, match=message):
        connect(session_for(summaries))


def test_an_aws_error_is_reported_by_its_code_alone() -> None:
    session = FakeSession()
    arn = "arn:aws:iam::000011112222:user/sentinelops-dev"
    session.stubbers["bedrock-agent"].add_client_error("list_knowledge_bases", "AccessDeniedException", f"User: {arn} is not authorized")

    with pytest.raises(KnowledgeBaseError) as caught:
        connect(session)

    assert str(caught.value) == f"couldn't read the knowledge base {NAME}: AWS returned AccessDeniedException"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_runbooks_that_dont_build_are_refused_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    build = script("build_runbooks")

    def broken() -> Any:
        raise build.FormatError("bad-deploy.md: no ## Diagnosis")

    monkeypatch.setattr(build, "load", broken)
    with pytest.raises(KnowledgeBaseError, match="this checkout's runbooks don't build: bad-deploy.md"):
        connect(FakeSession())  # a call would fail the stubber: nothing is asked of AWS


def test_the_name_is_the_one_terraform_gives_it() -> None:
    text = (knowledge_base.TOOLS.parent / "infra" / "runbooks_kb.tf").read_text(encoding="utf-8")
    found = re.search(r'resource "aws_bedrockagent_knowledge_base" "runbooks" \{\s*name\s*=\s*"([^"]+)"', text)
    assert found and found[1] == NAME
