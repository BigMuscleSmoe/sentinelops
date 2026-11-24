# The test sweeps, graded: FAIL on strict recall

Graded 2026-11-18. This is the result of the grader redesign's test sweep. The agent's typed, cited evidence was checked in code, scored against a blind audit, and judged by a pass bar committed before the sweep ran. **Both arms fail on strict recall, under either reading of the form-only flags.** The bar and the frozen files are unchanged. The result stands as recorded here.

## What was measured

- **The agent** investigates one CloudWatch alarm and answers with a `Hypothesis`. Each evidence item in it is one typed statement (kinds: value, range, all, count, first, last, none, gap) that cites a tool result by label (`m1`, `l2`, `d1`).
- **The checker** (`agent/checker/`) recomputes every typed item from the result it cites and flags it when it doesn't hold: contradicted, outside the read, or unresolved. It calls no model.
- **The blind audit** labels every slip in every run, strict or loose, from the transcripts alone, before any checker output is seen. The audits are `agent/evals/audits/<sweep>.json`. The labeler is Claude; its conventions are in each file's `about`.
  - The user labeled 3 Haiku runs blind, for the diagnosis only, and agrees with the audit on 3 of 3.
  - The slip labels are one labeler's.
- **The grader** (`python -m evals grade`) matches flags to labels by item ID:
  - **Precision** is the flags hand-checked right over all flags (`<sweep>.flags.json`).
  - **Strict recall** is the strict labels caught over all strict labels. A catch is a flag on the label's own item, saying its own complaint. Labels no flag can match (root_cause sentences, inferences, unsupported claims) stay in the denominator.
- **The pass bar** (`agent/evals/pass_bar.json`), frozen at `8c7b2f6` before the test sweep: precision 85%, strict recall 70%.
- **The test split:** 11 alarms in 7 windows, none replayed, swept, or tuned on before this sweep. Six windows come from live breaks after the freeze. The seventh, `2026-11-14-2357`, is the held-out `downstream_timeout` window: it was recorded before the freeze and kept unseen. By fault: 3 alarms bad_deploy, 6 pool exhaustion, 2 downstream_timeout.
- **Two arms**, each model at its default effort, and neither sends an effort setting:
  - **Claude Haiku 5.5**, the production model: sweep `20261117T192758Z`, 3 runs an alarm, 33 runs, run at `d0a6ad4`.
  - **Claude Sonnet 5.5**: sweep `20261118T050147Z-sonnet`, 2 runs an alarm, 22 runs, run at `bf3fffd`.

## The result

| | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Runs answered | 33 of 33 | 22 of 22 |
| Diagnoses right: rubric, and audit | 33 and 33 (agree on 33) | 22 and 22 (agree on 22) |
| Confidently wrong | 0 | 0 |
| Typed evidence items | 455 (13.8 a run) | 341 (15.5 a run) |
| Strict slips in the audit | 100 (3.03 a run) | 61 (2.77 a run) |
| **Strict recall** (bar 70%) | **26 of 100, 26%** (95% CI 18-35%) | **1 of 61, 2%** (95% CI 0.3-9%) |
| Recall on checkable labels | 26 of 83, 31% | 1 of 43, 2% |
| Flags | 37 (1.12 a run) | 4 (0.18 a run) |
| **Precision, form-only right** (bar 85%) | **35 of 37, 95%** (95% CI 82-99%) | **3 of 4, 75%** (95% CI 30-95%) |
| **Precision, form-only false** | **29 of 37, 78%** (95% CI 63-89%) | **1 of 4, 25%** (95% CI 5-70%) |
| Verdict, either reading | FAIL (recall) | FAIL (recall, and precision) |

- **The intervals** are Wilson 95% intervals, computed for this report; the grader doesn't print them. They treat labels and flags as independent, but slips cluster by run, so the true intervals are wider. Even so, neither arm's recall interval comes near 70%.
- **Haiku's precision meets the bar only when form-only flags count as right.** Its upper bound with them counted false (89%) is above the bar and its lower bound (63%) below, so that reading doesn't settle it.
- **Sonnet's 4 flags are too few for a reliable precision.** One flag moves it 25 points, and its intervals span 30-95% and 5-70%. Its 75% neither meets nor clearly misses the bar.

