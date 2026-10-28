"""The alarm an investigation starts from, and the prompt it becomes."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_FIELD_CHARS = 1_000
_OPERATORS = {
    "GreaterThanThreshold": ">",
    "GreaterThanOrEqualToThreshold": ">=",
    "LessThanThreshold": "<",
    "LessThanOrEqualToThreshold": "<=",
}


class AlarmNotFiring(Exception):
    pass


@dataclass(frozen=True)
class AlarmEvent:
    name: str
    fired_at: datetime
    reason: str
    description: str
    condition: str

    @classmethod
    def live(cls, cloudwatch: Any, name: str) -> "AlarmEvent":
        """The alarm as it is now. Refuses unless it's in ALARM."""
        alarm = _describe(cloudwatch, name)
        if alarm["StateValue"] != "ALARM":
            raise AlarmNotFiring(f"{name} is {alarm['StateValue']}, not ALARM.")
        return cls._from(alarm, alarm["StateUpdatedTimestamp"], alarm.get("StateReason", ""))

    @classmethod
    def as_of(cls, cloudwatch: Any, name: str, moment: datetime) -> "AlarmEvent":
        """The alarm's last change of state at or before `moment`, which must be into ALARM.

        A replay starts from that change, as a live run would have.
        """
        alarm = _describe(cloudwatch, name)
        history = cloudwatch.describe_alarm_history(
            AlarmName=name,
            HistoryItemType="StateUpdate",
            StartDate=moment - timedelta(days=1),
            EndDate=moment,
            ScanBy="TimestampDescending",
            MaxRecords=1,
        )["AlarmHistoryItems"]
        if not history:
            raise AlarmNotFiring(f"{name} didn't change state in the day before {_iso(moment)}.")
        change = history[0]
        new_state = json.loads(change["HistoryData"])["newState"]
        if new_state["stateValue"] != "ALARM":
            raise AlarmNotFiring(
                f"{name} was {new_state['stateValue']} at {_iso(moment)}, not ALARM."
            )
        return cls._from(alarm, change["Timestamp"], new_state.get("stateReason", ""))

    @classmethod
    def _from(cls, alarm: dict[str, Any], fired_at: datetime, reason: str) -> "AlarmEvent":
        return cls(
            name=alarm["AlarmName"],
            fired_at=fired_at.astimezone(UTC),
            reason=reason,
            description=alarm.get("AlarmDescription", ""),
            condition=_condition(alarm),
        )

    def prompt(self) -> str:
        return "\n".join(
            [
                "An alarm fired on checkout-api.",
                "",
                f"Alarm: {self.name}",
                f"Fired at: {_iso(self.fired_at)}",
                f"Description: {_cut(self.description)}",
                f"Condition: {self.condition}",
                f"CloudWatch's reason: {_cut(self.reason)}",
                "",
                "Find out why it fired, and end with a Hypothesis.",
            ]
        )


def _describe(cloudwatch: Any, name: str) -> dict[str, Any]:
    alarms = cloudwatch.describe_alarms(AlarmNames=[name], AlarmTypes=["MetricAlarm"])["MetricAlarms"]
    if not alarms:
        raise AlarmNotFiring(f"No metric alarm named {name}.")
    return alarms[0]


def _condition(alarm: dict[str, Any]) -> str:
    stat = alarm.get("ExtendedStatistic") or alarm.get("Statistic", "")
    metric = f"{alarm.get('MetricName', 'metric math')} {stat}".strip()
    operator = _OPERATORS.get(alarm.get("ComparisonOperator", ""), alarm.get("ComparisonOperator", ""))
    periods = alarm.get("EvaluationPeriods", 1)
    needed = alarm.get("DatapointsToAlarm", periods)
    return (
        f"{metric} {operator} {alarm.get('Threshold')} over {alarm.get('Period')} s periods, "
        f"in {needed} of {periods} periods."
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _cut(text: str) -> str:
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + " ...[cut]"
