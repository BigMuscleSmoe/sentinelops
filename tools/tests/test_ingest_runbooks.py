"""The deploy's ingestion step, against botocore's Stubber: no AWS.

The responses are shaped by the service model (the Stubber checks them): a
job that runs, completes, or fails, and a search that returns this build, an
older one, or the wrong documents.
"""

from datetime import UTC, datetime

import pytest
from botocore.stub import Stubber
from conftest import client

import ingest_runbooks as ir

KB = "KBTEST0001"
DS = "DSTEST0001"
JOB = "JOBTEST001"
AT = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
DOCS = [f"runbook-{n}--{section}.md" for n in range(2) for section in ("symptoms", "diagnosis")]
MANIFEST = {"corpus": "ab2862d71ff4", "runbooks": 2, "sections": len(DOCS), "documents": sorted(DOCS)}
IDS = {"knowledgeBaseId": KB, "dataSourceId": DS}
FULL = {
    "numberOfDocumentsScanned": 4,
    "numberOfMetadataDocumentsScanned": 4,
    "numberOfNewDocumentsIndexed": 4,
    "numberOfModifiedDocumentsIndexed": 0,
    "numberOfMetadataDocumentsModified": 0,
    "numberOfDocumentsDeleted": 0,
    "numberOfDocumentsFailed": 0,
}


def job(status: str, statistics: dict | None = None, reasons: list[str] | None = None) -> dict:
    body = {**IDS, "ingestionJobId": JOB, "status": status, "startedAt": AT, "updatedAt": AT}
    if statistics is not None:
        body["statistics"] = statistics
    if reasons:
        body["failureReasons"] = reasons
    return {"ingestionJob": body}


def result(name: str, corpus: str = MANIFEST["corpus"]) -> dict:
    return {
        "content": {"type": "TEXT", "text": "# Runbook: Example\n\n## Symptoms\n\nText."},
        "location": {"type": "S3", "s3Location": {"uri": f"s3://sentinelops-runbooks-test/{name}"}},
        "metadata": {"corpus": corpus, "runbook": name.split("--")[0] + ".md"},
        "score": 0.5,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def agent():
    c = client("bedrock-agent")
    with Stubber(c) as stub:
        yield c, stub
        stub.assert_no_pending_responses()


@pytest.fixture
def runtime():
    c = client("bedrock-agent-runtime")
    with Stubber(c) as stub:
        yield c, stub
        stub.assert_no_pending_responses()


def expect_start(stub: Stubber, response: dict) -> None:
    stub.add_response("start_ingestion_job", response, {**IDS, "description": "corpus ab2862d71ff4"})


def expect_poll(stub: Stubber, response: dict) -> None:
    stub.add_response("get_ingestion_job", response, {**IDS, "ingestionJobId": JOB})


def expect_search(stub: Stubber, results: list[dict], **extra) -> None:
    params = {
        "knowledgeBaseId": KB,
        "retrievalQuery": {"text": ir.SEARCH},
        "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 100}},
    }
    stub.add_response("retrieve", {"retrievalResults": results, **extra}, params)


# The ingestion job


def test_a_job_is_polled_until_it_completes(agent) -> None:
    c, stub = agent
    clock = Clock()
    expect_start(stub, job("STARTING"))
    expect_poll(stub, job("IN_PROGRESS"))
    expect_poll(stub, job("COMPLETE", FULL))
    stats = ir.ingest(c, KB, DS, MANIFEST, sleep=clock.sleep, clock=clock)
    assert stats == FULL
    assert clock.sleeps == [ir.POLL_S, ir.POLL_S]


def test_an_unchanged_corpus_passes(agent) -> None:
    """A second deploy with nothing changed: every document scanned, none indexed again."""
    c, stub = agent
    unchanged = {**FULL, "numberOfNewDocumentsIndexed": 0, "numberOfDocumentsSkipped": 4}
    expect_start(stub, job("STARTING"))
    expect_poll(stub, job("COMPLETE", unchanged))
    assert ir.ingest(c, KB, DS, MANIFEST, sleep=lambda s: None) == unchanged


def test_a_failed_job_is_refused_with_its_reasons_masked(agent) -> None:
    c, stub = agent
    expect_start(stub, job("STARTING"))
    expect_poll(stub, job("FAILED", reasons=["Access denied for arn:aws:iam::123456789012:role/sentinelops-runbooks-kb"]))
    with pytest.raises(ir.IngestError) as refused:
        ir.ingest(c, KB, DS, MANIFEST, sleep=lambda s: None)
    message = str(refused.value)
    assert message.startswith("the ingestion job ended FAILED")
    assert "role/sentinelops-runbooks-kb" in message
    assert "123456789012" not in message and "<acct>" in message


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"numberOfDocumentsFailed": 1}, "1 documents failed"),
        ({"numberOfDocumentsScanned": 3}, "3 documents scanned, not 4"),
        ({"numberOfMetadataDocumentsScanned": 0}, "0 metadata files scanned, not 4"),
    ],
)
def test_a_completed_job_whose_counts_are_off_is_refused(agent, change, expected) -> None:
    c, stub = agent
    expect_start(stub, job("STARTING"))
    expect_poll(stub, job("COMPLETE", {**FULL, **change}))
    with pytest.raises(ir.IngestError, match=expected):
        ir.ingest(c, KB, DS, MANIFEST, sleep=lambda s: None)


