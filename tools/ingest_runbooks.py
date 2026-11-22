"""Sync the runbook knowledge base with this build, and check it holds exactly this build.

The deploy runs this after the apply has uploaded the section documents that
tools/build_runbooks.py wrote. It starts an ingestion job on the knowledge
base's one data source, waits up to 5 minutes for it, and fails on a job that
doesn't complete, a document that failed, or a scan that didn't see every
section and its metadata file. Then one search must return every section of
this build and nothing else, each tagged with this build's corpus digest: the
counters' meanings aren't all documented, and that check doesn't depend on
them.

Only AWS's error code and the operation are printed, never an error message,
which can name an ARN and with it the account ID: the deploy's logs are
public. The job's own failure reasons are printed with any 12-digit number
masked.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "infra" / ".build" / "runbook-sections.json"

TIMEOUT_S = 300
POLL_S = 5
RUNNING = ("STARTING", "IN_PROGRESS", "STOPPING")
# Any query does: the check is which documents come back, not their order.
# Retrieve returns at most 100, more than the index should ever hold, so a
# stale document past the build's would show.
SEARCH = "checkout requests failing"
MAX_RESULTS = 100
REASONS_SHOWN = 5
REASON_CHARS = 300


class IngestError(Exception):
    """The knowledge base doesn't hold this build. The message says what's wrong."""


def masked(text: str) -> str:
    return re.sub(r"\d{12}", "<acct>", text)[:REASON_CHARS]


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise IngestError(f"no manifest at {path}: run python tools/build_runbooks.py first") from None
    if manifest["sections"] != len(manifest["documents"]):
        raise IngestError(f"the manifest lists {len(manifest['documents'])} documents for {manifest['sections']} sections")
    return manifest


def ingest(
    agent: Any,
    knowledge_base_id: str,
    data_source_id: str,
    manifest: dict[str, Any],
    *,
    timeout_s: float = TIMEOUT_S,
    poll_s: float = POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Run one ingestion job to its end, and return its statistics if every section was read and none failed."""
    ids = {"knowledgeBaseId": knowledge_base_id, "dataSourceId": data_source_id}
    job = agent.start_ingestion_job(**ids, description=f"corpus {manifest['corpus']}")["ingestionJob"]
    deadline = clock() + timeout_s
    while job["status"] in RUNNING:
        if clock() >= deadline:
            raise IngestError(f"the ingestion job was still {job['status']} after {timeout_s:.0f} s")
        sleep(poll_s)
        job = agent.get_ingestion_job(**ids, ingestionJobId=job["ingestionJobId"])["ingestionJob"]

    reasons = [masked(reason) for reason in job.get("failureReasons", [])]
    if job["status"] != "COMPLETE":
        raise IngestError(f"the ingestion job ended {job['status']}" + "".join(f"\n  {r}" for r in reasons[:REASONS_SHOWN]))

    stats = {key: int(value) for key, value in job.get("statistics", {}).items()}
    sections = manifest["sections"]
    problems = []
    if stats.get("numberOfDocumentsFailed", 0):
        problems.append(f"{stats['numberOfDocumentsFailed']} documents failed")
    if stats.get("numberOfDocumentsScanned", 0) != sections:
        problems.append(f"{stats.get('numberOfDocumentsScanned', 0)} documents scanned, not {sections}")
    if stats.get("numberOfMetadataDocumentsScanned", 0) != sections:
        problems.append(f"{stats.get('numberOfMetadataDocumentsScanned', 0)} metadata files scanned, not {sections}")
    if reasons:
        problems.extend(reasons[:REASONS_SHOWN])
    if problems:
        raise IngestError("the ingestion job completed, but:\n  " + "\n  ".join(problems))
    return stats


def check_corpus(runtime: Any, knowledge_base_id: str, manifest: dict[str, Any]) -> int:
    """One search; every result must be a document of this build, tagged with its corpus, and every document must come back."""
    response = runtime.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": SEARCH},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": MAX_RESULTS}},
    )
    results = response["retrievalResults"]
    names = []
    stale = []
    for result in results:
        uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
        name = uri.rsplit("/", 1)[-1] or "(no location)"
        names.append(name)
        corpus = result.get("metadata", {}).get("corpus")
        if corpus != manifest["corpus"]:
            stale.append(f"{name} (corpus {corpus})")

    expected = set(manifest["documents"])
    problems = []
    if missing := sorted(expected - set(names)):
        problems.append(f"{len(missing)} sections didn't come back: {', '.join(missing[:REASONS_SHOWN])}")
    if extra := sorted(set(names) - expected):
        problems.append(f"{len(extra)} documents aren't in this build: {', '.join(extra[:REASONS_SHOWN])}")
    if repeated := sorted(name for name, count in Counter(names).items() if count > 1):
        problems.append(f"{len(repeated)} documents came back more than once: {', '.join(repeated[:REASONS_SHOWN])}")
    if stale:
        problems.append(f"{len(stale)} results aren't tagged corpus {manifest['corpus']}: {', '.join(stale[:REASONS_SHOWN])}")
    if response.get("nextToken"):
        problems.append(f"more than {MAX_RESULTS} results")
    if problems:
        raise IngestError("the knowledge base doesn't hold exactly this build:\n  " + "\n  ".join(problems))
    return len(results)


def make_clients(region: str) -> tuple[Any, Any]:
    import boto3

    session = boto3.Session(region_name=region)
    return session.client("bedrock-agent"), session.client("bedrock-agent-runtime")


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--data-source-id", required=True)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args(argv)

    failure = None
    try:
        manifest = load_manifest(args.manifest)
        agent, runtime = make_clients(args.region)
        stats = ingest(agent, args.knowledge_base_id, args.data_source_id, manifest)
        print(
            f"Ingested corpus {manifest['corpus']}: {stats.get('numberOfDocumentsScanned', 0)} documents scanned, "
            f"{stats.get('numberOfNewDocumentsIndexed', 0)} new, {stats.get('numberOfModifiedDocumentsIndexed', 0)} modified, "
            f"{stats.get('numberOfMetadataDocumentsModified', 0)} metadata files changed, "
            f"{stats.get('numberOfDocumentsDeleted', 0)} deleted, {stats.get('numberOfDocumentsSkipped', 0)} skipped, 0 failed"
        )
        found = check_corpus(runtime, args.knowledge_base_id, manifest)
        print(f"The knowledge base holds corpus {manifest['corpus']}: {found} sections, and nothing else")
    except IngestError as exc:
        failure = str(exc)
    except ClientError as exc:
        error = exc.response.get("Error", {}).get("Code", "unknown")
        failure = f"{exc.operation_name} answered {error}"
    except BotoCoreError as exc:
        failure = type(exc).__name__
    if failure is not None:
        print(f"Runbooks not ingested: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
