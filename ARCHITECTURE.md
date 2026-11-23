# Architecture

**What's built and what isn't.** Built and running: the demo service, the trigger, the agent on AgentCore Runtime with its four tools behind Gateway, runbook search on a Bedrock knowledge base, and the eval harness. Designed here but not built yet: code execution, the remediation flow, the live trace, and the frontend. Each of those sections says so in its heading.

## Design goals

1. **The agent's reasoning must be visible.** A black box that says "fixed it" is worthless as a demo and worse in production. Every tool call, every intermediate thought, streams to the UI (Phase 5). Until then, each run's whole record is stored with its incident.
2. **The agent cannot change production.** It reads, it reasons, it proposes. A human merges.
3. **Idle cost near zero.** Everything serverless or scaled to zero, except the demo service itself.
4. **Bounded execution.** Hard caps on tool calls, tokens, and wall-clock time per incident.
5. **The public demo can't run up a bill.** Anyone can click the button, so the limits that matter are global, not per-user.

## Components

### Demo service (`services/checkout-api/`)

A deliberately small Lambda-backed API standing in for a real service. It has a fault injector behind a DynamoDB feature flag, with three failure modes built and a fourth planned:

| Mode | Symptom | Where the cause is | Status |
|---|---|---|---|
| `db_pool_exhaustion` | 500s under load, latency climbs first | Connection pool size dropped in a recent config change | Built |
| `bad_deploy` | 500s immediately after a deploy event | A bug shipped in the latest release | Built |
| `downstream_timeout` | Intermittent 504s, no error rate change on this service | Upstream dependency slow, not this service's fault | Built |
| `memory_leak` | Gradual latency climb over ~20 min, then OOM | Memory growth in this service | Planned |

`downstream_timeout` matters most. It's the one where the correct answer is "this isn't my service's problem," and an agent that confidently blames the wrong component fails the test.

`memory_leak` takes ~20 minutes to develop, which is too slow for a live click. It's planned as a synthesized recording for the eval only; the demo button (Phase 5) will offer the other three.

There's no real database, so the fault injector has to produce the metric shapes the runbooks describe: for pool exhaustion, `pool_wait_time_ms` and latency rise before errors do. If the injected shape doesn't match the runbook, the agent is being tested against a lie.

The pool can't live in process memory: Lambda runs one request per execution environment, so 50 concurrent requests would get 50 private pools and never contend. It lives in one DynamoDB item that every instance shares (`sentinelops-checkout-pool`), holding the time each connection slot next comes free, plus the end times of requests in flight for `concurrent_requests`. A request reads the item, then claims a slot with one conditional atomic update: an idle slot if there is one, otherwise a place in the queue of the slot that frees first, if that wait fits within the 1.5 s acquire timeout. If it doesn't, the request waits out the timeout and fails with `TimeoutError: acquiring connection from pool`. A slot's release time is written when it's claimed, so a crashed request can't leak a connection, and the runbook's leak check comes out negative as it should. Two simpler designs failed in the harness (`services/checkout-api/sim/pool_sim.py`):
- **Unconditional read-then-write.** Requests arriving within a round trip of each other share connections, and at burst rates that hides the fault.
- **Compare-and-swap of the whole item.** It's exact, but it serializes every request on the others' round trips, so the ledger becomes the bottleneck instead of the pool.

`tests/test_pool_ordering.py` drives the real handler through that harness and asserts the runbook's ordering, from pool wait to latency to errors.

Every break also starts `sentinelops-traffic-burst`: the same 6-minute ramp from 6 to 22 requests a second, whatever the mode, so the traffic's shape gives nothing away. Without it the pool fault never shows, and `bad_deploy`'s error alarm flaps on sparse failures. The harness replays the burst's own schedule (`traffic/burst.py`). Only one burst runs at a time, and Lambda drops a burst it can't start within a minute rather than run it late. checkout-api is capped at 80 concurrent executions, well above the burst's peak, because requests Lambda turns away never reach the metrics.

