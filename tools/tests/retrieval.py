"""The retrieval set: record the knowledge base's searches, and score the ranking on them.

Run from tools/ (profile sentinelops for record):
  uv run python -m tests.retrieval record   # one Retrieve per query (billed: a few embedded tokens each)
  uv run python -m tests.retrieval report   # the scores, from the recording; no AWS

The queries (fixtures/retrieval/queries.json) are fixed before any search.
The recording keeps each search's sections in order with their scores, and
nothing account-specific: the document's name, its runbook and section, the
score. It's tied to the corpus digest, so a runbook edit fails
test_retrieval.py until the set is recorded again, on purpose.

Beside the knowledge base's ranking (sentinelops_tools.runbooks.rank), a
keyword yardstick: each runbook's Symptoms and Diagnosis text against the
query's words, by how many distinct ones they share. It only says what plain
word overlap would get; nothing ships it.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import build_runbooks as rb
from sentinelops_tools.runbooks import RANKED_SECTIONS, Hit, hits_from_retrieve, rank

HERE = Path(__file__).resolve().parent / "fixtures" / "retrieval"
QUERIES = HERE / "queries.json"
RECORDED = HERE / "recorded.json"
INFRA = Path(__file__).resolve().parents[2] / "infra"
BAR = 0.90  # top-2 on fitting queries, before any agent run (decision 14)
SURGE = "traffic-surge.md"

# Words too common to say anything about a runbook.
STOPWORDS = set(
    """a an the and or but not no nor of to in on at by for from with without as is are was were be been
    it its this that these those so than then there their they them which who what when while where all any
    each every only just still yet also very more most less much such same other own one first once again
    has have had do does did can could would should will may might must""".split()
)


def load_queries(path: Path = QUERIES) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["queries"]


def load_recorded(path: Path = RECORDED) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def recorded_hits(recording: dict[str, Any], query_id: str) -> list[Hit]:
    return [Hit(**hit) for hit in recording["searches"][query_id]["hits"]]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_%]+", text.lower()) if w not in STOPWORDS and len(w) > 1}


def keyword_rank(query: str, corpus: rb.Corpus) -> list[str]:
    """Runbooks by how many of the query's distinct words their Symptoms and Diagnosis hold; ties by name."""
    asked = words(query)
    overlap = {}
    for runbook in corpus.runbooks:
        held = set().union(*(words(runbook.sections[name]) for name in RANKED_SECTIONS))
        overlap[runbook.file] = len(asked & held)
    return sorted(overlap, key=lambda name: (-overlap[name], name))


def score(queries: list[dict[str, Any]], ranked: dict[str, list[str]]) -> dict[str, Any]:
    """Top-1 and top-2 over the queries a runbook fits, and where traffic-surge.md outranks the right one."""
    fitting = [q for q in queries if q["expect"]]
    top1 = [q["id"] for q in fitting if ranked[q["id"]][0] == q["expect"]]
    top2 = [q["id"] for q in fitting if q["expect"] in ranked[q["id"]][:2]]
    surge_above = [
        q["id"]
        for q in fitting
        if q["expect"] != SURGE and ranked[q["id"]].index(SURGE) < ranked[q["id"]].index(q["expect"])
    ]
    return {
        "fitting": len(fitting),
        "top1": len(top1) / len(fitting),
        "top2": len(top2) / len(fitting),
        "misses": [q["id"] for q in fitting if q["id"] not in top2],
        "surge_above": surge_above,
    }


