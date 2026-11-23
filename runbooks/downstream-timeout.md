# Runbook: payments-api timeouts (a slow downstream)

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** this service's team owns the alarm and the escalation. The fix belongs to payments-api's owners.

## Symptoms

checkout-api calls payments-api once per checkout, after the order is written, and waits at most `PAYMENTS_TIMEOUT_MS` (2000 ms). When payments-api slows down, checkouts slow down with it, and the slowest become 504s. The fault is in the dependency, not in this service: nothing in checkout-api changes, and its deploy history shows no change that acts on the payments call.

**The latency alarm fires; the error and pool-wait alarms stay OK**

- p99 latency over all routes passes the 1.5 s threshold soon after the slowdown begins and stays above it. Checkouts are a minority of requests, but their tail is the payments call, and it sets the p99
- p99 above 2 s, checkout only: `POST /checkout` p99 sits just above the 2000 ms timeout. Other routes stay fast: `GET /products/{id}` and `POST /cart/items` never call payments-api
- Payments p99 near the timeout: `payments_p99_ms` rises from its usual level to about the timeout, since a timed-out call is recorded at the timeout. The typical call (the request lines' `upstream_latency_ms`) slows well past its usual level, though most calls still finish inside the timeout
- A few timeouts a minute: `payments_timeouts` stays above 0 under load, a small share of checkouts, not all of them
- `concurrent_max` climbs under load, since each checkout holds its request open for the call. The extra instances' cold starts can lift the other routes' p99 a little; their warm requests are unchanged

**err% stays at 0.** A 504 is the upstream's failure, not this service's: its log line has `error: 0`, and it counts under `payments_timeouts`, never err%. A flat error rate does not rule this out.

**Logs:** each timeout is a line with `status: 504`, `level: ERROR`, and `UpstreamTimeout: payments-api did not respond within 2000 ms`, with no stack, on the version already running, with and without a promo. Its `latency_ms` is just over the 2000 ms timeout. The failures-only log read returns these lines, and the message names the timeout in effect. On the slow checkouts that finish, `upstream_latency_ms` is most of `latency_ms`.

**Throughout:** `pool_wait_p99_ms` stays near zero, `db_query_p50_ms` stays flat, and `pool_active_max` stays below `pool_max_size`. The order's connection goes back to the pool before the charge, so a slow payments-api holds no connection.

## What this is not

If `pool_wait_p99_ms` rises with the latency, the time is spent waiting for a connection: that is pool exhaustion (see `db-pool-exhaustion.md`), not this.

If `db_query_p50_ms` rises with the latency, the time is in the database's queries, and every route slows, not only checkout (see `slow-database.md`).

If err% jumps, the failures are 500s with a stack, or a new version appears in the logs or the deploy history, check for a bad deploy (see `bad-deploy.md`). A slow payments-api produces no 500s.

If `payments_timeouts` rises while payments-api's typical call stays at its usual latency, the timeout itself may have been lowered. Check the deploy history for a change to `PAYMENTS_TIMEOUT_MS`, and read the timeout the 504 lines name. That change is in this service, and is fixed here.

## Diagnosis

1. Read the metrics. If `payments_p99_ms` and `payments_timeouts` rose at the onset while `pool_wait_p99_ms` and `db_query_p50_ms` stayed flat, the time is in payments-api.
2. Read the failing log lines. 504s with `UpstreamTimeout` naming payments-api, and no 500s, confirm it.
3. On the 504 lines, compare `upstream_latency_ms` with `upstream_timeout_ms`. `upstream_timeout_ms` is the limit in effect for that request, and a timed-out call's `upstream_latency_ms` sits at it: payments-api used the whole limit and never answered. If `upstream_timeout_ms` is below the configured `PAYMENTS_TIMEOUT_MS`, the timeout was lowered, a change in this service (see "What this is not"). Successful checkout lines carry the same two fields: calls that succeed close to the limit mean payments-api is slow for everyone, not only for the 504s.
4. Compare routes: `GET /products/{id}` and `POST /cart/items` should be at their usual latency. If they are slow too, the cause is not payments-api alone.
5. Read the deploy history for the minutes before the onset. If nothing was deployed or changed there, checkout-api did not cause it. A deploy or config change from well before the onset, or one already rolled back, did not start a slowdown that began just now.
6. The fault is upstream: nothing in checkout-api's code or config fixes it, so no change to this service is proposed.

## Resolution

**Immediate:** escalate to payments-api's owners with the onset time, `payments_p99_ms`, and `payments_timeouts` per minute. checkout-api has nothing to roll back.

**Do not raise `PAYMENTS_TIMEOUT_MS`** to make the 504s go away. Each checkout then holds its request longer and concurrency climbs with it. checkout-api is capped at 80 concurrent requests, and past that every route is throttled, not only checkout. Do not lower it either: that turns slow charges that would have succeeded into 504s.

**Do not resize the pool or roll back checkout-api:** neither is involved, and a rollback changes code that isn't failing.

**Do not add a retry during the incident.** checkout-api calls payments-api once and never retries. A retried charge after a timeout can charge a card twice unless payments-api deduplicates it.

The incident ends when payments-api recovers: the latency alarm returns to OK soon after.

## Prevention

None of these exists today; each is a proposal.

- Alarm on `payments_timeouts` and on payments-api's latency directly. Today a slow payments-api shows up only through the all-routes latency alarm, since err% leaves out 504s
- Agree a latency objective with payments-api's owners, and set `PAYMENTS_TIMEOUT_MS` from it
- Before any retry on the charge, require an idempotency key that payments-api honors
- A circuit breaker on the payments call, so a slow payments-api fails checkouts fast instead of holding each one for the full timeout
