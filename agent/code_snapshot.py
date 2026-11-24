"""checkout-api's code by release, for runs whose tools run here: a sweep's, or main.py's without Gateway.

Through Gateway, the read tool's Lambda ships the snapshot the deploy built
(tools/build_code_snapshot.py). A run here builds the same snapshot in memory,
with that same builder, from each release's commit in this clone
(sentinelops_core.releases), never from the working directory. So it can't be
stale, and its digest is the SHA-256 the deploy's build prints for the same
commits. A release commit missing from the clone, or a file the builder
refuses, refuses the run before any model call. It calls git, never AWS or a
model.
"""

import hashlib
import subprocess
from dataclasses import dataclass

from knowledge_base import script
from sentinelops_tools.repo import Snapshot, parse


class CodeSnapshotError(Exception):
    """The snapshot can't be built from this clone. The message says why."""


@dataclass(frozen=True)
class CodeSnapshot:
    files: Snapshot  # release -> path -> the file's text, as the read tool takes it
    digest: str  # the SHA-256 of the snapshot as the deploy stages it


def build() -> CodeSnapshot:
    """Every release's code from its commit, as the read tool's function ships it; CodeSnapshotError otherwise."""
    builder = script("build_code_snapshot")
    try:
        document = builder.build_snapshot()
    except builder.BuildError as e:
        raise CodeSnapshotError(f"checkout-api's code snapshot can't be built from this clone:\n{e}") from None
    except (OSError, subprocess.CalledProcessError) as e:
        raise CodeSnapshotError(f"checkout-api's code snapshot can't be built: git failed ({type(e).__name__})") from None
    return CodeSnapshot(parse(document), hashlib.sha256(builder.snapshot_bytes(document)).hexdigest())
