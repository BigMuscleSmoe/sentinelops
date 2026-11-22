# Runbook: Slow database queries

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** this service's team owns the alarm and the escalation. A fix in the database belongs to the database's owners; a slow query this service's own release introduced is fixed here.

## Symptoms

Every route but `GET /health` takes a connection from the pool, runs one query, and gives the connection back. `db_query_ms` is how long that query held its connection. When the database slows down, every query takes longer, so every route that queries slows together, and each connection stays busy longer.

**First: query time rises on every route**

- `db_query_p50_ms` rises from its usual level at the onset. It is the earliest sign, and it rises on `GET /products/{id}`, `POST /cart/items`, and `POST /checkout` alike: the request lines' `db_query_ms` shows it per route
- p50 and p99 latency rise with it, on every route, by about the extra query time. The latency alarm (p99 above 1,500 ms in 2 of the last 3 10-second periods) fires if queries slow enough
- err% stays at 0 while there are still connections to spare

**Then, under load: the pool fills**

- Connections in use are about requests a second times how long each query holds one. At the same traffic, queries twice as slow keep twice as many connections busy, so `pool_active_max` climbs toward `pool_max_size` (20)
- Once concurrency passes the pool, requests queue: `pool_wait_p99_ms` rises, and the pool-wait alarm (p99 above 100 ms) can fire
- A request that can't get a connection within `POOL_ACQUIRE_TIMEOUT_MS` (1,500 ms) fails: a 500 with `TimeoutError: acquiring connection from pool (waited 1500 ms, pool_max_size=20)`, and err% rises. These failures follow the slow queries; they are not the cause

**Logs:** lines that got a connection carry `db_query_ms` above its usual level, on every route. `pool_max_size` on the lines stays at the configured 20, and the version is the one already running.

**Throughout:** `payments_p99_ms` and `payments_timeouts` stay at their usual level. Nothing in checkout-api's deploy history needs to have changed.

## What this is not

If `db_query_p50_ms` stays flat, the database is not slow, whatever else is.

If `pool_wait_p99_ms` rises while `db_query_p50_ms` stays flat, the pool is too small for the traffic, not too slow: check `pool_max_size` on the log lines and the deploy history for a change to `POOL_MAX_SIZE` (see `db-pool-exhaustion.md`).

If only `POST /checkout` slows, and `payments_p99_ms` rises with it, the time is in the payments-api call, not the database (see `downstream-timeout.md`).

If err% jumps at once, with stack traces from this service's own code and no rise in latency first, check for a bad deploy (see `bad-deploy.md`).

If query time rises only while traffic is high and falls back with it, the database is reaching its own capacity under load: start from the traffic (see `traffic-surge.md`).

## Diagnosis

1. Read the metrics over a window that starts before the onset. Confirm `db_query_p50_ms` rose, and find whether it moved before `pool_active_max`, `pool_wait_p99_ms`, latency, and err%. Query time moving first points here; pool wait moving first with query time flat points at the pool.
2. Read the request lines on each route (failures only off, one route at a time). `db_query_ms` should be up on all three. A rise on one route only points at that route's query, not the whole database.
3. Read `pool_max_size` on the lines. If it's below the configured 20, the pool shrank: see `db-pool-exhaustion.md`.
4. Read the deploy history for the minutes before the onset. A release there may have changed a query: then the slow query is in this service's code, and rolling the release back should bring `db_query_ms` back (see `bad-deploy.md` for the rollback). If nothing changed in checkout-api, the slowdown is in the database itself.
5. The database's own metrics, its load, locks, and slow-query log, are not in checkout-api's metrics or logs. Say what you couldn't check, and what the database's owners should look at.

## Resolution

**If a release at the onset slowed a query:** roll it back, then fix the query in the code.

**If nothing in checkout-api changed:** escalate to the database's owners with the onset time, `db_query_p50_ms` per minute, and which routes slowed. checkout-api has nothing to roll back.

**Do not raise `POOL_MAX_SIZE`** to absorb the wait. More connections send more concurrent queries to a database that is already slow, and can push it past its own connection limit: a slow database becomes a down one.

**Do not lower `POOL_ACQUIRE_TIMEOUT_MS`** to fail faster: it turns slow requests that would have succeeded into 500s.

The incident ends when query time returns to its usual level; pool wait and latency follow it down.

## Prevention

None of these exists today; each is a proposal.

- Alarm on `db_query_ms` itself. Today a slow database shows only through the latency and pool-wait alarms, after its effects reach them
- Log each query's name beside its time, so a single slow query stands apart from a slow database
- Link the database's own dashboards from this runbook, so its load and slow-query log are one click away
