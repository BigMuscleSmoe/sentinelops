# Runbook: Database connection pool exhaustion

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** Platform team

## Symptoms

- Error rate on `/checkout` climbs above 5% and keeps rising
- p99 latency degrades **before** the error rate moves, typically by 3–6 minutes
- Logs contain `TimeoutError: acquiring connection from pool`
- Database CPU and memory look normal — the database itself is fine
- Errors correlate with request volume, not with any particular endpoint

## What this is not

If error rate spikes instantly with no latency ramp, this is not pool exhaustion. Check for a recent deploy instead (see `bad-deploy.md`).

If latency climbs but errors stay flat and the slow spans are in an upstream call, the problem is downstream, not here (see `downstream-timeout.md`).

## Diagnosis

1. Check `pool_active_connections` and `pool_wait_time_ms` metrics in the `checkout-api` namespace.
2. Compare current `pool_max_size` config against request concurrency. Sustained concurrency above pool size means requests queue.
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