### Trigger (`trigger/`)

A small Lambda between EventBridge and the agent. Every investigation, alarm-driven or demo-driven, goes through it, so it's the one place global limits live:

- **One investigation at a time.** Conditional write on a `lock` item in DynamoDB, with a 420 s TTL, past the runtime's 360 s maximum lifetime, so a crashed run can't hold it forever. If the lock is held, the new incident is stored as `busy`, with no run.
- **Hourly cap.** The start times of the last hour's investigations, 20 at most. Past that, the incident is stored as `capped`, not investigated. The per-session guards don't protect against an alarm storm; this does.
- **Triage, in code.** One incident item per service. An ALARM with no open incident is new: it opens one and starts a run. Another alarm during an open incident is a duplicate, and the same alarm again is flapping; both are recorded, and neither starts a run. The rule forwards OK events too, and an incident closes once every alarm has been OK for 10 minutes, or 60 minutes after it opened. So a fault's later alarms (the pool fault's latency and errors) and its re-fires (`bad_deploy`'s error alarm) never start a second run. Code, not a model: deterministic, testable, free, and alarm text can't steer it. The first design gave this to Nova Lite, which has no Bedrock quota here and would have been a model call outside the guard. A one-line summary for the timeline, which gives the UI something to show in the first seconds, will come from the alarms in code too (planned, Phase 5).

Triage runs first, then the lock and the cap. The trigger then invokes AgentCore Runtime synchronously, a live run of the alarm that opened the incident, sent once and never retried. It stops the session, stores the answer (the Hypothesis, the run's numbers, and its whole record, gzipped) on the incident, and releases the lock. Timeouts nest, each outlasting the one inside it: the guard's 300 s, the runtime's 360 s maximum lifetime, the trigger's 370 s wait for the answer, the Lambda's 400 s, and the lock's 420 s (`trigger/tests/test_timeouts.py`). Lambda's automatic retries are off, so a failed trigger can't start a second investigation.

Everything lives in one table, `sentinelops-incidents` (`trigger/store.py`): each service's triage state, every alarm change with its verdict, every incident with its investigation, and the lock and the cap. A change's writes are one transaction conditioned on the state's version, so two alarms judged at once can't both open an incident.

Disabling the EventBridge rule (`sentinelops-alarms`, whose on/off state Terraform ignores) stops everything, including the demo, because the demo only injects faults. It never invokes the agent directly.

### Agent (`agent/`)

Strands agent loop on AgentCore Runtime. Structure:

```
agent/
  app.py               # the AgentCore Runtime entrypoint: POST /invocations, GET /ping (Starlette, uvicorn)
  main.py              # the local CLI: a live or replayed alarm, tools here or through Gateway
  investigation.py     # one alarm's investigation, for both entrypoints; its record is the transcript
  gateway.py           # the tools' wrappers that call them through Gateway (MCP, SigV4)
  knowledge_base.py    # finds the runbook knowledge base and checks it holds this checkout's runbooks
  build_runtime.py     # stages the Runtime's arm64 package for Terraform to zip
  alarm.py             # the alarm a run starts from, and the prompt it becomes
  runner.py            # one investigation: the Strands agent, its tools, structured output, under the guard
  hooks.py             # Strands hooks that put every model call through the guard
  providers.py         # the model setting and the sign-in by federation
  pricing.py           # per-token prices, by provider and model
  prompts/
    system.md          # the agent's operating instructions
  models.py            # Hypothesis (Pydantic); Remediation comes in Phase 4
  guards.py            # step cap, token cap, cost cap, timeout
  sources.py           # labels each tool result (m1, l2, d1, r1) so evidence can cite it
  checker/             # recomputes each typed evidence item from the result it cites, in code
  evals/               # sweeps over recorded alarms, rubrics, audits, the pass bar, the CI gate
  trace.py             # writes trace events to DynamoDB (Phase 5, not written yet)
```

**Model provider.** A setting, not a code path: `SENTINELOPS_MODEL_PROVIDER` is `anthropic` (the default) or `bedrock`, and `SENTINELOPS_MODEL_ID` picks the model. AWS declined my Bedrock quota requests on 2026-10-22, so the agent runs on the Anthropic API with Claude Haiku 5.5, through Strands' Anthropic provider. Bedrock is refused in code until it has quota; switching is then a setting plus its prices in `pricing.py`, since a model without a price can't start. The Anthropic API is signed in to by workload identity federation only; the API key it started with is retired (Security notes).

**How the guard sees every model call.** Strands' `BeforeModelCallEvent` hook bounds the coming prompt, prices the most the call can cost, and asks the guard; a call that could take spending past the cap is never sent. `AfterModelCallEvent` records the reply's real token usage and cost. Each tool is wrapped by `guarded_tool` before Strands registers it. Nothing else in the loop calls the model or edits the history: no conversation manager, no context management, tools one at a time.

Tools the agent can call:

| Tool | Backed by | Status |
|---|---|---|
| `get_recent_logs` | Lambda via Gateway | Built |
| `get_metrics` | Lambda via Gateway | Built |
| `get_deploy_history` | Lambda via Gateway | Built |
| `search_runbooks` | Lambda via Gateway (Bedrock KB `Retrieve`) | Built |
| `read_repo_file` | Lambda via Gateway (read-only GitHub token) | Phase 4 |
| `run_analysis` | AgentCore Code Interpreter (built-in, not a Lambda) | Deferred until the eval shows it's needed |

**Structured output.** The agent ends every run with a `Hypothesis`, through a strict tool schema (`models.py`):

```python
class Evidence(BaseModel):          # one typed statement, citing one tool result
    kind: EvidenceKind              # value, range, all, count, first, last, none, gap
    source: str | None              # the result's label: m1, l2, d1
    # plus the fields its kind uses: column, at, start, end, lines, value, low, high, count, seconds
    note: str

class Hypothesis(BaseModel):
    root_cause: str
    confidence: float               # 0 to 1
    evidence: list[Evidence]
    inferences: list[Inference]     # a conclusion and the evidence it rests on
    not_checked: list[str]
    affected_component: str
    fault_location: FaultLocation   # this_service, upstream, or undetermined
    runbook_cited: str | None
    halted_by: StopReason | None    # set only by code, hidden from the model's schema
```

Typed evidence is what makes the answer checkable: `agent/checker/` recomputes each item from the result it cites, with no model.

`runbook_cited` names the runbook the diagnosis follows, and code checks it once the run answers (`runner.check_citation`): it stays only if it names, exactly, a runbook a search in this run showed in full. Otherwise it's cleared and logged with its reason (never shown, only listed by name, or cut short by the result's cap), and the model isn't asked again. A runbook is never evidence: an evidence item citing a search (`r1`) is the checker's `miscited` flag.

From Phase 4, a `remediation` field (`file_path`, `base_commit`, `diff`, `rationale`) carries the patch, and the agent writes it itself. Nothing downstream calls a model, so all LLM spend stays inside the guarded session. `remediation` will be `None` when the fix isn't in this service (`downstream_timeout`) or the agent can't name one.

**Guards are not optional.** `guards.py` enforces:
- max 15 tool calls per incident
- max 200k cumulative input tokens
- max $0.10 per run, checked before each model call against the most that call can cost
- 5 minute wall clock
- 2 resends of a malformed answer

A limit ends the run with a halted `Hypothesis` (confidence 0, `halted_by` set), never an exception.

An agent stuck in a retry loop is the actual failure mode that costs money, not the pricing page.

### Tool Lambdas (`tools/`)

```
tools/                      # one uv project, sentinelops-tools (built 2026-10-28)
  sentinelops_tools/
    env.py                  # ToolEnv: the AWS clients and the run's clock, bound once per investigation
    metrics.py              # get_metrics: GetMetricData, one table of the service's metrics
    logs.py                 # get_recent_logs: FilterLogEvents, whole log lines
    deploys.py              # get_deploy_history: deploy and config-change history from DynamoDB, up to now
    toolset.py              # make_tools(env): the functions the model sees, with their docstrings
    runbooks.py             # search_runbooks: one Bedrock KB Retrieve, the 2 closest runbooks in full
    github_read.py          # planned, Phase 4: read a file, list recent commits
  build_runbooks.py         # the runbooks cut into one document per section, run before a plan
  ingest_runbooks.py        # the deploy's ingestion job, and its check that the KB holds this build
  tests/                    # replay recorded AWS calls through botocore's Stubber
```

The tools are plain Python functions in one project, which the agent depends on by path and imports directly while developing locally. The model sets only bounded parameters; the log group, the table, and the end of every time range come from the run, and a replay pins the clock to the moment the alarm fired. Logs are read with `FilterLogEvents` as whole lines, not through Logs Insights, which lags by minutes under load and flattens arrays; metrics are the primary source. An AWS error reaches the model as its error code only.

Deployed, each tool is its own Lambda (`sentinelops-tool-metrics`, `-logs`, `-deploys`, and `-runbooks`): a thin handler (`handlers.py`) around the same function, behind an AgentCore Gateway (`sentinelops-tools`, IAM auth) that serves them as MCP tools named `<target>___<tool>`. A handler takes the tool's arguments, plus `as_of` to replay a moment (capped at its own clock), and answers `{"text": ...}`. The agent's tools have the same names and descriptions whether they run locally or through Gateway, and only their bodies call Gateway, so the prompt and its token counts don't change. Only the gateway's role may invoke the functions, and AgentCore may assume that role only for a gateway in this account.

Each tool Lambda has its own IAM role scoped to exactly what it needs. The logs tool cannot write to DynamoDB, and the runbooks tool may only `bedrock:Retrieve` from the one knowledge base. The GitHub tool (Phase 4) won't be able to read CloudWatch, and its token will be read-only: the agent has no way to open a PR. Only the remediation flow will hold a write-capable token. It will also only see the service's own code (`services/checkout-api/checkout/`), never the fault simulator or controller.

### Knowledge base (`runbooks/`; built, Phase 3)

Seven Markdown runbooks, drafted from the service's actual behavior and reviewed one at a time: one for each fault the service injects, one for the planned memory leak, and three neighbors it never injects (slow database, traffic surge, Lambda throttling), so the closest runbook isn't always the right one. Each has the same five sections: Symptoms, What this is not, Diagnosis, Resolution, Prevention. They're the domain knowledge that makes the agent's answers specific rather than generic.

**Indexing.** `tools/build_runbooks.py` cuts each runbook into one document per section (35 today), each with a metadata file: the runbook, its title, the section, and a digest of the whole corpus. Terraform uploads them, and a Bedrock Knowledge Base backed by S3 Vectors embeds them with Titan Text Embeddings V2. It chunks nothing itself, so a chunk is exactly one section: the runbooks are already structured, and their own headers are the right boundaries. After the apply, the deploy runs an ingestion job and one search that must return every section of this build, tagged with its digest, and nothing else (`tools/ingest_runbooks.py`). A local run or an eval sweep runs the same check first and refuses a knowledge base holding other runbooks.

**Search** (`tools/sentinelops_tools/runbooks.py`). One `Retrieve` call returns every section, and the runbooks are ranked by position: each where its first Symptoms or Diagnosis section comes back. The 2 closest are shown in full (Symptoms, What this is not, Diagnosis), the rest by file name and title. Resolution and Prevention are never shown. There are no scores: on queries no runbook fits, the best score (0.67 to 0.78) fell inside the range for queries one does fit (0.62 to 0.92), so a score can't say "none of these". The query is cut at 500 characters, and the result is capped at 40,000, like the log tool's.

**Measured** (`tools/tests/fixtures/retrieval/`). On 40 queries a runbook fits, written in the agent's own words: 90% top-2, the bar, met with no margin, and 75% top-1. The 12 written after five runbook fixes, which the fixes never saw, are the fairer number: 83% top-2. Both of their misses, and 3 of the 4 misses overall, are on `memory-leak.md`, whose best-matching section is What this is not, which doesn't rank. A keyword baseline over Symptoms and Diagnosis nearly tied on top-2 (85%, and 83% on the fresh 12) and trailed on top-1 (65%). In the 20-run tuning sweep (2026-11-23), `downstream-timeout.md` ranked above `bad-deploy.md` on every bad deploy, and 3 of 9 pool runs needed a second search, yet the agent cited each fault's own runbook in all 20.

### Remediation flow (`remediation/`; planned, Phase 4)

Lambda Durable Functions runs the post-diagnosis sequence. It's deterministic, with no model calls:

1. **Gate.** Re-validate the payload into a `Hypothesis` and check `hypothesis.can_open_pr`. If it's False, post the findings to the incident and stop. This step contains no rule of its own: the threshold, the halted-run check, the fault-location check, and (from Phase 4) the `remediation is None` check all live in `can_open_pr` in `models.py`, so the agent's view and the remediation flow's view of "PR-worthy" can't drift apart. `can_open_pr` exists today, and a test fails the build if any other code compares confidence or checks `halted_by`.
2. **Validate.** The diff must apply cleanly to `base_commit`, and every touched `.py` file must pass `ast.parse`. The real tests run in GitHub Actions on the PR, not here.
3. **Open or update the PR.** Branch `sentinelops/<fault_mode>`. If an open PR already exists for that fault mode, push to it and add a comment linking the new incident. Without this, a public demo becomes hundreds of identical PRs.
4. **Notify.**

Durable execution means a failure at step 3 resumes at step 3 rather than re-running the whole thing. The incident ID is the idempotency key for each step.

### Live trace (planned, Phase 5)

- `trace.py` writes each event to a DynamoDB `trace_events` table, keyed by incident ID and sequence number. There's one event per step (tool call started, tool result summary, reasoning, hypothesis), not one per token. Per-token `postToConnection` calls would mean hundreds of API calls per run for no visible benefit.
- A DynamoDB Stream on that table drives a broadcaster Lambda that pushes each event to every open WebSocket connection. Connection IDs live in a `connections` table: written on `$connect`, deleted on `$disconnect`, with a TTL for connections that drop silently.
- Only one investigation runs at a time, so there's no subscription routing: every viewer watches the current incident. A viewer who connects mid-run, or between runs, first gets the stored events for the latest incident replayed.

### Frontend (`web/`; planned, Phase 5)

React 19 + Vite. One screen. Left pane: the incident timeline. Right pane: the live agent trace, one step at a time over API Gateway WebSockets.

The "Break the service" button is the demo. It has to be obvious, fast, and work without login. It lets the viewer pick one of the three live fault modes and calls a demo API that:

- refuses if an investigation is running or the hourly cap is hit, and shows the current or latest run instead
- limits each IP to one break per 10 minutes (DynamoDB counter with TTL). This is a speed bump, not protection, because IPs are cheap to rotate. The global lock and hourly cap are what bound spend.
- sets the fault flag and fires a burst of traffic, so the alarm doesn't wait on the 1-minute synthetic generator
- lets the fault clear itself: it expires after 390 s, when the burst ends

**Time to alarm.** Alarms use 10-second high-resolution periods on leading indicators (`pool_wait_time_ms` p99, error rate), so click-to-alarm targets under 60 seconds. Measured on 2026-10-27: 42 s for the pool fault and 36 s for the bad deploy. About 17 s of each is CloudWatch itself: an alarm changes state ~17 s after the end of the 10-second period that trips it, because EMF metrics arrive with Lambda's batched log delivery and the alarm evaluates every 10 s. The UI shows the metrics moving during that gap so it isn't dead air.

## Data flow, in order

```
a break (the fault controller today, the "Break the service" button from Phase 5), or a real fault
  → fault flag set, traffic burst
  → high-resolution alarm crosses threshold
  → alarm state change → EventBridge (native, no SNS)
  → trigger Lambda: triage (in code), lock, hourly cap
  → AgentCore Runtime session starts
  → agent: get_metrics()
  → agent: search_runbooks(what it measured)
  → agent: get_deploy_history()
  → agent: get_recent_logs()
  → agent: run_analysis(...)           # deferred
  → agent: read_repo_file(path)        # planned, Phase 4; only if the fix is in this service
  → agent emits Hypothesis (with Remediation or None, from Phase 4)
  → trigger stops the session, stores it, releases the lock
  → Durable Function: gate → validate → open/update PR → notify   # planned, Phase 4
  → trace closes, UI shows result                                  # planned, Phase 5
```

Measured: alarms fire 36-42 s after a break. The first live run (2026-10-29) took 22 s from start to answer, with 4 tool calls and 4 model calls, for $0.0094, well inside the caps. With runbook search, the 20-run tuning sweep (2026-11-23) took 18 to 48 s a run, with 4 to 7 tool calls, for $0.0084 on average.

## Security notes

- No long-lived AWS keys in the repo or CI. Local development uses one IAM user key in the `sentinelops` profile; CI uses OIDC.
- The Code Interpreter sandbox has no network egress. The agent can analyze data you hand it, not reach the internet.
- Two GitHub tokens, both in Secrets Manager (Phase 4): read-only for the agent's `github_read` tool, and write-scoped (contents and PRs on one repo) for the remediation flow.
- No Anthropic API key (since 2026-11-12): the agent signs in by Anthropic workload identity federation. The runtime's role gets a JWT from STS `GetWebIdentityToken` (audience `https://api.anthropic.com`, RS256, 300 s), an Anthropic federation rule trusts that exact role, and the SDK trades the JWT for a token of minutes. Local runs assume `sentinelops-local`, which only `sentinelops-dev` may assume and which may only ask STS for that JWT, under a second rule. The key it replaced, first kept in Secrets Manager, is disabled (kept that way, not deleted, until it expires on its own 2027-02-03) and its secret deleted.
- The public demo has no login, so its protection is the global lock and hourly cap in the trigger Lambda. Per-IP limits alone are not protection. A public demo endpoint plus an LLM is how people get surprise bills.
- Secrets in Secrets Manager, referenced by ARN in Terraform, never in tfvars committed to git.

## Terraform provider coverage

AgentCore Runtime and Gateway, S3 Vectors-backed knowledge bases, and Lambda Durable Functions are all new. I checked AWS provider support for each in Phase 0, so "everything in Terraform" stays true on purpose, not by accident. Any gap goes here, with the workaround chosen.

Checked 2026-09-30 against AWS provider 6.67.0. Region: `us-east-1`.

| Service | Terraform | Notes |
|---|---|---|
| AgentCore Runtime, Gateway, Gateway targets | `aws_bedrockagentcore_*` | The runtime resource exposes both settings, as `lifecycle_configuration` (`idle_runtime_session_timeout`, `max_lifetime`; 60 to 1,209,600 s), confirmed in 6.67.0's schema on 2026-10-29 |
| Bedrock KB on S3 Vectors | `aws_bedrockagent_knowledge_base`, `storage_configuration.type = "S3_VECTORS"` | Provider ≥ 6.27 |
| Lambda Durable Functions | `durable_config` on `aws_lambda_function` | Provider ≥ 6.25; us-east-1 since Dec 2025 |

Gaps: none so far.
