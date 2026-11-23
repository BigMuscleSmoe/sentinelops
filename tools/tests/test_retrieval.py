"""The retrieval set's queries, and the ranking the runbook search uses.

The queries hold no figure from any window and nothing only the staging
knows, and cover every runbook: the three injected faults, the four
neighbors, and symptoms none fits.
"""

import re
from collections import Counter

import build_runbooks as rb
import pytest
from retrieval import BAR, keyword_rank, load_queries, load_recorded, recorded_hits, score
from test_runbooks import STAGING

from sentinelops_tools.runbooks import Hit, hits_from_retrieve, rank

INJECTED = {"db-pool-exhaustion.md", "bad-deploy.md", "downstream-timeout.md"}
NEIGHBORS = {"slow-database.md", "traffic-surge.md", "lambda-throttling.md", "memory-leak.md"}
# Figures that are names or protocol, not measurements: percentiles in column
# names, and HTTP statuses.
NOT_FIGURES = {"p50", "p99", "db_query_p50_ms", "pool_wait_p99_ms", "payments_p99_ms", "200", "400", "500", "500s", "504"}


# The queries


def test_every_runbook_has_queries_and_some_fit_none() -> None:
    queries = load_queries()
    expected = Counter(q["expect"] for q in queries)
    assert set(expected) - {None} == INJECTED | NEIGHBORS
    assert all(expected[name] >= 5 for name in INJECTED)
    assert all(expected[name] >= 2 for name in NEIGHBORS)
    assert expected[None] >= 4
    assert len({q["id"] for q in queries}) == len(queries)
    for q in queries:
        assert q["group"] == ("injected" if q["expect"] in INJECTED else "neighbor" if q["expect"] else "none")


def test_round_2_checks_the_four_runbooks_round_1_missed_on() -> None:
    """Written after round 1 was measured and before its fixes, never changed since: 3 each."""
    fresh = [q for q in load_queries() if q.get("round", 1) == 2]
    assert Counter(q["expect"] for q in fresh) == {
        "db-pool-exhaustion.md": 3,
        "bad-deploy.md": 3,
        "downstream-timeout.md": 3,
        "memory-leak.md": 3,
    }
    assert all(q["id"].startswith("fresh-") for q in fresh)
    assert all(q.get("round", 1) == 1 for q in load_queries()[:32])


def test_no_query_holds_a_figure_or_what_only_the_staging_knows() -> None:
    for q in load_queries():
        text = q["query"]
        assert len(text) <= 500
        assert text.isascii()
        figures = [w for w in re.findall(r"[A-Za-z0-9_%]+", text) if re.search(r"\d", w) and w.lower() not in NOT_FIGURES]
        assert figures == [], (q["id"], figures)
        staged = [p for p in STAGING if re.search(p, text, flags=re.IGNORECASE)]
        assert staged == [], (q["id"], staged)


# The recording


def test_the_recording_is_of_these_queries_and_whole() -> None:
    """A query edit fails here until the set is recorded again (python -m tests.retrieval record).

    A runbook edit doesn't: it reaches the knowledge base only through a deploy,
    which this test gates, so the report flags a recording of another corpus
    as stale instead.
    """
    recording = load_recorded()
    queries = load_queries()
    assert list(recording["searches"]) == [q["id"] for q in queries]
    for q in queries:
        search = recording["searches"][q["id"]]
        assert search["query"] == q["query"]
        documents = [h["document"] for h in search["hits"]]
        assert len(documents) == len(set(documents)) == recording["number_of_results"]


def test_the_recorded_ranking_meets_the_bar() -> None:
    """Top-2 over every fitting query, both rounds together: 90% before any agent run (decision 14)."""
    recording = load_recorded()
    queries = load_queries()
    ranked = {q["id"]: rank(recorded_hits(recording, q["id"])) for q in queries}
    assert score(queries, ranked)["top2"] >= BAR


# The ranking


def hit(runbook: str, section: str, score: float = 0.5) -> Hit:
    slug = section.lower().replace(" ", "-")
    return Hit(document=f"{runbook[:-3]}--{slug}.md", runbook=runbook, section=section, score=score)


def test_a_runbook_ranks_by_its_first_symptoms_or_diagnosis_section() -> None:
    hits = [
        hit("a.md", "What this is not", 0.9),
        hit("b.md", "Resolution", 0.8),
        hit("b.md", "Diagnosis", 0.7),
        hit("a.md", "Prevention", 0.6),
        hit("c.md", "Symptoms", 0.5),
        hit("a.md", "Symptoms", 0.4),
        hit("c.md", "Diagnosis", 0.3),
    ]
    assert rank(hits) == ["b.md", "c.md", "a.md"]


def test_a_runbook_with_no_ranked_section_in_the_results_is_not_ranked() -> None:
    assert rank([hit("a.md", "Resolution"), hit("b.md", "Symptoms")]) == ["b.md"]


def test_a_retrieve_response_becomes_hits_in_its_order() -> None:
    response = {
        "retrievalResults": [
            {
                "content": {"text": f"# Runbook: {runbook}\n\n## {section}\n\nx", "type": "TEXT"},
                "location": {"type": "S3", "s3Location": {"uri": f"s3://bucket/{name}"}},
                "metadata": {"runbook": runbook, "section": section, "title": runbook.upper(), "corpus": "abc"},
                "score": score,
            }
            for name, runbook, section, score in [
                ("b--diagnosis.md", "b.md", "Diagnosis", 0.61),
                ("a--symptoms.md", "a.md", "Symptoms", 0.55),
            ]
        ]
    }
    assert hits_from_retrieve(response) == [
        Hit("b--diagnosis.md", "b.md", "Diagnosis", 0.61, "B.MD", "# Runbook: b.md\n\n## Diagnosis\n\nx"),
        Hit("a--symptoms.md", "a.md", "Symptoms", 0.55, "A.MD", "# Runbook: a.md\n\n## Symptoms\n\nx"),
    ]


def test_the_score_counts_top_1_top_2_and_traffic_surge_above_the_right_runbook() -> None:
    queries = [
        {"id": "q1", "expect": "a.md"},
        {"id": "q2", "expect": "a.md"},
        {"id": "q3", "expect": "b.md"},
        {"id": "q4", "expect": None},
    ]
    ranked = {
        "q1": ["a.md", "traffic-surge.md", "b.md"],
        "q2": ["traffic-surge.md", "a.md", "b.md"],
        "q3": ["a.md", "traffic-surge.md", "b.md"],
        "q4": ["traffic-surge.md", "a.md", "b.md"],
    }
    s = score(queries, ranked)
    assert (s["fitting"], s["top1"], s["top2"]) == (3, 1 / 3, 2 / 3)
    assert s["misses"] == ["q3"]
    assert s["surge_above"] == ["q2", "q3"]


@pytest.mark.parametrize("runbook", sorted(INJECTED | NEIGHBORS))
def test_the_keyword_yardstick_finds_a_runbook_from_its_own_symptoms(runbook) -> None:
    corpus = rb.load()
    text = next(r for r in corpus.runbooks if r.file == runbook).sections["Symptoms"]
    assert keyword_rank(text, corpus)[0] == runbook
