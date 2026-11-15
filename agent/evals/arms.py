"""The sweep's arms: the agent's model, and the caps a run and a sweep get, per arm (R10).

The production cap stays the guard's default, $0.10 a run; an arm's caps apply
to its eval runs only. Each arm's price comes from pricing.PRICES, so an arm
whose model has no checked price can't run. An arm with `blocked` set can be
planned but refuses to make model calls, until what it names is done.
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
    blocked: str | None = None

    def settings(self, base: Settings) -> Settings:
        return replace(base, model_id=self.model_id)


ARMS: dict[str, Arm] = {
    # The trial's median answered run was $0.0062 (2026-11-14), under the old sweep's mean.
    "haiku": Arm("haiku", "claude-haiku-5-5", per_run_usd=Limits().max_cost_usd, cap_usd=1.00, expected_run_usd=0.0071),
    # The user's call, 2026-11-14: $0.50 a run with 16,000 max output, $5.00 for the arm.
    # The estimate is 2026-11-13's, at Haiku's token counts ($0.023 to $0.231).
    "sonnet": Arm(
        "sonnet",
        "claude-sonnet-5-5",
        per_run_usd=0.50,
        cap_usd=5.00,
        expected_run_usd=0.108,
        blocked=(
            "the redesign's step 9 comes first: its native-JSON answer path given the main path's resends, "
            "a free count_tokens comparison, and one paid check run"
        ),
    ),
}
DEFAULT_ARM = "haiku"
