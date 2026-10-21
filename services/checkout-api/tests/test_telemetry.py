import json

from checkout.telemetry import NAMESPACE, log_line

AT = 1_800_000_000.123


def parse(line: str) -> dict:
    assert "\n" not in line and line.isascii()
    return json.loads(line)


def metric_names(directive: dict) -> list[str]:
    return [metric["Name"] for metric in directive["Metrics"]]


def test_a_line_declares_only_the_metrics_it_carries():
    record = parse(log_line({"route": "GET /products/{id}", "error": 0, "latency_ms": 61.2, "pool_wait_time_ms": 0.0}, AT))
    (directive,) = record["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == NAMESPACE == "checkout-api"
    assert directive["Dimensions"] == [[]]
    assert metric_names(directive) == ["error", "latency_ms", "pool_wait_time_ms"]
    assert all(metric["StorageResolution"] == 1 for metric in directive["Metrics"])
    assert record["latency_ms"] == 61.2  # EMF reads the values from the line's own fields


def test_upstream_metrics_go_under_the_dependency_dimension():
    fields = {"error": 0, "dependency": "payments-api", "upstream_latency_ms": 84.0, "upstream_timeout": 0}
    service, upstream = parse(log_line(fields, AT))["_aws"]["CloudWatchMetrics"]
    assert metric_names(service) == ["error"]
    assert upstream["Dimensions"] == [["dependency"]]
    assert metric_names(upstream) == ["upstream_latency_ms", "upstream_timeout"]


def test_no_metrics_block_when_there_are_none_or_none_are_wanted():
    assert "_aws" not in parse(log_line({"route": "GET /health", "status": 200}, AT))
    assert "_aws" not in parse(log_line({"latency_ms": 1.0}, AT, metrics=False))


def test_timestamps_come_first_and_unset_fields_are_left_out():
    record = parse(log_line({"status": 200, "error_type": None, "latency_ms": 5.0}, AT))
    assert list(record)[0] == "timestamp"
    assert record["timestamp"] == "2027-01-15T08:00:00.123Z"
    assert record["_aws"]["Timestamp"] == 1_800_000_000_123
    assert "error_type" not in record
