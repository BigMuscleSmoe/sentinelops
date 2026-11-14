"""The labeling packet: what a run saw and what it answered, with the IDs labels point at, and nothing else.

Each run gets its alarm prompt, every tool call with its input and its output
verbatim (as the agent read them, source labels and line numbers included), and
its answer with every target numbered: root_cause by sentence (s1, s2, ...),
evidence items (e1, ...), and inferences (i1, ...). Left out: thinking, the
model's own text between calls, the answer tool's calls and acknowledgements,
costs, and anything the checker or the rubric says. The same packet serves the
blind audit (every run) and the user's calibration labels (R8: 3 runs).
"""

import json
from typing import Any

from evals.audit import sentences

FENCE = "````"  # four backticks, so a tool output's own ``` can't close it


def tool_calls(transcript: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """(name, input, output text) for each tool call but the answer's, in order."""
    calls: dict[str, tuple[str, Any]] = {}
    out = []
    for message in transcript.get("messages", []):
        for block in message.get("content", []):
            if "toolUse" in block:
                use = block["toolUse"]
                calls[use["toolUseId"]] = (use["name"], use.get("input"))
            elif "toolResult" in block:
                result = block["toolResult"]
                name, given = calls.get(result["toolUseId"], ("?", None))
                if name == "Hypothesis":
                    continue
                text = "\n".join(part["text"] for part in result.get("content", []) if "text" in part)
                out.append((name, given, text))
    return out


def prompt(transcript: dict[str, Any]) -> str:
    """The conversation's first message, the alarm; a transcript cut to its tool results has none."""
    messages = transcript.get("messages") or []
    if messages and messages[0].get("role") == "user":
        texts = [block["text"] for block in messages[0].get("content", []) if "text" in block]
        if texts:
            return "\n".join(texts)
    return "(no prompt kept)"


def shown(item: dict[str, Any]) -> dict[str, Any]:
    """An evidence item as the model stated it: the strict schema's unset fields (null) left out."""
    return {k: v for k, v in item.items() if v is not None}


def render_run(run: str, transcript: dict[str, Any]) -> str:
    lines = [f"## {run}", "", "### The alarm (the agent's prompt)", "", FENCE + "text", prompt(transcript), FENCE, ""]
    lines += ["### Tool calls", ""]
    for n, (name, given, text) in enumerate(tool_calls(transcript), 1):
        asked = "(input not kept)" if given is None else json.dumps(given, ensure_ascii=False)
        lines += [f"#### {n}. {name} {asked}", "", FENCE + "text", text, FENCE, ""]
    lines += ["### The answer", ""]
    hypothesis = transcript.get("hypothesis")
    if not hypothesis:
        return "\n".join(lines + ["No answer.", ""])
    if hypothesis.get("halted_by"):
        return "\n".join(lines + ["The run was halted by a limit: no claims to label.", ""])
    lines += [
        f"- confidence: {hypothesis.get('confidence')}",
        f"- affected_component: {hypothesis.get('affected_component')}",
        f"- fault_location: {hypothesis.get('fault_location')}",
        "",
        "root_cause, by sentence:",
        "",
    ]
    lines += [f"- [s{n}] {s}" for n, s in enumerate(sentences(hypothesis.get("root_cause") or ""), 1)]
    lines += ["", "evidence:", ""]
    lines += [f"- [e{n}] `{json.dumps(shown(item), ensure_ascii=False)}`" for n, item in enumerate(hypothesis.get("evidence") or [], 1)]
    lines += ["", "inferences:", ""]
    lines += [
        f"- [i{n}] {inf.get('claim')} (rests on {', '.join(inf.get('rests_on') or [])})"
        for n, inf in enumerate(hypothesis.get("inferences") or [], 1)
    ] or ["- none"]
    lines += ["", "not_checked:", ""]
    lines += [f"- {text}" for text in hypothesis.get("not_checked") or []] or ["- none"]
    return "\n".join(lines + [""])


def render(sweep: str, runs: dict[str, dict[str, Any]], guide: str) -> str:
    head = [f"# Labeling packet: sweep {sweep}", "", guide, "", f"Runs: {', '.join(runs)}", ""]
    return "\n".join(head) + "\n" + "\n".join(render_run(run, t) for run, t in runs.items())