def record(knowledge_base_id: str) -> Path:
    """One Retrieve per query, every section back (one per document in the manifest); refused on another corpus."""
    import boto3

    import ingest_runbooks as ir

    manifest = ir.load_manifest()
    runtime = boto3.Session(region_name="us-east-1").client("bedrock-agent-runtime")
    searches = {}
    for query in load_queries():
        response = runtime.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": query["query"]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": manifest["sections"]}},
        )
        corpora = {result.get("metadata", {}).get("corpus") for result in response["retrievalResults"]}
        if corpora != {manifest["corpus"]}:
            raise SystemExit(f"{query['id']}: the knowledge base answered corpus {sorted(map(str, corpora))}, not {manifest['corpus']}")
        hits = hits_from_retrieve(response)
        scores = [hit.score for hit in hits]
        if scores != sorted(scores, reverse=True):
            raise SystemExit(f"{query['id']}: the results' scores don't fall in the order returned; check what a score means")
        if sorted(hit.document for hit in hits) != manifest["documents"]:
            raise SystemExit(f"{query['id']}: the search returned {len(hits)} sections, not this build's {manifest['sections']}")
        searches[query["id"]] = {
            "query": query["query"],
            "hits": [
                {"document": h.document, "runbook": h.runbook, "section": h.section, "score": round(h.score, 6)} for h in hits
            ],
        }
    recording = {
        "corpus": manifest["corpus"],
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "number_of_results": manifest["sections"],
        "searches": searches,
    }
    RECORDED.write_bytes((json.dumps(recording, indent=1) + "\n").encode("ascii"))
    return RECORDED


def report() -> str:
    queries = load_queries()
    recording = load_recorded()
    corpus = rb.load()
    kb = {q["id"]: rank(recorded_hits(recording, q["id"])) for q in queries}
    kw = {q["id"]: keyword_rank(q["query"], corpus) for q in queries}
    lines = [f"Retrieval set: {len(queries)} queries, corpus {recording['corpus']}, recorded {recording['recorded_at']}", ""]
    lines.append(f"{'query':<19} {'expect':<23} {'kb rank':>7}  {'kb top 2':<46} {'kw rank':>7}")
    for q in queries:
        expect = q["expect"] or "(none fits)"
        kb_at = kb[q["id"]].index(q["expect"]) + 1 if q["expect"] else "-"
        kw_at = kw[q["id"]].index(q["expect"]) + 1 if q["expect"] else "-"
        lines.append(f"{q['id']:<19} {expect:<23} {kb_at!s:>7}  {', '.join(kb[q['id']][:2]):<46} {kw_at!s:>7}")
    lines.append("")
    subsets = {
        "all": lambda q: True,
        "round 1": lambda q: q.get("round", 1) == 1,
        "round 2": lambda q: q.get("round", 1) == 2,
        "injected": lambda q: q["group"] == "injected",
        "neighbor": lambda q: q["group"] == "neighbor",
    }
    for label, ranked in (("knowledge base", kb), ("keyword yardstick", kw)):
        for subset, keep in subsets.items():
            chosen = [q for q in queries if q["expect"] and keep(q)]
            if not chosen:
                continue
            s = score(chosen, ranked)
            lines.append(
                f"{label:<18} {subset:<9} top-1 {s['top1']:6.1%}  top-2 {s['top2']:6.1%}  of {s['fitting']}"
                + (f"  misses: {', '.join(s['misses'])}" if s["misses"] else "")
            )
        s = score(queries, ranked)
        lines.append(f"{label:<18} {SURGE} above the right runbook: {', '.join(s['surge_above']) or 'nowhere'}")
    # The first ranked section's score: the results come best first.
    best = {q["id"]: next(h.score for h in recorded_hits(recording, q["id"]) if h.section in RANKED_SECTIONS) for q in queries}
    fit_best = [best[q["id"]] for q in queries if q["expect"]]
    lines.append("")
    lines.append(f"best Symptoms/Diagnosis score: fitting {min(fit_best):.3f} to {max(fit_best):.3f}")
    for q in queries:
        if not q["expect"]:
            lines.append(f"  {q['id']}: {best[q['id']]:.3f}, top 2 {', '.join(kb[q['id']][:2])}")
    return "\n".join(lines)


def terraform_output(name: str) -> str:
    return subprocess.run(
        ["terraform", "output", "-raw", name], cwd=INFRA, capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("action", choices=["record", "report"])
    args = parser.parse_args(argv)
    if args.action == "record":
        path = record(terraform_output("runbooks_knowledge_base_id"))
        print(f"recorded {len(load_queries())} searches to {path.name}")
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
