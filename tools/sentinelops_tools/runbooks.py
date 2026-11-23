"""Runbook search: which runbooks a knowledge-base search puts first.

The knowledge base holds one document per runbook section, each tagged with
its runbook and section (tools/build_runbooks.py). One search returns every
section, best first. A runbook ranks by its first Symptoms or Diagnosis
section in that order: those describe what its fault looks like and how to check
for it. What this is not names the neighbors' symptoms on purpose, and
Resolution and Prevention describe fixes, so none of them ranks a runbook.

The search tool shows the top runbooks in full; the retrieval set
(tests/fixtures/retrieval/) measures this ranking on recorded searches.
"""

from dataclasses import dataclass
from typing import Any

RANKED_SECTIONS = ("Symptoms", "Diagnosis")


@dataclass(frozen=True)
class Hit:
    document: str  # "bad-deploy--symptoms.md"
    runbook: str  # "bad-deploy.md"
    section: str  # "Symptoms"
    score: float


def hits_from_retrieve(response: dict[str, Any]) -> list[Hit]:
    """A Retrieve response's results in its order, best first, as the sections they are."""
    hits = []
    for result in response.get("retrievalResults", []):
        metadata = result.get("metadata", {})
        uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
        hits.append(
            Hit(
                document=uri.rsplit("/", 1)[-1],
                runbook=str(metadata.get("runbook", "")),
                section=str(metadata.get("section", "")),
                score=float(result.get("score", 0.0)),
            )
        )
    return hits


def rank(hits: list[Hit]) -> list[str]:
    """Runbooks, best first, each where its first Symptoms or Diagnosis section comes in the search's order.

    By position, not score: the search returns its results best first, and the
    order is what it means by best.
    """
    ranked: list[str] = []
    for hit in hits:
        if hit.section in RANKED_SECTIONS and hit.runbook not in ranked:
            ranked.append(hit.runbook)
    return ranked
