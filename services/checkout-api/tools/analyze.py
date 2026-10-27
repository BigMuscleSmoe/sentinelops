"""After a live run: alarm history, 10 s metrics, the burst's summary, throttling, log lines.

usage: python -I analyze.py <t0 file> [horizon_s]
The t0 file is the one live_run.sh wrote. Uses the AWS CLI (profile sentinelops)
and prints nothing account-specific. Logs Insights lags behind ingestion under
load, so right after a run its counts can come up short while the metrics are
already complete; insights_queries.md has the queries for settled counts.
"""

import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

env = {**os.environ, "AWS_PROFILE": "sentinelops", "AWS_PAGER": "", "MSYS_NO_PATHCONV": "1", "AWS_REGION": "us-east-1"}
t0 = float(re.search(r"T0=([0-9.]+)", open(sys.argv[1], encoding="utf-8").read()).group(1))
horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 460
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
start, end = math.floor((t0 - 60) / 10) * 10, math.ceil((t0 + horizon) / 10) * 10


def aws(*args):
    out = subprocess.run(["aws", *args, "--output", "json"], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout) if out.stdout.strip() else {}


print(f"T0 {datetime.fromtimestamp(t0).strftime('%H:%M:%S.%f')[:-3]} local")

# 1. Alarm history: the authoritative transition times.
print("\n== alarm transitions (CloudWatch history), seconds after T0")
for name in ("pool-wait", "errors", "latency"):
    items = aws("cloudwatch", "describe-alarm-history", "--alarm-name", f"sentinelops-checkout-{name}",
                "--history-item-type", "StateUpdate", "--start-date", iso(t0 - 5), "--end-date", iso(t0 + horizon + 60))
    rows = sorted(items.get("AlarmHistoryItems", []), key=lambda i: i["Timestamp"])
    out = []
    for item in rows:
        at = datetime.fromisoformat(item["Timestamp"]).timestamp()
        new = json.loads(item["HistoryData"])["newState"]["stateValue"]
        out.append(f"{new} +{at - t0:.1f}")
    print(f"  {name:10s}", ", ".join(out) or "no transitions")

# 2. 10 s metrics, as the alarms see them.
stats = {
    "n": ("latency_ms", "SampleCount"), "wait": ("pool_wait_time_ms", "p99"), "lat": ("latency_ms", "p99"),
    "err": ("error", "Average"), "active": ("pool_active_connections", "Maximum"),
    "conc": ("concurrent_requests", "Maximum"), "query": ("db_query_ms", "p50"),
}
queries = [{"Id": key, "MetricStat": {"Metric": {"Namespace": "checkout-api", "MetricName": metric}, "Period": 10, "Stat": stat}}
           for key, (metric, stat) in stats.items()]
with open("mdq.json", "w", encoding="utf-8") as f:
    json.dump(queries, f)
data = aws("cloudwatch", "get-metric-data", "--metric-data-queries", "file://mdq.json", "--start-time", iso(start), "--end-time", iso(end))
series = {r["Id"]: {datetime.fromisoformat(t).timestamp(): v for t, v in zip(r["Timestamps"], r["Values"])} for r in data["MetricDataResults"]}
thresholds = {"wait": 100.0, "err": 0.05, "lat": 1500.0}
print("\n== 10 s periods (start relative to T0); * = breaching")
print("     t     n  wait_p99  lat_p99   err%  active  conc  query_p50")
breaching = {k: [] for k in thresholds}
first = {}
for p in range(start, end, 10):
    n = series["n"].get(p)
    for key, limit in thresholds.items():
        value = series[key].get(p)
        breaching[key].append(value is not None and value > limit)
        if key not in first and sum(breaching[key][-3:]) >= 2:
            first[key] = p + 10 - t0
    if n is None:
        continue
    mark = lambda key: "*" if breaching[key][-1] else " "
    print(f"{p - t0:6.0f} {n:5.0f} {series['wait'].get(p, 0):8.1f}{mark('wait')} {series['lat'].get(p, 0):7.0f}{mark('lat')} "
          f"{100 * series['err'].get(p, 0):5.1f}{mark('err')} {series['active'].get(p, 0):6.0f} {series['conc'].get(p, 0):5.0f} "
          f"{series['query'].get(p, 0):9.1f}")
