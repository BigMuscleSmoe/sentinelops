"""The sweep's arms: the agent's model, the caps a run and a sweep get, and the runs per alarm, per arm (R10).

The production cap stays the guard's default, $0.10 a run; an arm's caps apply
to its eval runs only. Each arm's price comes from pricing.PRICES, so an arm
whose model has no checked price can't run. An arm with `blocked` set can be
planned but refuses to make model calls, until what it names is done. A sweep
runs each alarm the arm's number of times unless --runs says otherwise.
"""

from dataclasses import dataclass, replace

from guards import Limits
from providers import Settings


@dataclass(frozen=True)
class Arm:
    name: str
    model_id: str
    per_run_usd: float  # the guard's cap on one run
    cap_usd: float  # the sweep's: a run starts only if this still has room for a whole run
    expected_run_usd: float  # for the plan's estimate only
    runs_per_scenario: int  # the runs each alarm gets, unless --runs says otherwise
    blocked: str | None = None

    def settings(self, base: Settings) -> Settings:
        return replace(base, model_id=self.model_id)


ARMS: dict[str, Arm] = {
    # The trial's median answered run was $0.0062 (2026-11-14), under the old sweep's mean.
    "haiku": Arm(
        "haiku", "claude-haiku-5-5", per_run_usd=Limits().max_cost_usd, cap_usd=1.00, expected_run_usd=0.0071, runs_per_scenario=3
    ),
    # The user's calls: $0.50 a run with 16,000 max output, $5.00 for the arm (2026-11-14);
    # 3 runs an alarm if the check run projects the 33-run test sweep at $4.50 or less, else 2
    # (2026-11-16), so the sweep never ends with a round cut short. The check run
    # (deploy-1113-0257, 2026-11-16) cost $0.2774 against the $0.129 the count_tokens
    # comparison predicted at Haiku's output: 2.15 times, projecting $7.74, so 2. The estimate
    # is the predicted mean ($3.60 for 33 runs) times the same 2.15.
    "sonnet": Arm(
        "sonnet", "claude-sonnet-5-5", per_run_usd=0.50, cap_usd=5.00, expected_run_usd=0.235, runs_per_scenario=2
    ),
}
DEFAULT_ARM = "haiku"
