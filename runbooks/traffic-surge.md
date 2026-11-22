# Runbook: Traffic surge

**Service:** checkout-api
**Severity:** SEV-3 while the service absorbs it; once requests fail, the severity of what failed
**Owner:** this service's team.

## Symptoms

More requests arrive than usual. On its own that isn't a failure. The service scales out, one Lambda instance per request in flight, and each request that queries takes a connection from one shared pool of 20 for as long as its query runs. A surge the service absorbs looks like this:

**`req` rises; little else moves**

- `req` rises, and `concurrent_max` and `pool_active_max` rise with it, in proportion: connections in use are about requests a second times how long each query holds one
- p50 latency stays at its usual level. p99 can rise while new instances start: each new instance's first request is slower than its warm ones (`cold_start: true` on that line)
- err% stays at 0, `pool_wait_p99_ms` stays near zero, and `db_query_p50_ms` and `payments_p99_ms` stay flat
- No alarm fires: none of the three alarms watches traffic. If one fired, the surge met a limit or arrived with a fault: see What this is not

**At the edges: what the service never sees**

- API Gateway throttles requests at the stage's default route settings: a rate limit of 30 requests a second, with up to 60 at once. Requests past them are throttled before they reach checkout-api, and callers get `429 Too Many Requests` from API Gateway. They leave no log line and no metric here: nothing in `req`, nothing in err%. A `req` that flattens while callers report errors is the sign
- checkout-api is capped at 80 concurrent executions. Past that, Lambda refuses the invocation before the code runs (see `lambda-throttling.md`)

## What this is not

If the pool fills under traffic it has held before, check `pool_max_size` on the log lines and the deploy history. A smaller pool is a config change, not a surge (see `db-pool-exhaustion.md`).

If `db_query_p50_ms` rises with the traffic, the database is slowing under the load (see `slow-database.md`).

If only `POST /checkout` slows, with `payments_p99_ms` up, the time is in payments-api (see `downstream-timeout.md`).

If err% steps up to a steady share at once, with stack traces and a new version on the failing lines, it's a bad deploy that the traffic only made louder (see `bad-deploy.md`).

If callers see errors that leave no line in this service's logs, the requests were refused before the code ran: by API Gateway's throttle (429s, above) or by Lambda's concurrency cap (see `lambda-throttling.md`).

Traffic is the trigger, not the cause, whenever something else changed at the onset: a config value, a release, a dependency. A surge is the cause only when nothing else changed and the extra traffic alone reached one of the limits above.

## Diagnosis

1. Read the metrics over a window that starts before the rise. Note when `req` rose, which of err%, latency, `pool_wait_p99_ms`, and `db_query_p50_ms` moved, and whether each moved before or after it.
2. If only `req`, `concurrent_max`, and `pool_active_max` moved, the service absorbed the surge, and an alarm that fired has another cause: follow the signal that moved.
3. If pool wait rose, compare `pool_active_max` with `pool_max_size` on the log lines. Connections pegged at the configured 20, under much more traffic than usual and with query time flat, mean the pool is too small for this load. Pegged at a smaller size, the pool was changed (see `db-pool-exhaustion.md`).
4. Read the deploy history for changes at the onset. A change whose time fits the onset is a suspect before the traffic is.
5. Where the extra traffic comes from isn't in checkout-api's logs: the lines carry no client fields. Nor are the 429s, which are in API Gateway's own metrics, not this service's. Say what you couldn't check.

## Resolution

**If the service absorbed it:** nothing to fix. Note the peak, so the next load test covers it.

**If a limit was reached:** follow the runbook for that limit (the pool, the database, the concurrency cap). Raise a limit only with the next one in mind: more requests through API Gateway mean more concurrency, more connections, and more load on the database. Each limit protects the one after it.

**If the surge is clients retrying failures:** fix the failure first. Retries multiply the load a failure creates, and they stop when it does.

## Prevention

None of these exists today; each is a proposal.

- Alarm on API Gateway's 429 count, since throttled requests never reach this service's metrics
- Load test at 1.5x the expected peak before changing any limit: the API's throttle, the concurrency cap, or the pool
- Rate limits per client, so one caller's surge can't use the whole API's limit
