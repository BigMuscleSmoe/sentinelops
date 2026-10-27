# Logs Insights queries

For the log group `/aws/lambda/sentinelops-checkout-api`. Paste them into Logs Insights in the console, or run them with `aws logs start-query`. For a run, set the time range from the start in its `t0` file to about 470 s later.

Two things to know first:

- **Logs Insights lags behind ingestion under load.** After a burst it can be up to 3 minutes short, while the metrics from the same lines are already complete. Wait a few minutes before counting.
- **It flattens arrays.** A line's `stack` is only queryable as `stack.0`, `stack.1`, and so on, and `fields stack` returns nothing. To see a stack, read the whole line (`@message`).

## Settled counts for a run

The total should match the metrics' sample count of `latency_ms` over the same range, to within a line or two:

```
filter ispresent(route)
| stats count() as n by status, error_type
| sort status
```

Every invocation's platform report should say `success`. Anything else is a Lambda timeout or crash:

```
filter type = "platform.report"
| stats count() as n by record.status
```

## Which version served each request

For a `bad_deploy` run: 1.43.0 from a few seconds after the break until the clear, and 1.42.0 only outside that window.

```
filter ispresent(route)
| stats count() as n, min(@timestamp) as first, max(@timestamp) as last by version
```

## Failures, as whole lines

```
filter error = 1
| fields @timestamp, @message
| sort @timestamp
| limit 5
```

## The DynamoDB round trip

What a successful checkout spends outside its query, its upstream call, and its pool wait, which is mostly its flag read. This is the measurement `ROUND_TRIP_S` in `sim/pool_sim.py` cites: over 2026-10-27 03:10-18:30 UTC, 2,172 warm checkouts gave a median of 4.3 ms and a p95 of 11.4 ms.

```
filter route = "POST /checkout" and status = 200
| fields latency_ms - db_query_ms - upstream_latency_ms - pool_wait_time_ms as rest
| stats count() as n, pct(rest, 5) as p5, pct(rest, 25) as p25, pct(rest, 50) as p50, pct(rest, 75) as p75, pct(rest, 95) as p95, max(rest) as mx by cold_start
```
