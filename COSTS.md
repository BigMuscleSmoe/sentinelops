# Costs

Read this before your first `terraform apply`.

## Expected

| State | Monthly |
|---|---|
| Idle, demo service up | $5–15 |
| Active development, heavy agent testing | $40–80 |
| Torn down between job applications | ~$1 |

Comfortably inside a typical credit grant. The failure mode isn't steady-state spend, it's a runaway loop.

## What actually bills

### Model tokens on the Anthropic API (as long as Bedrock isn't used)

AWS declined my Bedrock quota requests on 2026-10-22, so the agent calls the Anthropic API directly. It needs no Bedrock quota: the Runtime runs my code, and that code calls the Anthropic API. This spend bills to the Anthropic account: the AWS credits, the AWS budgets, and Cost Explorer never see it.

**Prices: Claude Haiku 5.5, per million tokens.** Source: Anthropic's pricing page, https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-28 and again 2026-11-15 (unchanged).

| Prompt length | Input | Cache write (5 min) | Cache write (1 h) | Cache read | Output |
|---|---|---|---|---|---|
| Up to 100,000 tokens | $0.10 | $0.125 | $0.20 | $0.01 | $0.50 |
| Over 100,000 tokens | $0.50 | $0.625 | $1.00 | $0.05 | $2.50 |

The page's rule for the threshold, verbatim: "Claude Haiku 5.5 is priced by prompt length: a request whose prompt is over 100,000 tokens pays higher prices. A request's prompt length counts all of its input tokens, including cache reads and cache writes. Each request is priced on its own: a request over the threshold pays the higher prices even when part of its prompt is a cache hit, and earlier requests keep the prices they were billed at."

- These prices are in `agent/pricing.py` (`PRICES`, with `PRICING_SOURCE` and `PRICES_CHECKED`), and `test_pricing.py` pins them. A model with no entry is refused at startup, so changing models means checking this page and adding its prices first.
- For comparison, from the same page: Sonnet 5.5 is $2 in and $10 out, and Opus 5.5 $4 and $20, with no prompt-length tiers. A typical run would cost about 20x on Sonnet. Sonnet 5.5's prices are in `PRICES` since 2026-11-15 (`dd16dc6`), for the eval's second arm only ($0.50 a run, $6.00 for the arm since 2026-11-16, at 2 runs an alarm: 22 test runs projected at $5.16): under the production cap the guard refuses its first call, which reserves $0.16. Its one check run (2026-11-16, a `bad_deploy` alarm) cost $0.2774, 43x Haiku's $0.0064 on the same alarm (one resend made 43% of it). The test sweep's 22 Sonnet runs then cost $2.1455, a mean of $0.0975 a run, against $0.1991 for Haiku's 33, a mean of $0.0060: about 16x (`agent/evals/reports/2026-11-18-test-sweeps.md`).

**Per run:** a replay of the `bad_deploy` alarm sends about 8.9 KB on its first request (a 4.6 KB system prompt, 3.7 KB of tool definitions, the alarm), and each tool result adds 2-13 KB (a 60-line log sample, 33 KB). The six runs on 2026-10-28 (five replays and the live test) cost $0.0054-0.0100 each, the live one 77K input tokens over 4 model calls. A longer investigation of 6-9 tool calls is about 50-120K input tokens and a few thousand output tokens: **~$0.01-0.02**.

