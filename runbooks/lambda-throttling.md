# Runbook: Lambda concurrency throttling

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** this service's team. The cap is set on the function itself, in this service's infrastructure.

## Symptoms

checkout-api runs one request per Lambda instance, and the function is capped at 80 concurrent executions (its reserved concurrency). The 80 are its own: no other function in the account can take them, and it can't use more. When all 80 are busy, Lambda refuses the next invocation with a throttling error (`TooManyRequestsException`, reason `ReservedFunctionConcurrentInvocationLimitExceeded`, HTTP 429) before any of this service's code runs. API Gateway gets that refusal instead of a response from checkout-api, and answers the caller with an error of its own.

**What this service's metrics show**

- `concurrent_max` climbs to the cap and stays flat at or just under 80. It counts the requests in flight that use the pool, so `GET /health` calls aren't in it
- `req` stops rising, or falls, while callers keep sending: a throttled request is never counted
- err% can stay low while callers fail: throttled requests aren't in it, so the error alarm doesn't see them
- The requests that do run are slow. Concurrency is requests a second times how long each takes, and API Gateway's throttle in front of the function (a rate limit of 30 requests a second, with up to 60 at once) holds traffic down, so the cap is reached when requests take much longer than usual, not from traffic alone. p99 latency is high, and the latency alarm (p99 above 1,500 ms in 2 of the last 3 10-second periods) can fire

**What they don't show**

- A throttled request leaves no log line, no `latency_ms`, and no metric here. Lambda counts it in the function's own `Throttles` metric, which isn't among this service's metrics

## What this is not

If `concurrent_max` stays well under 80, Lambda is not throttling. Errors that callers see but this service never logs come instead from API Gateway's own throttle, which answers `429 Too Many Requests` (see `traffic-surge.md`).

If failures leave lines here (500s with stack traces, 504s with `upstream_timeout`), those requests ran: the service failed them, Lambda didn't refuse them. Start from the failing lines (see `bad-deploy.md`, `downstream-timeout.md`, `db-pool-exhaustion.md`).

The cap is almost always the effect, not the cause: something made requests slow, and slow requests pile up until they fill it. If `pool_wait_p99_ms`, `db_query_p50_ms`, or `payments_p99_ms` rose before `concurrent_max` reached the cap, that is the cause: follow its runbook (`db-pool-exhaustion.md`, `slow-database.md`, `downstream-timeout.md`).

## Diagnosis

1. Read the metrics over a window that starts before the onset. Find when `concurrent_max` reached the cap, whether it stayed flat there, and whether `req` flattened or fell at the same time.
2. Find what slowed requests first: which of `pool_wait_p99_ms`, `db_query_p50_ms`, `payments_p99_ms`, and p50 latency moved before the cap was reached. The first to move is the cause; the cap is its effect.
3. Read the request lines (failures only off). The requests that ran are slow, and their fields show where the time went: `pool_wait_time_ms`, `db_query_ms`, `upstream_latency_ms`. Throttled requests have no lines at all.
4. Read the deploy history for changes at the onset. A release or config change that slowed requests comes first. The cap itself is set on the function, not in the service's config keys, so a change to it may not appear in the deploy history.
5. The function's `Throttles` metric, and what API Gateway sent callers, aren't in checkout-api's metrics or logs. Say what you couldn't check, and what the service's owners should look at.

## Resolution

**Fix what made requests slow,** by its runbook. Once requests are fast again, concurrency falls below the cap and throttling stops on its own.

**Do not raise the cap to absorb slow requests.** More instances send more requests into the same pool of 20 connections and the same slow database or upstream: they queue longer and fail more. The cap protects what's behind it. Raise it only for a lasting rise in traffic at the usual speed, with the pool and API Gateway's throttle in mind (see `traffic-surge.md`).

**Callers should retry throttled requests** with exponential backoff and jitter, not at once: immediate retries keep the function at its cap.

## Prevention

None of these exists today; each is a proposal.

- Alarm on the function's `Throttles` metric, since throttled requests never reach this service's metrics or alarms
- Alarm on `concurrent_max` near the cap, which this service does emit, before requests are refused
- Turn on the stage's access logs with `$context.integration.integrationStatus` and `$context.integrationErrorMessage`, which record, for each request, the status Lambda returned to API Gateway and its error message
