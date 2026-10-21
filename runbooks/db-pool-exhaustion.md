# Runbook: Database connection pool exhaustion

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** Platform team

## Symptoms

Pool exhaustion arrives in stages, and the early signs come minutes before any request fails. Diagnose it from those: the first responder, human or agent, is expected to identify pool exhaustion before errors appear.

**Early: the pool is saturated, nothing fails yet**

- `pool_wait_time_ms` p99 rises above 100ms as soon as traffic outgrows the pool
- p99 latency degrades steadily while the error rate stays flat. A 0% error rate does not rule this out
- `pool_active_connections` is pegged at `pool_max_size`, and `concurrent_requests` is well above it

**Later: requests start timing out, typically 3–6 minutes after the wait first rises**

- Error rate on `/checkout` climbs above 5% and keeps rising
- Logs contain `TimeoutError: acquiring connection from pool`
- Errors correlate with request volume, not with any particular endpoint

**Throughout:** query time once a connection is acquired (`db_query_ms`) stays flat. The database itself is fine; requests are waiting for a connection, not for their queries.

## What this is not

If error rate spikes instantly with no latency ramp, this is not pool exhaustion. Check for a recent deploy instead (see `bad-deploy.md`).

If latency climbs while errors stay flat, check where the time goes before deciding. Early pool exhaustion looks like that too, but its time is in `pool_wait_time_ms`. If `pool_wait_time_ms` is flat and the time is in an upstream call (`upstream_latency_ms`), the problem is downstream, not here (see `downstream-timeout.md`).

## Diagnosis

1. Check `pool_active_connections` and `pool_wait_time_ms` metrics in the `checkout-api` namespace.
2. Compare current `pool_max_size` config against request concurrency (`concurrent_requests`). Sustained concurrency above pool size means requests queue.
3. Check for recent changes to `POOL_MAX_SIZE` in the service config. This has been lowered by accident twice.
4. Rule out leaked connections: if `pool_active_connections` stays pegged at max even as traffic drops, connections aren't being returned.

## Resolution

**Immediate:** raise `POOL_MAX_SIZE` to 2x observed peak concurrency, redeploy. Takes effect in ~90 seconds.

**If connections are leaking:** the fix is in code, not config. Look for a code path that acquires a connection and returns early without releasing it — usually an error branch missing a `finally` block or a context manager.

**Do not** raise the pool size past the database's `max_connections` limit divided by instance count. That converts a service outage into a database outage.

## Prevention

- Alarm on `pool_wait_time_ms` p99 above 100ms, which fires before user-visible errors
- Config changes to pool sizing require a second reviewer
- Load test at 1.5x expected peak before any pool config change
