"""checkout-api's releases: the commit each one's code is, and where a fix for it goes.

The snapshot builder (tools/build_code_snapshot.py) reads each release's
checkout/ from its commit here, never from the working directory, so the read
tool shows a release's code as that release ran it. The remediation flow takes
a fix's base from here: a release branch, never main (BUILD_PLAN.md, Phase 4
design, decision 4).

These are the repo's own commits. Deploy history's commit IDs are its own and
stay as they are (decision 5): no read shows a commit.

- 1.42.0 is main's checkout/ as it runs, tagged v1.42.0. A test holds main's
  checkout/ to it: a change there is a new release, and needs an entry here.
- 1.43.0 is that commit plus one, "Simplify promo pricing", on release/1.43.0:
  pricing.py without the null check sim/releases.py removes at runtime.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    version: str
    commit: str  # the full SHA of the commit whose checkout/ is this release's code
    ref: str  # where GitHub keeps that commit: its tag, or its release branch
    branch: str | None  # the base a fix PR targets; None: no fix PRs (main is never a base)


RELEASES = {
    release.version: release
    for release in (
        Release("1.42.0", "3fb990f5c865fc3d638078df10d887994e9b249e", "refs/tags/v1.42.0", None),
        Release(
            "1.43.0", "ee9ef23a8d7eb51670b5c00e655a9d1814e0a640", "refs/heads/release/1.43.0", "release/1.43.0"
        ),
    )
}