### Form-only flags, both readings

A form-only flag marks an item whose stated facts all hold in the cited result but whose typing is wrong: a wrong or nonexistent column, or a value that isn't the field's own (`3 routes`, `2xx`). There are 8 in all.

- **Haiku (6):** `pool-1117-0235-r1` e1, `pool-1116-2101-err-r2` e1, `deploy-1116-2238-r2` e2, `pool-1116-2058-r3` e1, `pool-1116-2101-lat-r3` e1, `pool-1117-0235-r3` e1.
- **Sonnet (2):** `deploy-1116-2238-r1` e8, `pool-1116-2101-err-r2` e9.

**The committed reading counts them right, as wrong as typed** (the user's call). That follows the redesign's earlier hand checks, both before this sweep:

- **Step 4** (`eafed0b`): `agent/tests/fixtures/checker/verdicts.json` pins 7 column flags as wrong as typed.
- **Step 6** (the tuning sweep `20261114T221926Z`, run at `5f0d3f9`): a change typed under `version`, and three records in one column, were called wrong as typed. Those hand calls are in the project's local notes, not this repo.

The audit's convention 4 leaves form-only faults unlabeled, so a form-only flag on an item no label names also counts as an audit miss: 5 of Haiku's 6 audit misses, and Sonnet's 1.

**The other reading counts them false.** The grader prints only the committed reading; precision is the flags called right over all flags. The form-only-false figures come from the grader's own `Report.group`, run with these 8 calls flipped to false. Changing the scorer to print both would have broken the freeze.

### By fault

| Arm, fault | Strict recall | Flags (form-only) | Precision, form-only right / false | Prose-only share |
|---|---|---|---|---|
| Haiku, bad_deploy | 2 of 39 | 6 (1) | 5 / 4 of 6 | 12 of 39, 31% |
| Haiku, pool exhaustion | 19 of 50 | 25 (5) | 24 / 19 of 25 | 3 of 50, 6% |
| Haiku, downstream_timeout | 5 of 11 | 6 (0) | 6 / 6 of 6 | 2 of 11, 18% |
| Sonnet, bad_deploy | 0 of 30 | 1 (1) | 1 / 0 of 1 | 14 of 30, 47% |
| Sonnet, pool exhaustion | 1 of 26 | 3 (1) | 2 / 1 of 3 | 3 of 26, 12% |
| Sonnet, downstream_timeout | 0 of 5 | 0 | - | 1 of 5, 20% |

## Why recall fails: the slips are in text the checker doesn't read

The checker reads an item's typed fields. A label counts as checkable when it sits on an evidence item with a complaint a flag can make.

- **Haiku:** 83 of 100 strict labels are checkable and 26 are caught. Of the 57 missed, the checker finds 50 of the items holding, 4 loose, and 3 contradicted but for another reason. By complaint: 46 outside the read, 10 contradicted, 1 miscited.
- **Sonnet:** 43 of 61 are checkable and 1 is caught. Of the 42 missed, 41 sit on items that hold and 1 on an item contradicted for another reason. By complaint: 39 outside the read, 3 contradicted.

So most of what the checker misses isn't a misread number. It's a claim written in the item's note, beside typed fields that hold. Two shapes account for most of it:

- **A claim about a period the run never read.** Older changes get ruled out with "no effect", "flat until 02:30", or "errors did not start until 20:57", when the run's metrics begin an hour or more after the change. The typed range covers only the rows that were read, so it holds.
- **A universal over a read that left lines out.** "Every failing request had a cart with no promo" is written beside an item over the 20 lines shown, out of 43 found. The item holds over what it cites; the note claims all 43.

**The arms differ in where their slips sit.** Sonnet's typed fields are nearly always right: 4 flags on 341 items, 2 of them form-only. Yet it makes almost as many strict slips per run as Haiku, nearly all in notes and prose.

## R4: no judge this round

R4, decided before the sweep, calls for a model judge only if prose-only labels hold a fifth or more of the strict slips. **It's applied as written, to the production model: Haiku, 17 of 100, 17%. No judge this round** (the user's call).

