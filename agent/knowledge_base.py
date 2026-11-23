"""The runbook knowledge base, for runs whose tools run here: a sweep's, or main.py's without Gateway.

Through Gateway, the search's Lambda has the knowledge base's ID in its
environment. A run here finds it by its name, then checks that it holds this
checkout's runbooks: one search must return every section this checkout's
builder makes, each tagged with its corpus digest, and nothing else. That is
the deploy's own check after each ingest (tools/ingest_runbooks.py). So a
knowledge base that's missing, not yet holding a runbook edit made here, or
holding one this checkout lacks, is refused before any model call. The check
costs the listing (free) and one Retrieve, and calls no model.

Only AWS's error code is reported, never its message, which can name an ARN.
"""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

NAME = "sentinelops-runbooks"  # infra/runbooks_kb.tf
TOOLS = Path(__file__).resolve().parents[1] / "tools"


class KnowledgeBaseError(Exception):
    """The knowledge base can't serve this run. The message says why."""


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    corpus: str  # the digest of the runbooks it holds: this checkout's


def connect(session: Any) -> KnowledgeBase:
    """The knowledge base by its name, once it's checked to hold this checkout's runbooks; KnowledgeBaseError otherwise."""
    from botocore.exceptions import BotoCoreError, ClientError

    build, ingest = script("build_runbooks"), script("ingest_runbooks")
    try:
        manifest = this_build()
    except build.FormatError as e:
        raise KnowledgeBaseError(f"this checkout's runbooks don't build: {e}") from None
    try:
        knowledge_base_id = find(session.client("bedrock-agent"))
        ingest.check_corpus(session.client("bedrock-agent-runtime"), knowledge_base_id, manifest)
    except ClientError as e:
        raise KnowledgeBaseError(f"couldn't read the knowledge base {NAME}: AWS returned {e.response['Error']['Code']}") from None
    except BotoCoreError as e:
        raise KnowledgeBaseError(f"couldn't read the knowledge base {NAME}: {type(e).__name__}") from None
    except ingest.IngestError as e:
        raise KnowledgeBaseError(
            f"{NAME} doesn't hold this checkout's runbooks (corpus {manifest['corpus']}): {e}\n"
            "Push the runbook change so the deploy ingests it, or run from the commit the knowledge base was built from."
        ) from None
    return KnowledgeBase(knowledge_base_id, manifest["corpus"])


def find(agent: Any) -> str:
    """The ID of the one ACTIVE knowledge base named NAME."""
    matches = [
        summary
        for page in agent.get_paginator("list_knowledge_bases").paginate()
        for summary in page["knowledgeBaseSummaries"]
        if summary["name"] == NAME
    ]
    if not matches:
        raise KnowledgeBaseError(f"no knowledge base is named {NAME}")
    if len(matches) > 1:
        raise KnowledgeBaseError(f"{len(matches)} knowledge bases are named {NAME}")
    (summary,) = matches
    if summary["status"] != "ACTIVE":
        raise KnowledgeBaseError(f"the knowledge base {NAME} is {summary['status']}, not ACTIVE")
    return summary["knowledgeBaseId"]


def this_build() -> dict[str, Any]:
    """The manifest tools/build_runbooks.py would write for this checkout's runbooks, made in memory."""
    corpus = script("build_runbooks").load()
    return {
        "corpus": corpus.digest,
        "sections": len(corpus.sections),
        "documents": sorted(section.file for section in corpus.sections),
    }


def script(name: str) -> ModuleType:
    """One of tools/'s scripts, loaded by path once: they sit beside the tools package, not in it."""
    module_name = f"sentinelops_tools_script_{name}"
    if module_name not in sys.modules:
        path = TOOLS / f"{name}.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"can't load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]