# The alarm history lags these times by ~17 s (measured 2026-10-27).
print("2 of 3 first breaching at (s after T0, the end of the deciding period):",
      {k: round(v, 1) for k, v in first.items()} or "never")

# 3. The burst's own summary line.
print("\n== burst summary")
events = aws("logs", "filter-log-events", "--log-group-name", "/aws/lambda/sentinelops-traffic-burst",
             "--start-time", str(int((t0 - 5) * 1000)), "--end-time", str(int((t0 + horizon + 120) * 1000)),
             "--filter-pattern", '"burst sent"')
for event in events.get("events", []):
    line = json.loads(event["message"])
    print(f"  logged +{event['timestamp'] / 1000 - t0:.1f}s:", json.dumps({k: line[k] for k in line if k not in ("time", "level", "type")}))
reports = aws("logs", "filter-log-events", "--log-group-name", "/aws/lambda/sentinelops-traffic-burst",
              "--start-time", str(int((t0 - 5) * 1000)), "--end-time", str(int((t0 + horizon + 120) * 1000)),
              "--filter-pattern", '"platform.report"')
for event in reports.get("events", []):
    record = json.loads(event["message"]).get("record", {})
    metrics = record.get("metrics", {})
    print("  report:", {k: metrics.get(k) for k in ("durationMs", "maxMemoryUsedMB", "initDurationMs")}, record.get("status"))
if not events.get("events"):
    print("  (no summary line yet)")

# 4. Lambda concurrency, throttles, and errors, at 1 minute.
print("\n== 1 minute: checkout-api concurrency and throttles; burst throttles and errors")
lq = []
for i, (ns, metric, stat, dims) in enumerate([
    ("AWS/Lambda", "ConcurrentExecutions", "Maximum", [{"Name": "FunctionName", "Value": "sentinelops-checkout-api"}]),
    ("AWS/Lambda", "Throttles", "Sum", [{"Name": "FunctionName", "Value": "sentinelops-checkout-api"}]),
    ("AWS/Lambda", "Throttles", "Sum", [{"Name": "FunctionName", "Value": "sentinelops-traffic-burst"}]),
    ("AWS/Lambda", "Errors", "Sum", [{"Name": "FunctionName", "Value": "sentinelops-traffic-burst"}]),
]):
    lq.append({"Id": f"m{i}", "MetricStat": {"Metric": {"Namespace": ns, "MetricName": metric, "Dimensions": dims}, "Period": 60, "Stat": stat}})
with open("mdq.json", "w", encoding="utf-8") as f:
    json.dump(lq, f)
data = aws("cloudwatch", "get-metric-data", "--metric-data-queries", "file://mdq.json",
           "--start-time", iso(math.floor(t0 / 60) * 60 - 60), "--end-time", iso(end + 60))
labels = ["checkout concurrency max", "checkout throttles", "burst throttles", "burst errors"]
for label, r in zip(labels, data["MetricDataResults"]):
    values = r["Values"]
    print(f"  {label:26s} max/min {max(values, default=0):.0f}/{min(values, default=0):.0f}  sum {sum(values):.0f}  ({len(values)} minutes)")
os.remove("mdq.json")


# 5. What checkout-api logged during the run, and anything that would leak the answer.
def insights(query):
    qid = aws("logs", "start-query", "--log-group-name", "/aws/lambda/sentinelops-checkout-api",
              "--start-time", str(int(t0)), "--end-time", str(int(t0 + horizon)), "--query-string", query)["queryId"]
    for _ in range(40):
        result = aws("logs", "get-query-results", "--query-id", qid)
        if result["status"] == "Complete":
            return [{c["field"]: c["value"] for c in row if not c["field"].startswith("@")} for row in result["results"]]
        time.sleep(1.5)
    return [{"error": "query did not finish"}]


print("\n== checkout-api lines in the run, by route, version, and error type (short until Logs Insights catches up)")
for row in insights("filter ispresent(route) | stats count() as n by route, version, error_type, status | sort route, status"):
    print("  ", row)
print("\n== lines that would leak the answer (expect none)")
leaks = insights(r"filter @message like /sim\/|Traceback|stackTrace|pool ledger|config refresh|override|bad_deploy|db_pool_exhaustion|releases/ | stats count() as n by level")
print("  ", leaks or "none")
