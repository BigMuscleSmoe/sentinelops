"""A sweep: every scenario, N times, one run at a time, through investigation.py.

The run is main.py's replay with the window in AWS's place: the window answers
the alarm, metrics, logs, and deploy history. Only the sign-in (STS, as
main.py's runs do) and the runbook search reach AWS: the search reads the live
knowledge base, which the sweep's command first checks holds this checkout's
runbooks (knowledge_base.connect) and records by its corpus digest. The code
read shows the snapshot the command builds from each release's commit
(code_snapshot.build), recorded by its digest. The model is the real one, so a
sweep is billed.

Each run's transcript is kept whole in scratch/evals/<sweep>/, and sweep.json
records every outcome as it lands. Errors are kept apart from outcomes and
never scored: an API error or timeout, a refused sign-in, a read the recording
can't answer, or an answer from a model other than the one asked for. A halted
run, a refusal by the model, and a run with no Hypothesis are outcomes.

Spend is held three ways: the guard's cap on a run, the sweep's cap (a run
starts only if the cap still has room for a whole run's worth), and the $10
monthly limit in the Anthropic Console. Both caps are the arm's (evals/arms.py):
$0.10 and $1.00 for Haiku, the production cap. sweep.json records the arm and
the commit the sweep ran on, so a test sweep can be held to the freeze. A sweep
is one split's (tuning or test) and one commit's: one already begun is resumed
only as the same arm's and split's, on the commit it started on, against the
same runbooks and code. The sweep never grades.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evals import repo
from evals.arms import ARMS, DEFAULT_ARM, Arm
from evals.scenarios import Scenario, Split, load_window, window_module
from code_snapshot import CodeSnapshot
from evals.witness import ModelWitness, same_model
from guards import Limits
from investigation import Refused, investigate_alarm
from knowledge_base import KnowledgeBase
from pricing import ModelPrice
from providers import Settings

SWEEPS = Path("scratch") / "evals"
MAX_TEXT = 300
_ACCOUNT_LIKE = re.compile(r"\d{12}")


@dataclass
class RunRecord:
    run: str  # <scenario>-r<n>
    scenario: str
    attempt: int
    status: str  # answered | halted | model_refused | no_hypothesis | error
    detail: str | None
    cost_usd: float
    input_tokens: int
    tool_calls: int
    model_calls: int
    seconds: float
    model_asked: str
    models_answered: list[str]
    transcript: str | None
    started_at: str

    @property
    def scored(self) -> bool:
        return self.status != "error"


class WindowSession:
    """Stands in for boto3.Session in a replay: the window for reads, a real session for the sign-in and the runbook search."""

    def __init__(self, window: Any, sign_in: Any) -> None:
        self.window = window
        self.sign_in = sign_in
        self.gaps: list[str] = []
        self._outside = window_module().OutsideWindow

    def client(self, service: str, **kwargs: Any) -> Any:
        if service in ("logs", "cloudwatch", "dynamodb"):
            return _Noting(self.window.client(service), self.gaps, self._outside)
        if service in ("sts", "bedrock-agent-runtime"):
            return self.sign_in.client(service, **kwargs)
        raise ValueError(f"a replay has no {service} client")

    def get_credentials(self) -> Any:
        raise RuntimeError("an eval runs its tools here, not through Gateway")


class _Noting:
    """A window's fake client that notes every read the recording can't answer, then lets it fail."""

    def __init__(self, fake: Any, gaps: list[str], outside: type[Exception]) -> None:
        self._fake = fake
        self._gaps = gaps
        self._outside = outside

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fake, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attr(*args, **kwargs)
            except self._outside as e:
                self._gaps.append(f"{name}: {e}")
                raise

        return call


def plan(scenarios: Sequence[Scenario], runs: int) -> list[tuple[Scenario, int]]:
    """Round by round, so a sweep stopped early still covers every scenario evenly."""
    return [(s, n) for n in range(1, runs + 1) for s in scenarios]


def run_id(scenario: Scenario, n: int) -> str:
    return f"{scenario.id}-r{n}"


def classify(failure: str) -> str:
    """A run that ended without a Hypothesis: the model's doing (an outcome) or not (an error)."""
    if failure.startswith("The model refused"):
        return "model_refused"
    if "without a Hypothesis" in failure or "StructuredOutput" in failure:
        return "no_hypothesis"
    return "error"


def _short(text: str) -> str:
    text = _ACCOUNT_LIKE.sub("<12 digits>", text)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " ...[cut]"