def test_a_completed_job_with_failure_reasons_is_refused(agent) -> None:
    c, stub = agent
    expect_start(stub, job("STARTING"))
    expect_poll(stub, job("COMPLETE", FULL, reasons=["A document was too large"]))
    with pytest.raises(ir.IngestError, match="A document was too large"):
        ir.ingest(c, KB, DS, MANIFEST, sleep=lambda s: None)


def test_a_job_still_running_at_the_deadline_is_refused(agent) -> None:
    c, stub = agent
    clock = Clock()
    expect_start(stub, job("STARTING"))
    for _ in range(3):
        expect_poll(stub, job("IN_PROGRESS"))
    with pytest.raises(ir.IngestError, match="still IN_PROGRESS after 15 s"):
        ir.ingest(c, KB, DS, MANIFEST, timeout_s=15, sleep=clock.sleep, clock=clock)
    assert clock.now == 15


# The search


def test_a_search_returning_exactly_this_build_passes(runtime) -> None:
    c, stub = runtime
    expect_search(stub, [result(name) for name in reversed(DOCS)])
    assert ir.check_corpus(c, KB, MANIFEST) == 4


@pytest.mark.parametrize(
    "results, expected",
    [
        ([result(name) for name in DOCS[:3]], "1 sections didn't come back: runbook-1--diagnosis.md"),
        ([result(name) for name in DOCS] + [result("gone--symptoms.md")], "1 documents aren't in this build: gone--symptoms.md"),
        ([result(name) for name in DOCS] + [result(DOCS[0])], "1 documents came back more than once"),
        ([result(DOCS[0], corpus="9ec4bb37c0dc")] + [result(name) for name in DOCS[1:]], "1 results aren't tagged corpus ab2862d71ff4"),
    ],
)
def test_a_search_that_shows_another_build_is_refused(runtime, results, expected) -> None:
    c, stub = runtime
    expect_search(stub, results)
    with pytest.raises(ir.IngestError, match=expected):
        ir.check_corpus(c, KB, MANIFEST)


def test_more_results_than_one_search_returns_is_refused(runtime) -> None:
    c, stub = runtime
    expect_search(stub, [result(name) for name in DOCS], nextToken="more")
    with pytest.raises(ir.IngestError, match="more than 100 results"):
        ir.check_corpus(c, KB, MANIFEST)


# main


def write_manifest(tmp_path):
    import json

    path = tmp_path / "runbook-sections.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    return path


def test_main_ingests_and_checks(tmp_path, monkeypatch, capsys, agent, runtime) -> None:
    (a, agent_stub), (r, runtime_stub) = agent, runtime
    monkeypatch.setattr(ir, "make_clients", lambda region: (a, r))
    expect_start(agent_stub, job("COMPLETE", FULL))
    expect_search(runtime_stub, [result(name) for name in DOCS])
    argv = ["--knowledge-base-id", KB, "--data-source-id", DS, "--manifest", str(write_manifest(tmp_path))]
    assert ir.main(argv) == 0
    out = capsys.readouterr().out
    assert "Ingested corpus ab2862d71ff4: 4 documents scanned, 4 new" in out
    assert "holds corpus ab2862d71ff4: 4 sections, and nothing else" in out
    assert KB not in out and DS not in out


def test_main_prints_an_aws_error_by_its_code_only(tmp_path, monkeypatch, capsys, agent) -> None:
    a, stub = agent
    monkeypatch.setattr(ir, "make_clients", lambda region: (a, None))
    stub.add_client_error(
        "start_ingestion_job",
        service_error_code="ConflictException",
        service_message="A job is running for arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBTEST0001",
        http_status_code=409,
    )
    argv = ["--knowledge-base-id", KB, "--data-source-id", DS, "--manifest", str(write_manifest(tmp_path))]
    assert ir.main(argv) == 1
    err = capsys.readouterr().err
    assert err.strip() == "Runbooks not ingested: StartIngestionJob answered ConflictException"


def test_main_without_a_build_says_to_build(tmp_path, capsys) -> None:
    argv = ["--knowledge-base-id", KB, "--data-source-id", DS, "--manifest", str(tmp_path / "missing.json")]
    assert ir.main(argv) == 1
    assert "run python tools/build_runbooks.py first" in capsys.readouterr().err


def test_the_real_manifest_matches_the_build(tmp_path) -> None:
    """What the deploy will check against: the builder's manifest, as load_manifest reads it."""
    import build_runbooks as rb

    corpus = rb.load()
    manifest = ir.load_manifest(rb.write(corpus, tmp_path / "runbook-sections"))
    assert manifest["corpus"] == corpus.digest
    assert manifest["sections"] == len(manifest["documents"]) == 35
