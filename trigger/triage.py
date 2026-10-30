"""Triage: whether one alarm's change of state starts an investigation. Code, not
a model: a few comparisons of names and times, deterministic and free, and no
alarm text can steer it.

Each service has one incident at a time. An alarm going into ALARM with no
incident open is NEW: it opens one, and only a NEW change may start an
investigation. Another alarm going into ALARM during an open incident is a
DUPLICATE, and one that has already been in ALARM during it is FLAPPING: both
are recorded, and neither starts a run. Changes back to OK are recorded too,
because they close incidents: an incident closes once every alarm has been OK
for 10 minutes, or 60 minutes after it opened, whichever comes first.

So a fault's later alarms (the pool fault's latency and errors) and its
re-fires (bad_deploy's errors) never start a second run, and two breaks need
10 quiet minutes between them, or the second joins the first.

Every time here is the alarm's own time of change, from its event, so a late
delivery is judged as of when the change happened. A change already seen, or
one older than the latest seen for its alarm, is SEEN and ignored: EventBridge
and Lambda each deliver at least once.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

ALARM, OK = "ALARM", "OK"
QUIET = timedelta(minutes=10)
MAX_OPEN = timedelta(minutes=60)
# The alarms the trigger acts on, by name prefix, and the service each watches.
SERVICES = {"sentinelops-checkout-": "checkout-api"}


class Verdict(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    FLAPPING = "flapping"
    OK = "ok"
    SEEN = "seen"


@dataclass(frozen=True)
class Change:
    """One alarm's change of state: into ALARM, or back to OK."""

    alarm: str
    state: str
    at: datetime


@dataclass(frozen=True)
class Incident:
    id: str
    opened_at: datetime
    opened_by: str
    # Every alarm that has gone into ALARM during it, in order.
    alarmed: tuple[str, ...]


@dataclass(frozen=True)
class ServiceState:
    """What triage knows about one service between changes."""

    # Each alarm's latest change seen: its state and when.
    alarms: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    # The latest incident, open or closed.
    incident: Incident | None = None
    # Since when every alarm has been OK; None while one is in ALARM.
    quiet_since: datetime | None = None

    def open_incident(self, at: datetime) -> Incident | None:
        """The incident still open at `at`, if any."""
        incident = self.incident
        if incident is None or at - incident.opened_at >= MAX_OPEN:
            return None
        if self.quiet_since is not None and at - self.quiet_since >= QUIET:
            return None
        return incident

    def to_dict(self) -> dict[str, Any]:
        incident = self.incident
        return {
            "alarms": {name: {"state": s, "at": iso(at)} for name, (s, at) in sorted(self.alarms.items())},
            "incident": None
            if incident is None
            else {
                "id": incident.id,
                "opened_at": iso(incident.opened_at),
                "opened_by": incident.opened_by,
                "alarmed": list(incident.alarmed),
            },
            "quiet_since": None if self.quiet_since is None else iso(self.quiet_since),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceState":
        incident = data["incident"]
        return cls(
            alarms={name: (a["state"], parse_time(a["at"])) for name, a in data["alarms"].items()},
            incident=None
            if incident is None
            else Incident(
                id=incident["id"],
                opened_at=parse_time(incident["opened_at"]),
                opened_by=incident["opened_by"],
                alarmed=tuple(incident["alarmed"]),
            ),
            quiet_since=None if data["quiet_since"] is None else parse_time(data["quiet_since"]),
        )


@dataclass(frozen=True)
class Triage:
    verdict: Verdict
    # The service's state after the change.
    state: ServiceState
    # The open incident the change belongs to; for NEW, the one it opened.
    incident: Incident | None


def classify(state: ServiceState, change: Change, service: str) -> Triage:
    """Judge one change against the service's state as of the change's own time."""
    seen = state.alarms.get(change.alarm)
    if seen is not None and change.at <= seen[1]:
        return Triage(Verdict.SEEN, state, None)
    alarms = {**state.alarms, change.alarm: (change.state, change.at)}
    incident = state.open_incident(change.at)

    if change.state == OK:
        if any(s == ALARM for s, _ in alarms.values()):
            quiet_since = None
        else:
            quiet_since = state.quiet_since or change.at
        return Triage(Verdict.OK, replace(state, alarms=alarms, quiet_since=quiet_since), incident)

    if incident is None:
        incident = Incident(incident_id(service, change.at), change.at, change.alarm, (change.alarm,))
        return Triage(Verdict.NEW, ServiceState(alarms, incident), incident)
    if change.alarm in incident.alarmed:
        verdict = Verdict.FLAPPING
    else:
        verdict = Verdict.DUPLICATE
        incident = replace(incident, alarmed=(*incident.alarmed, change.alarm))
    return Triage(verdict, ServiceState(alarms, incident), incident)


def service_for(alarm: str) -> str | None:
    """The service an alarm watches, or None for an alarm that isn't ours."""
    for prefix, service in SERVICES.items():
        if alarm.startswith(prefix):
            return service
    return None


def incident_id(service: str, opened_at: datetime) -> str:
    """Unique per service: an incident lasts at least 10 minutes."""
    return f"{service}-{opened_at.astimezone(UTC):%Y%m%dT%H%M%S}Z"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    """An ISO 8601 time with its offset (CloudWatch writes +0000), in UTC. ValueError otherwise."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{value} has no time zone")
    return moment.astimezone(UTC)
