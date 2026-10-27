# Live-run tools

Scripts that break the deployed checkout-api on purpose and measure what happened: when each alarm fired, what the metrics and logs showed, and whether any line gave the fault away. They run against the real stack, so each run sets off real alarms and costs a few cents at list price. None of this is deployed: `infra/checkout_api.tf` keeps `tools/` out of every zip.

- `live_run.sh`: breaks the service and watches the alarms, then clears the fault.
- `analyze.py`: reads back the run.
- `insights_queries.md`: Logs Insights queries for settled counts and whole failure lines.

## Before a run

- You need Git Bash or Linux, the AWS CLI with the `sentinelops` profile, and Python 3.12.
- All three `sentinelops-checkout-*` alarms must be OK. `live_run.sh` won't start otherwise.
- Leave 7 minutes after the last burst started. Only one burst runs at a time, and a break during one gets no burst of its own.
- Don't push to `main` during a `bad_deploy` break. CI's smoke test checks the version, which the fault changes.

## Run a break

From the repo root, run it in `scratch/`, which git ignores, so the files a run writes stay out of the repo:

```bash
mkdir -p scratch && cd scratch
bash ../services/checkout-api/tools/live_run.sh bad_deploy   # or db_pool_exhaustion, or healthy
```

A fault mode calls the controller's `break`, which also starts the 6-minute burst. `healthy` sends the burst alone, as a control. The script then:

1. prints each alarm transition, in seconds after the invoke, for 440 s;
2. sends `clear` for a fault;
3. leaves `t0_<mode>.txt`, the run's start time, for `analyze.py`.

It stops without clearing if the break is refused, for example because another fault is active. If you stop it early, the fault expires 10 minutes after the break, or clear it now:

```bash
echo '{"action": "clear"}' > ctl.json
AWS_PROFILE=sentinelops aws lambda invoke --function-name sentinelops-fault-controller \
  --cli-binary-format raw-in-base64-out --payload file://ctl.json out.json && cat out.json; rm -f ctl.json out.json
```

## Analyze it

```bash
python -I ../services/checkout-api/tools/analyze.py t0_bad_deploy.txt
```

It prints:

- the alarm transitions from CloudWatch's history;
- every 10-second period as the alarms see it;
- the burst's own summary and Lambda report;
- checkout-api's concurrency and throttles;
- the log lines by route, version, and error type;
- a scan for lines that would leak the answer.

Two things to know when reading it:

- An alarm changes state about 17 s after the end of the 10-second period that trips it, so the history runs that much behind the table.
- The log counts come up short until Logs Insights catches up, which can take minutes. The metrics are complete sooner. For settled counts, run `insights_queries.md` a few minutes later.

## What to expect

From the runs on 2026-10-27, in seconds after the click:

| Run | Alarms |
|---|---|
| `healthy` | none |
| `db_pool_exhaustion` | pool wait at 42, latency at 262, errors at 302 |
| `bad_deploy` | errors at 36, held until the burst ends. It can fire again before the clear, while sparse baseline failures continue |