**Hard cap: $0.10 per run, in code** (`Limits.max_cost_usd` in `guards.py`, applied by `hooks.py`).
- Before every model call, the most it can cost is reserved: its prompt as the API's `count_tokens` counts the request about to go out, plus a margin of 10% or 1,000 tokens, whichever is larger, and the full 16,000 output tokens, at the dearest input rate and the prompt length's tier. Counting is free, with its own rate limit (10,000 requests a minute at Build).
- The byte bound is the ceiling and the fallback: the last reply's exact token counts, one token per byte of everything added since (a token is never shorter than a byte), and 2,000 tokens for the API's own framing (the pricing page puts Haiku 5.5's tool-use system prompt at 286-406 tokens). A count plus margin above it is cut to it; a failed or skipped count uses it for that call; a count that proves low by more than its margin switches the rest of the run to it.
- If what's spent plus that reservation would pass $0.10, the call is never sent and the run ends with a halted `Hypothesis` (`cost_limit`). After each call, its real cost from the reply's usage is added.
- **The cap is no longer strictly provable** (a tradeoff I accepted on 2026-10-28), since the API calls its count an estimate. Within a price tier, the dearest-rate reservation (twice the plain input rate) still covers a limit as low as half the real prompt, while caching is off. The exposure is a count low by more than its margin on a prompt that crosses 100K, where the whole request pays the long-prompt prices: $0.07 spent, a 90K count reserving ~$0.028, and a real prompt just over 100K billing up to ~$0.09 make a ~$0.16 run. It can happen once a run at most, since a miss switches the rest to the byte bound, and the $10 monthly limit is the backstop. In three replays and the live run on 2026-10-28, all 15 counts equaled the billed prompt.
- A reservation was $0.009-0.015 in those runs, mostly the output allowance, so a run stops by about $0.085-0.09 spent. A failed call is charged its whole reservation.

**Monthly:** a $10 monthly spend limit in the Anthropic Console, which I set on 2026-10-28. Anthropic enforces it even if my code is wrong; at the limit the API answers 400 `invalid_request_error`, not 429. At ~$0.015 a run that's over 600 runs; at the cap, 100.

**The retired key's secret** was deleted on 2026-11-12, into its 30-day recovery window, during which Secrets Manager doesn't charge for it; it was $0.40 a month. Nothing in Secrets Manager now. Federation itself costs nothing: STS `GetWebIdentityToken` and `AssumeRole` are free.

### Bedrock tokens: the largest line item, if Bedrock is used

The agent doesn't use Bedrock today. Until it does, model tokens bill on the Anthropic API (above).

Every agent turn resends the full conversation plus accumulated tool results. An 8-step investigation costs roughly 8x a single prompt, not 1x. Budget 50k–150k input tokens per incident run.

Mitigations:
- Triage in code, in the trigger, so only a new incident starts a model run (Nova Lite was the first design)
- Truncate log payloads before they enter context: 200 lines, not 20,000
- Prompt caching on the system prompt and runbook context

### AgentCore Runtime

Runtime v1: $0.0895 per vCPU-hour and $0.00945 per GB-hour. Runtime v2: $0.1276 per vCPU-hour and $0.0169 per GB-hour, and it reclaims idle memory after 120 s (the AgentCore pricing page, read 2026-10-29). The page doesn't say which version a new runtime gets, so budget at v2's rates and check the first bill. Billed per second (1 s minimum) on CPU actually used, which scales to zero in I/O wait (where a run waiting on the model spends most of its time), and on **peak** memory including system overhead, with a 128MB floor. A direct-code zip is billed at S3 Standard rates.

**Per run** (~30-40 s, then `StopRuntimeSession`; a replay through the Runtime took 40 s from invoke to answer): about $0.0003 with Gateway's calls. The trigger adds about $0.0001: its Lambda waits out the run (~45 s at 128 MB), and the incident with its gzipped record is ~30 DynamoDB write units; the rule's AWS events are free. A full month at the trigger's cap of 20 runs an hour comes to about $6, under $10.

The number looks negligible and the trap is real: one developer measured $5.63 across 29 minutes from seven runtimes, because even a single Bedrock call registered multiple GB on CloudWatch once overhead was counted. Idle sessions that don't time out are where it goes.

- Set `idleRuntimeSessionTimeout=120`
- Set `maxLifetime` to something sane, not the 8-hour default: 360 s, behind the guard's 300 s (as built)
- Delete test runtimes when you're done with them, don't leave seven lying around

### AgentCore Web Search

$7 per 1,000 queries. **Don't enable it.** This project doesn't need the open web, and it's the easiest accidental spend in the stack.

### Gateway

$0.005 per 1,000 `ListTools`, `InvokeTool`, and `Ping` calls (the AgentCore pricing page, read 2026-10-29). Semantic search costs $0.025 per 1,000 searches plus $0.02 per 100 indexed tools a month, so it stays off: three tools need no search. Identity is free through Runtime or Gateway. A run makes about ten billed calls, ~$0.00005: the agent opens an MCP session per tool call, and in each `mcp` lists the tools before calling one (measured 2026-10-30). The tool Lambdas behind it fit in the free tier.

### The runbook knowledge base (Bedrock, on S3 Vectors)

35 documents, one per runbook section (36 KB of text in all), embedded with Titan Text Embeddings V2 into an S3 Vectors index of 35 vectors, about 0.2 MB with their text. **Under a cent a month at any use this project makes of it.**

| What bills | Price, us-east-1 | Here |
|---|---|---|
| Titan Text Embeddings V2 | $0.00002 per 1,000 input tokens, on demand | Embedding the whole corpus again: under $0.001 (at most a token a byte). A search embeds only its query, a few dozen tokens |
| S3 Vectors storage | $0.06 per GB-month | Under $0.0001 a month |
| S3 Vectors uploads | $0.20 per GB put | Under $0.0001 for the whole index |
| S3 Vectors queries | $2.50 per million, plus $0.004 per TB processed (the first 100K vectors' rate) | $0.0025 per 1,000 searches |
| The documents' bucket | S3 Standard | 70 objects, 42 KB |

Titan's price is from AWS's Price List API, effective 2026-11-01 (read 2026-11-22; the Bedrock pricing page draws its table by script). S3 Vectors' are from the S3 pricing page's worked examples, which use us-east-1 (read 2026-11-21 and 2026-11-22).

- **The knowledge base itself shows no charge.** The Price List API (AmazonBedrock, us-east-1, all 1,105 products, read 2026-11-22) has one knowledge-base line, $2 per 1,000 `GenerateQuery` calls for SQL retrieval, which this doesn't use; none for a vector knowledge base, an ingestion job, or `Retrieve`. The first bill is the check.
- **Each deploy** runs one ingestion job and one search. A job embeds only what changed, but an edit to any runbook changes every section's metadata file (each holds the corpus digest), so all 35 are indexed again: under $0.001.
- **Not a Managed Knowledge Base,** AWS's recommended default: $1.00 per 1,000 `Retrieve` calls and $5.00 per GB of raw data a month (the Bedrock pricing page, read 2026-11-12). Still small at this scale, but 400 times the query fee above, for a job this one does.
- **Never OpenSearch Serverless** (below).

### The GitHub App's key (KMS)

The remediation flow's GitHub App signs its JWTs with an RSA 2048 key in KMS, whose key material I import once by hand (`infra/github_app.tf`); the key itself is never readable. **$1 a month**, prorated hourly, from the moment Terraform creates it, imported or not. Signing costs $0.03 per 10,000 requests for an RSA 2048 key, and the flow signs a JWT only to get an installation token, so the requests cost nothing measurable. Both prices are from AWS's Price List API (`awskms`, us-east-1, read 2026-11-24). Terraform may never destroy the key, since no copy of the private key exists, and a deletion by any other route waits out a 7-day window in which it can be cancelled.

### Memory, Code Interpreter

Consumption-based, small at demo scale. Not worth optimizing until they show up in Cost Explorer.

## Cheap or free at this scale

Lambda, DynamoDB, S3, EventBridge, API Gateway, CloudFront, Cognito: under $5/month combined, mostly free tier.

High-resolution (10-second) alarms and metrics for the demo cost more than standard ones, but at a handful of alarms it's a few dollars a month at most.

**S3 Vectors for the knowledge base: under a cent a month** (above). OpenSearch Serverless doing the same job is roughly $700/month. This single choice is the difference between a portfolio project and a bill you can't pay. Do not let a tutorial talk you into OpenSearch.

## The sneaky ones

| Trap | Cost | Avoid by |
|---|---|---|
| **NAT Gateway** | ~$32/mo just to exist, plus data processing | Don't put Lambdas in a private VPC subnet unless you genuinely need to. Most wizards do this by default. |
| **CloudWatch Logs retention** | Grows silently forever | 7-day retention on every log group, in Terraform, from day one |
| **Always-on demo host** | $30+/mo for an idle EC2 | Lambda or Fargate Spot, never a t3.medium sitting there |
| **Orphaned resources** | Varies | `terraform destroy` on scratch environments; check for leftovers manually after experiments |

## Guardrails: set these before writing code

1. **AWS Budgets alarm at $25** with email alerts
2. **Bedrock invocation quotas** set low on the account. Waits until Bedrock is used; until then model spend is on the Anthropic API, bounded by #7.
3. **Step cap in agent code:** hard stop at 15 tool calls, enforced in `guards.py`, not in the prompt
4. **Global limits in the trigger Lambda:** one investigation at a time, max 20 per hour. The demo is public and unauthenticated, so this is the only thing that bounds what strangers can spend. Per-IP rate limits are a speed bump, not a ceiling.
5. **`cost_center` tags** on everything via Terraform `default_tags`
6. **Daily Cost Explorer check** for the first week
7. **Cost cap per run in agent code:** $0.10, checked before every model call against its `count_tokens` count plus a margin, never above the byte bound (`guards.py`, `hooks.py`), plus my $10 monthly limit in the Anthropic Console while the model runs on the Anthropic API

Filter Cost Explorer on `AmazonBedrockAgentCore`, not `AmazonBedrock`. They're separate products with separate billing models, and charges under `bedrock:InvokeAgent` mean you're accidentally on the legacy Bedrock Agents service instead.

## If something runs away

1. Disable the EventBridge rule that invokes the agent (`aws events disable-rule --name sentinelops-alarms --region us-east-1`; drilled 2026-10-30, and a deploy leaves it disabled). This stops new incidents instantly, including ones started from the public demo, since the demo only injects faults and never calls the agent directly.
2. Stop active AgentCore Runtime sessions (`aws bedrock-agentcore stop-runtime-session`, with the session ID from the trigger's latest `investigating` log line; the README's kill switch has the full command).
3. Check Cost Explorer with hourly granularity to find which component spiked.
4. Fix the loop, then re-enable.

Keep step 1 as a one-line command in your notes. You want it available at 3am without thinking.