async def run_one(
    scenario: Scenario,
    n: int,
    attempt: int,
    *,
    settings: Settings,
    price: ModelPrice,
    sign_in: Any,
    out_dir: Path,
    api_transport: Callable[[], httpx.AsyncBaseTransport],
    token_http_client: Any = None,
    limits: Limits = Limits(),
    knowledge_base_id: str | None = None,
    code: CodeSnapshot | None = None,
) -> RunRecord:
    import anthropic

    rid = run_id(scenario, n)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    session = WindowSession(load_window(scenario.window), sign_in)
    witness = ModelWitness(api_transport())
    client = anthropic.DefaultAsyncHttpxClient(transport=witness)
    clock = time.monotonic()
    record = RunRecord(rid, scenario.id, attempt, "error", None, 0.0, 0, 0, 0, 0.0, settings.model_id, [], None, started)
    try:
        outcome = await investigate_alarm(
            scenario.alarm,
            scenario.fired_at,
            gateway_url=None,
            settings=settings,
            price=price,
            session=session,
            http_client=client,
            token_http_client=token_http_client,
            limits=limits,
            knowledge_base_id=knowledge_base_id,
            code=code.files if code else None,
        )
    except Exception as e:  # an error of the harness or the recording, never the model's
        record.detail = _short(f"{type(e).__name__}: {e}")
        record.seconds = round(time.monotonic() - clock, 1)
        return record
    finally:
        await client.aclose()

    record.models_answered = list(witness.models)
    if isinstance(outcome, Refused):
        record.detail = _short(f"refused: {outcome.reason}")
        record.seconds = round(time.monotonic() - clock, 1)
        return record

    report = outcome.report
    record.cost_usd = round(report.cost_usd, 6)
    record.input_tokens = report.input_tokens
    record.tool_calls = len(report.tool_calls)
    record.model_calls = len(report.model_calls)
    record.seconds = round(report.seconds, 1)

    wrong = [m for m in witness.models if not same_model(settings.model_id, m)]
    if session.gaps:
        record.detail = _short("read outside the recording: " + "; ".join(session.gaps))
    elif wrong:
        record.detail = _short(f"answered by {', '.join(sorted(set(wrong)))}, asked for {settings.model_id}")
    elif report.model_calls and len(witness.models) < sum(r.startswith("200 ") for r in witness.responses):
        record.detail = _short(f"not every response said which model answered ({', '.join(witness.responses)})")
    elif report.hypothesis is not None:
        stop = report.hypothesis.model_dump(mode="json").get("halted_by")
        record.status = "halted" if stop else "answered"
        record.detail = stop
    else:
        failure = report.failure or "no Hypothesis and no failure"
        record.status = classify(failure)
        record.detail = _short(failure)

    transcript = outcome.record()
    transcript["eval"] = {
        "run": rid,
        "scenario": scenario.id,
        "window": scenario.window,
        "fault": scenario.fault.value,
        "seen": scenario.seen,
        "split": scenario.split.value,
        "attempt": attempt,
        "status": record.status,
        "detail": record.detail,
        "models_answered": record.models_answered,
    }
    name = f"{rid}.json" if attempt == 1 else f"{rid}.attempt{attempt}.json"
    write_json(out_dir / name, transcript)
    record.transcript = name
    return record


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8", newline="\n")
    tmp.replace(path)


def load_records(out_dir: Path) -> list[RunRecord]:
    path = out_dir / "sweep.json"
    if not path.exists():
        return []
    return [RunRecord(**r) for r in json.loads(path.read_text(encoding="utf-8"))["records"]]


def _sha(commit: str | None) -> str:
    return commit[:7] if commit else "no recorded commit"


def check_resumed(
    name: str,
    was: dict[str, Any],
    arm: Arm,
    split: Split,
    commit: str | None,
    dirty: list[str],
    corpus: str | None = None,
    code: str | None = None,
) -> None:
    """A sweep already begun is resumed only as what it is; raises ValueError otherwise.

    The same arm's and split's, on the commit it started on, searching the same
    runbooks and reading the same code: sweep.json holds one commit, one corpus,
    and one code snapshot for all its runs,
    and a test sweep is held to the freeze by it. A sweep from before the split
    was recorded is a tuning one.
    """
    was_arm, was_split = was.get("arm", DEFAULT_ARM), was.get("split", Split.TUNING.value)
    if was_arm != arm.name:
        raise ValueError(f"{name} is the {was_arm} arm's sweep, not {arm.name}'s")
    if was_split != split.value:
        raise ValueError(f"{name} is a {was_split} sweep, not a {split.value} one")
    if was.get("commit") != commit:
        raise ValueError(
            f"{name} ran on {_sha(was.get('commit'))}, and HEAD is {_sha(commit)}: a sweep is one commit's, so start a new one"
        )
    if dirty and not was.get("dirty"):
        raise ValueError(f"{name} ran on a clean tree, and it has uncommitted changes now: {', '.join(dirty[:5])}")
    if was.get("runbooks_corpus") != corpus:
        raise ValueError(f"{name} searched runbooks corpus {was.get('runbooks_corpus')}, and this run's is {corpus}")
    if was.get("code_snapshot") != code:
        raise ValueError(f"{name} read code snapshot {was.get('code_snapshot')}, and this run's is {code}")


