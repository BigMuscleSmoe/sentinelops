"""The checker's fixture: the three trials' 26 answered runs, and each item's verdict pinned.

runs.json.gz holds, per run, the alarm, the answer, its trims, the eval's
identity, and of the messages only the tool calls' names and the tool results'
text: no thinking, no model text, no costs. Built from the trials' transcripts
(sweeps 20261114T055051Z, 20261114T183031Z, 20261114T190302Z, Haiku 5.5 on the
recorded windows); trial 1's halted run is left out, having no claims.

verdicts.json pins every item's verdict and carries a hand note on each flagged
one, read against its cited result.
"""

import gzip
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "checker"
RUNS: dict[str, dict[str, Any]] = json.loads(gzip.decompress((FIXTURES / "runs.json.gz").read_bytes()))["runs"]
PINNED: dict[str, Any] = json.loads((FIXTURES / "verdicts.json").read_text(encoding="utf-8"))
