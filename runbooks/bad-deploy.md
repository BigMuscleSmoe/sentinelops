# Runbook: Bad deploy (a release that fails requests)

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** this service's team. The release's author owns the fix.

## Symptoms

A release with a bug fails every request that takes the broken code path, from the moment it starts serving. Nothing builds up first: no queue, no slow dependency. The failures start within a period or two of the new version's first requests and hold at a steady share of traffic until the release is rolled back.

**The error alarm fires; the pool-wait and latency alarms stay OK**

- err% steps up at once, with no ramp before it, and holds at the share of requests that take the broken path. The error alarm needs err% above 5% in 2 of the last 3 10-second periods, so under load it fires soon after the onset. At baseline traffic a 10-second period holds only a few requests, so err% jumps between 0 and high values from one period to the next, and the alarm can go OK and fire again
- p99 latency does not rise. A request that fails in its own code answers quickly, and one that fails before it takes a database connection answers faster than one that succeeds. p50 can fall a little, since the fast failures count in it
- Only the routes that run the broken code fail. The other routes keep succeeding at their usual latency

**Logs:** each failure is a line with `status: 500`, `level: ERROR`, and `error: 1`, with `error_type`, `error_message` (the line's `message` is the two joined), and `stack`: the innermost frames, up to 10, each as `path:line in function`. The stack names the file and function that raised. Every failing line carries the new release's `version`.

**Where a failure stops a checkout:** `POST /checkout` prices the cart, then saves the order, which takes a database connection, then calls payments-api. A line records only the steps its request reached. A checkout that fails while pricing has no pool fields, no `db_query_ms`, and no payments fields; one that fails while saving the order has pool fields but no `db_query_ms`; one that fails at payments has all of them. The fields a failing line lacks show where it stopped, and the stack should agree.

**Deploy history:** a `kind: deploy` record shortly before the first failing period, whose `changes` show `version` going from the old release to the new one.

**Throughout:** `pool_wait_p99_ms` stays near zero, `pool_active_max` stays below `pool_max_size`, `db_query_p50_ms` stays flat, and `payments_p99_ms` and `payments_timeouts` stay at their usual level. The service's dependencies are fine; its own code is failing.

## What this is not

If latency climbs first and errors follow minutes later, this is not a deploy: the service is running out of something. Check `pool_wait_p99_ms` (see `db-pool-exhaustion.md`) and `db_query_p50_ms` (see `slow-database.md`).

If the failures are 504s with `UpstreamTimeout` and err% stays flat, the failing step is the payments-api call, not this release (see `downstream-timeout.md`).

If the change at the onset is a `kind: config` record, whose `changes` name a config key rather than `version`, the cause is that config change, not a release. A smaller pool, for example, fails requests by making them wait for a connection (see `db-pool-exhaustion.md`).

If the deploy's time doesn't fit, it's a coincidence. The failures start within a period or two of the new version's first requests, not before the deploy and not long after it. A deploy from hours before the onset, or one already rolled back, did not start failures that began just now.

If the same error appears on the old version's lines, before the deploy or beside it, the release didn't cause it.

If callers report errors that leave no line in this service's logs and no mark on err%, the requests never reached the code, or never finished it (see `lambda-throttling.md` and `memory-leak.md`).

## Diagnosis

1. Read the metrics. Note the first period in which err% rose, and check that latency, pool wait, database time, and payments-api stayed flat around it.
2. Read the deploy history. Find the newest `kind: deploy` record before that period, and its time. Account for every other record you read: say why its time or its change doesn't fit.
3. Read the failing lines (the log read's default, failures only). Note the `error_type`, the `error_message`, the top frame of the `stack`, and the `version`. Every failure should be on the new version.
4. Read the requests that succeed on the same route (failures only off, the same route). Find what the failing lines have in common that the successful ones on the new version don't: a field's value, or the shape of the request. A bug in one code path fails only the requests that take it.
5. Check which fields the failing lines lack (pool fields, `db_query_ms`, payments fields). They show the step the request stopped at, and that step should match the stack.
6. If the onset, the failing step, and the stack all point at the new release, the fault is in this service's code. Name the release, its deploy record's time, and the function that raised.

## Resolution

**Immediate:** roll back to the previous release, the `old` value in the deploy record's `version` change. A rollback is a deploy too, and shows in the history. Once the old release serves, err% falls back to 0 within a few periods. If it doesn't, the release wasn't the cause: reopen the diagnosis.

**Then:** fix forward. Find the commit in the release that changed the failing function, revert or fix it, and add a test that sends the request that failed.

**Do not** change config to make the errors stop. A bigger pool or a longer timeout doesn't fix code, and it adds a second change to untangle later.

**Do not** roll back past the release before the bad one. Older releases drop every change made since, and each of those then has to be deployed again.

## Prevention

None of these exists today; each is a proposal.

- After every deploy, a smoke test that sends one request of every shape the API accepts, on every route. Today's check after a deploy calls only `GET /health`, which never runs the checkout code
- A canary: a new release serves a small share of traffic first, and is rolled back on its own if its err% rises
- An error alarm per route, so a broken route shows even while the healthy routes keep the overall err% low