async def run_sweep(
    scenarios: Sequence[Scenario],
    runs: int,
    *,
    settings: Settings,
    price: ModelPrice,
    sign_in: Any,
    out_dir: Path,
    api_transport: Callable[[], httpx.AsyncBaseTransport],
    token_http_client: Any = None,
    arm: Arm = ARMS[DEFAULT_ARM],
    knowledge_base: KnowledgeBase | None = None,
    code: CodeSnapshot | None = None,
    cap_usd: float | None = None,
    per_run_usd: float | None = None,
    pause_s: float = 10.0,
    say: Callable[[str], None] = print,
) -> list[RunRecord]:
    """Each planned run once, skipping runs that already have an outcome, so a rerun
    of the same sweep retries only its errors. Stops before a run the cap can't cover.

    The arm sets both caps; cap_usd and per_run_usd override them, for tests. The
    knowledge base is the runbook search's, already checked (knowledge_base.connect);
    without one, a run's search says there's none. The code is the read tool's
    (code_snapshot.build); without it, a read says there's no code. The scenarios must be one
    split's, and a sweep already begun is resumed only as what it is
    (check_resumed): ValueError otherwise, before anything runs."""
    cap_usd = arm.cap_usd if cap_usd is None else cap_usd
    per_run_usd = arm.per_run_usd if per_run_usd is None else per_run_usd
    limits = Limits(max_cost_usd=per_run_usd)
    splits = sorted({s.split for s in scenarios})
    if len(splits) != 1:
        raise ValueError("a sweep is one split's: these scenarios are " + (" and ".join(splits) if splits else "none at all"))
    split = splits[0]
    commit, dirty = repo.head(), repo.dirty()
    corpus = knowledge_base.corpus if knowledge_base else None
    snapshot = code.digest if code else None
    earlier = out_dir / "sweep.json"
    if earlier.exists():
        was = json.loads(earlier.read_text(encoding="utf-8"))
        check_resumed(out_dir.name, was, arm, split, commit, dirty, corpus, snapshot)
    records = load_records(out_dir)
    done = {r.run for r in records if r.scored}
    attempts = {r.run: r.attempt for r in records}
    todo = [(s, n) for s, n in plan(scenarios, runs) if run_id(s, n) not in done]
    for i, (scenario, n) in enumerate(todo):
        spent = sum(r.cost_usd for r in records)
        if spent + per_run_usd > cap_usd:
            say(f"stopped: ${spent:.4f} spent, and a run may cost ${per_run_usd:.2f}, past the ${cap_usd:.2f} cap")
            break
        if i:
            await asyncio.sleep(pause_s)
        rid = run_id(scenario, n)
        record = await run_one(
            scenario,
            n,
            attempts.get(rid, 0) + 1,
            settings=settings,
            price=price,
            sign_in=sign_in,
            out_dir=out_dir,
            api_transport=api_transport,
            token_http_client=token_http_client,
            limits=limits,
            knowledge_base_id=knowledge_base.id if knowledge_base else None,
            code=code,
        )
        records.append(record)
        attempts[rid] = record.attempt
        write_json(
            out_dir / "sweep.json",
            {
                "sweep": out_dir.name,
                "arm": arm.name,
                "split": split.value,
                "model": settings.model_id,
                "per_run_usd": per_run_usd,
                "cap_usd": cap_usd,
                "commit": commit,
                "dirty": dirty,
                "runbooks_corpus": corpus,
                "code_snapshot": snapshot,
                "runs_per_scenario": runs,
                "scenarios": [s.id for s in scenarios],
                "records": [asdict(r) for r in records],
            },
        )
        total = sum(r.cost_usd for r in records)
        say(
            f"{rid:<22} {record.status:<14} ${record.cost_usd:.4f} {record.tool_calls:>2} tools "
            f"{record.seconds:>5.1f} s | total ${total:.4f}" + (f" | {record.detail}" if record.status == "error" else "")
        )
    return records
