# Runbook: Memory leak

**Service:** checkout-api
**Severity:** SEV-3 while failures are rare; SEV-2 once they are a steady share of requests
**Owner:** this service's team: a leak is in this service's code.

## Symptoms

Each Lambda instance keeps its process between requests, so whatever the code holds onto outside a request is still in memory for the next one. A leak adds a little memory with every request an instance serves, and each instance climbs, request by request, toward the function's 256 MB. When an instance runs out, its runtime is killed in the middle of a request, and that request fails. Lambda then resets the instance, and the next request on it starts the runtime again, as a cold start. The cycle repeats on every instance, so failures arrive as a steady trickle, not a step.

**What this service's metrics and logs show**

- `cold_start: true` lines keep appearing while `req` stays steady: instances that ran out are being replaced, not added for traffic
- p99 latency can rise with them, since a new instance's first request is slower than its warm ones. The latency alarm (p99 above 1,500 ms in 2 of the last 3 10-second periods) can fire
- err% stays low, and the error alarm may never fire: a killed request leaves no log line, so it isn't in `req`, err%, or latency
- `pool_wait_p99_ms`, `db_query_p50_ms`, and `payments_p99_ms` stay flat. A leak doesn't have to slow requests before the kill
- The first failures come well after the change that caused them, since an instance has to serve many requests before it runs out. The busier the service, the sooner they start

**What they don't show: Lambda's own records**

- Lambda writes a `platform.report` record for every invocation into the function's log group, beside this service's lines but without their fields (`service`, `route`, `status`). Its `record.metrics` holds `maxMemoryUsedMB` and `memorySizeMB` (256), and its `record.status` is `success`, `error`, `failure`, or `timeout`. Under a leak, `maxMemoryUsedMB` climbs toward 256. A killed request's report has `status` `error`, memory at or near 256, and no line from this service with its request ID (`record.requestId`, which this service logs as `request_id`)
- Lambda's `Errors` metric counts invocations that end in a function error, the runtime's as well as the code's. It isn't among this service's metrics

## What this is not

If failures leave lines here, with stack traces, and began right after a release, it's a bad deploy, not a leak: a leak's failures leave no lines, and start only once instances have served many requests (see `bad-deploy.md`).

If `concurrent_max` sits flat at the cap of 80, requests are being refused, not killed (see `lambda-throttling.md`). Refused requests leave no lines either, but they bring no stream of cold starts.

If cold starts rise with `req`, Lambda is adding instances for more traffic (see `traffic-surge.md`). A leak replaces instances while traffic stays steady.

A few cold starts at steady traffic are normal: Lambda replaces instances every few hours for maintenance, even under constant load. A leak makes them regular.

A request that runs into the function's 10-second timeout also resets its instance and leaves no line, but its report says `timeout`, not `error`. This service's own limits (`POOL_ACQUIRE_TIMEOUT_MS`, `PAYMENTS_TIMEOUT_MS`) end its requests well before that.

## Diagnosis

1. Read the request lines over a long window (failures only off). Count the `cold_start: true` lines against `req`, and note when a steady stream of them began.
2. Read the metrics over the same window. Confirm that `req` stayed steady, that `concurrent_max` stayed well under 80, and that pool wait, query time, and payments stayed flat.
3. Read the deploy history further back than the onset: the release that introduced a leak can come well before the first failures. A release followed, after a delay, by the stream of cold starts is the suspect.
4. Lambda's `platform.report` records and its `Errors` metric aren't in checkout-api's metrics, and the log reads return only this service's lines. Say what you couldn't check: whether `maxMemoryUsedMB` climbed toward 256, and whether the failed requests' reports show `status` `error`.

## Resolution

**Roll back the release that introduced the leak.** Instances on the old release start with fresh memory, so failures stop as the leaking instances are replaced. Then find what the code keeps between requests: anything added to a module-level list, dict, or cache on each request and never trimmed.

**Do not raise the function's memory to fix it.** More memory only spaces the kills out, and every request pays for the larger size. Raise it only when each request really needs more, in which case `maxMemoryUsedMB` is flat at a higher level, not climbing.

**A redeploy of the same release is not a fix:** it clears every instance's memory, so failures stop for a while and then come back.

## Prevention

None of these exists today; each is a proposal.

- Log each request's process memory (its peak resident size) on the request line, so this service's own metrics show the climb
- Alarm on the function's `Errors` metric, which counts the failures this service never logs
- A soak test before each release: one instance serving many requests in a row, with `maxMemoryUsedMB` checked for a climb
