from datetime import UTC, datetime, timedelta

from conftest import Stubbed

from sentinelops_tools.metrics import COLUMNS, metrics_table


def rows(text: str) -> dict[str, list[str]]:
    """The table's rows by time, each cell a column."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip().startswith("time"))
    header = lines[start].split()
    table = {}
    for line in lines[start + 1 :]:
        cells = line.split()
        if len(cells) != len(header):
            break
        table[cells[0]] = cells
    return table


def test_the_bad_deploy_shows_in_the_period_the_alarm_fired_in(recorded) -> None:
    with recorded("metrics_bad_deploy") as (env, fixture):
        text = metrics_table(env)

    # Recorded after CloudWatch dropped the 10-second data: 60 s periods, said so.
    assert "60 s periods" in text
    assert "10-second data is kept for 3 hours" in text
    # The query ends on the next minute, so the minute the alarm fired in is a row.
    query = fixture["calls"][0]["params"]
    assert query["EndTime"] == datetime(2026, 10, 27, 19, 6, tzinfo=UTC)
    table = rows(text)
    assert table["19:05:00*"][1:3] == ["276", "10.1"]  # requests, err%
    assert table["19:04:00"][1:3] == ["12", "0.0"]
    assert "* still filling in" in text


def test_the_pool_fault_shows_pool_wait_before_any_error(recorded) -> None:
    with recorded("metrics_pool") as (env, _):
        text = metrics_table(env)

    newest = rows(text)["18:56:00*"]
    header = ["time"] + [c.header for c in COLUMNS]
    cell = dict(zip(header, newest))
    assert cell["pool_wait_p99_ms"] == "585"
    assert cell["pool_active_max"] == "2"
    assert cell["err%"] == "0.0"
    assert cell["db_query_p50_ms"] == "66.7"  # the database itself is not slow


def test_a_live_window_reads_10_second_periods() -> None:
    now = datetime(2026, 10, 28, 4, 0, 47, tzinfo=UTC)
    stubbed = Stubbed(now, wall_clock=now + timedelta(seconds=2))
    start = datetime(2026, 10, 28, 3, 59, 40, tzinfo=UTC)
    times = [start + timedelta(seconds=10 * i) for i in range(7)]  # 03:59:40 to 04:00:40
    results = [
        {"Id": c.id, "Timestamps": times, "Values": [1.0] * len(times), "StatusCode": "Complete"}
        for c in COLUMNS
    ]
    stubbed.expect(
        "cloudwatch",
        "get_metric_data",
        {
            "MetricDataQueries": [
                {
                    "Id": c.id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "checkout-api",
                            "MetricName": c.metric,
                            "Dimensions": (
                                [{"Name": "dependency", "Value": c.dependency}] if c.dependency else []
                            ),
                        },
                        "Period": 10,
                        "Stat": c.stat,
                    },
                }
                for c in COLUMNS
            ],
            "StartTime": start,
            "EndTime": datetime(2026, 10, 28, 4, 0, 50, tzinfo=UTC),
            "ScanBy": "TimestampAscending",
        },
        {"MetricDataResults": results},
    )
    with stubbed:
        text = metrics_table(stubbed.env, minutes=1)

    assert "10 s periods" in text
    marked = [t for t in rows(text) if t.endswith("*")]
    # Periods that end within 20 s of now may still be filling in.
    assert marked == ["04:00:20*", "04:00:30*", "04:00:40*"]


def test_a_long_window_is_cut_to_90_rows() -> None:
    now = datetime(2026, 10, 28, 4, 0, 0, tzinfo=UTC)
    stubbed = Stubbed(now, wall_clock=now)
    stubbed.stubbers["cloudwatch"].add_response(
        "get_metric_data", {"MetricDataResults": []}, None
    )
    with stubbed:
        text = metrics_table(stubbed.env, minutes=60, period_seconds=10)

    assert text.startswith("No checkout-api metrics for 03:45:00 to 04:00:00 UTC")


def test_an_unsupported_period_is_refused_without_a_call() -> None:
    stubbed = Stubbed(datetime(2026, 10, 28, 4, 0, tzinfo=UTC))
    with stubbed:
        assert metrics_table(stubbed.env, period_seconds=30) == "period_seconds must be 10 or 60."


def test_an_aws_error_reaches_the_model_as_its_code_only() -> None:
    stubbed = Stubbed(datetime(2026, 10, 28, 4, 0, tzinfo=UTC))
    stubbed.stubbers["cloudwatch"].add_client_error(
        "get_metric_data",
        service_error_code="AccessDeniedException",
        service_message="User: arn:aws:iam::123456789012:user/someone is not authorized",
    )
    with stubbed:
        text = metrics_table(stubbed.env)

    assert text == "Could not read the metrics: AWS returned AccessDeniedException."
