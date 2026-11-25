"""The App can push branches and write files, so CI's triggers must give it no way to start a deploy.

Contents write lets the App send repository_dispatch, and a branch it pushes
starts any workflow that listens for pushes to that branch. So: one workflow,
triggered exactly by a push to main and by pull requests; deploy runs only on
the push; and the deploy role trusts only refs/heads/main (infra/bootstrap).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"


def lines() -> list[str]:
    return DEPLOY.read_text(encoding="utf-8").splitlines()


def test_deploy_yml_is_the_only_workflow() -> None:
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["deploy.yml"]


def test_its_triggers_are_exactly_a_push_to_main_and_pull_requests() -> None:
    text = lines()
    assert text.count("on:") == 1
    start = text.index("on:") + 1
    block = []
    for line in text[start:]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    assert block == ["  push:", "    branches: [main]", "  pull_request:"]


def test_nothing_else_can_start_it() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    for trigger in ("repository_dispatch", "workflow_dispatch", "workflow_run", "workflow_call", "pull_request_target",
                    "schedule", "issue_comment", "push:\n    tags", "branches-ignore"):
        assert trigger not in text, trigger


def test_the_deploy_job_runs_only_on_the_push() -> None:
    assert "    if: github.event_name == 'push' && vars.AWS_DEPLOY_ROLE_ARN != ''" in lines()


def test_remediation_has_its_own_test_leg() -> None:
    matrix = [line.strip() for line in lines() if line.strip().startswith("project: [")]
    assert matrix == ["project: [agent, core, tools, services/checkout-api, trigger, remediation]"]