- **Reported, not triggers:** Sonnet's share is 18 of 61 (30%), and the two arms pooled 35 of 161 (22%).
- **A judge for Sonnet's arm** would in any case need a model other than Sonnet, since the design never lets the judge be the model under test.

**R4's count undercounts free text.** It counts a slip in an evidence item's note as typed, since the note sits on an evidence item, though the checker never reads the note. Counted by where the slip actually is, the free-text share would be far higher: 50 of Haiku's 57 missed checkable labels are on items whose typed fields hold. This is recorded as a reason for the next design, not a change to this one.

## Cost

| | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Sweep total | $0.1991 (33 runs) | $2.1455 (22 runs) |
| Per run: mean, median | $0.0060, $0.0050 | $0.0975, $0.0885 |
| Per run: cheapest, dearest | $0.0037, $0.0154 | $0.0613, $0.1450 |
| Resends, trims, errors | 7, 20, 0 | 0, 0, 0 |

- **Sonnet costs about 16 times as much a run** (mean; 18 times at the median).
- **For that cost, the same diagnoses:** all right in both arms, with 2.77 strict slips a run against Haiku's 3.03.
- **The production cap rules Sonnet out.** Production caps a run at $0.10; the guard reserves each call's worst case, $0.16 at 16,000 output tokens on Sonnet, so the Sonnet arm ran under an eval-only cap of $0.50 a run.
- **These are the guard's figures.** The guard files a refused or failed call at its full reservation, so they can overstate the bill but not understate it.

## What the next iteration would change

This is a design, not yet built. Each change goes through a new freeze, fresh test windows, and a new blind audit. This sweep's test windows are now tuning data.

- **Make the over-claims typed, so code can check them:**
  - **An `unchanged` kind:** a column stays within low to high from a change to a later time, with the start anchored on the change's own deploy record. A claim reaching back before the read then fails as outside the read, instead of hiding in the note.
  - **An `every` kind:** a universal over everything a search found, flagged outside the read whenever the read was cut.
  - **`first` and `last`,** which flag a cut read the same way.
- **Flag claims left in notes.** A check, in evaluation only, flags a note's universals, periods, durations, and figures that its item's kind doesn't carry.
- **Tag each audit label with where its slip sits** (typed fields, note, sentence, inference), so the judge trigger counts free text as free text.
- **Give form-only flags a call of their own,** so the grader prints both precision readings itself.

## Reproducing it

The transcripts aren't in the repo: they hold the model's summarized thinking and are kept local. The audits, flag checks, and pass bar are committed. With the transcripts in `agent/scratch/evals/<sweep>/`:

```bash
# from agent/
uv run python -m evals grade scratch/evals/20261117T192758Z --audit evals/audits/20261117T192758Z.json --flags evals/audits/20261117T192758Z.flags.json
uv run python -m evals grade scratch/evals/20261118T050147Z-sonnet --audit evals/audits/20261118T050147Z-sonnet.json --flags evals/audits/20261118T050147Z-sonnet.flags.json
```

The grader's output, verbatim, is beside this file: `20261117T192758Z.grade.txt` and `20261118T050147Z-sonnet.grade.txt`. It's the record that lasts. The grader refuses to regrade a test sweep once its scenarios or windows change, and the next iteration changes them.

## Erratum (2026-11-24)

From Phase 3 through 2026-11-23, the answer schema's evidence examples included `1.43.0`, `TypeError`, `POOL_MAX_SIZE` and `20->2`, and both test sweeps sent them (`d0a6ad4`, `bf3fffd`). They are the bad deploy's release and error, and the pool fault's config key and its change. So the 3 bad-deploy and 6 pool exhaustion alarms in the 55-of-55 result, 45 of the 55 runs, weren't fully held out.

The 2 downstream_timeout alarms had no leak like that. The prompt and schema did name payments-api: as the dependency whose latency and timeouts the metrics table shows, in the fault location's description, in an evidence example that rules it out, and in a root-cause example about an expired TLS certificate on its endpoint. None of them named the downstream fault itself: not its timeout setting, the 504 it returns, or its error type.

The numbers in this report are unchanged. The next evaluation cycle uses the clean schema: since 2026-11-24 its examples are values this service doesn't have, and a test fails if anything the model is sent names a release, a commit, or a fault's file, function, error, or config key.
