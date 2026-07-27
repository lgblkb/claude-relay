#!/usr/bin/env python3
"""Tail a live `claude-relay run` logfile and print ONE compact line per event worth acting on.

Reached via the checkout-only `bin/run-progress` shim (never a console entry point): this is an
operator observability tool for watching a run in progress, not part of the supervisor's contract.

    ./bin/run-progress                 # newest log under ~/.claude-relay/logs
    ./bin/run-progress <path.log>      # a specific run

`project()` is a pure function of one decoded envelope, so every projection rule below — including
the two that were wrong on the first real run — is unit-testable without a running generation
(tests/test_progress.py).

Why it exists: the log is NDJSON straight from `claude --output-format stream-json`, where a single
`assistant` envelope can be tens of KB. Watching a 64-minute, 17-agent generation with `tail -f` is
unreadable, and grepping raw lines drowns the two signals that matter (which agent is running, and
whether anything failed). This projects each envelope down to one short line and flushes per line so
a pipe or a monitor sees it immediately.

Two projection bugs are baked out here because both produced actively MISLEADING output during the
first real run (DESIGN.md §4b), and both would recur in any hand-rolled equivalent:

  * gad-run runs several agents CONCURRENTLY (e.g. `review:framing` alongside `review:combined`), so
    their `task_progress` events interleave. Reporting on "description changed since last event"
    turns that into an alternating A/B/A/B feed that reads exactly like a stuck loop. Report on
    FIRST SIGHTING of each label instead — hence a set, not a last-value.
  * not every `task_started` is a workflow. Agents also launch `local_bash` background tasks
    (filesystem searches, long greps), which show up with `workflow_name: null` and read as
    "WORKFLOW-START None" unless distinguished by `task_type`.

Failure coverage is deliberate: this emits on error signatures too, not just progress. A watcher that
matches only the happy path is silent through a crash, and silence is indistinguishable from "still
working".
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".claude-relay" / "logs"

# Substrings that mark a line as worth surfacing even when it is not a decodable envelope (a
# supervisor traceback, a plugin resolution failure) — the cases where the stream stops being NDJSON
# precisely because something broke.
_ERROR_MARKERS = ("Traceback", "Unknown command", "ECONNREFUSED", "FATAL", "not found")

# How long the stream may be silent, with the file no longer growing, before concluding the run is
# over. Generous: a single agent can think for minutes without emitting anything.
_IDLE_GIVEUP_S = 90.0


def newest_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _project_system(env: dict[str, Any], seen: set[str]) -> str | None:
    sub = env.get("subtype")
    if sub == "init":
        return f"init model={env.get('model')} cwd={env.get('cwd')}"
    if sub == "task_started":
        kind = env.get("task_type")
        label = env.get("workflow_name") or (env.get("description") or "")[:80]
        return f"TASK-START[{kind}] {label} task={env.get('task_id')}"
    if sub == "task_progress":
        label = env.get("description")
        if not label or label in seen:
            return None
        seen.add(label)
        usage = env.get("usage") or {}
        phases = [
            p.get("title")
            for p in (env.get("workflow_progress") or [])
            if isinstance(p, dict) and p.get("type") == "workflow_phase"
        ]
        return (
            f"AGENT[{len(seen)}] {label} "
            f"tokens={usage.get('total_tokens')} tools={usage.get('tool_uses')} "
            f"plan={'>'.join(t for t in phases if t)[:120]}"
        )
    # hook_started / hook_response / thinking_tokens / background_tasks_changed carry nothing an
    # operator would act on.
    return None


def _project_assistant(env: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for block in env.get("message", {}).get("content") or []:
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name")
            inp = block.get("input") or {}
            extra = ""
            if name == "Workflow":
                extra = f" script={Path(str(inp.get('scriptPath', '?'))).name}"
            elif name == "TaskOutput":
                extra = f" block={inp.get('block')}"
            parts.append(f"tool:{name}{extra}")
        elif btype == "text":
            text = (block.get("text") or "").strip()
            if not text:
                continue
            first = text.splitlines()[0][:160]
            marker = "TEXT-ERROR" if any(m in text for m in _ERROR_MARKERS) else "text"
            parts.append(f"{marker}: {first}")
    return " | ".join(parts) if parts else None


def project(env: dict[str, Any], seen: set[str]) -> str | None:
    """One decoded envelope -> one short operator-facing line, or None to stay silent.

    `seen` accumulates already-reported agent labels and is MUTATED here. It is a parameter rather
    than module state so the caller owns the run's identity and tests need no reset hook.
    """
    kind = env.get("type")

    if kind == "system":
        return _project_system(env, seen)

    if kind == "rate_limit_event":
        info = env.get("rate_limit_info") or {}
        util = info.get("utilization")
        return (
            f"RATE_LIMIT {info.get('rateLimitType')} status={info.get('status')} "
            f"utilization={util if util is not None else '-'}"
        )

    if kind == "result":
        models = ",".join(sorted(env.get("modelUsage") or {})) or "-"
        return (
            f"RESULT subtype={env.get('subtype')} is_error={env.get('is_error')} "
            f"stop_reason={env.get('stop_reason')} api_error={env.get('api_error_status')} "
            f"turns={env.get('num_turns')} cost=${env.get('total_cost_usd')} "
            f"dur={round((env.get('duration_ms') or 0) / 1000)}s models={models}"
        )

    if kind == "assistant":
        return _project_assistant(env)

    if kind == "user":
        # Tool results arrive as `user` envelopes; surface only failures — a successful result is
        # noise, but a failed one is where a workflow death becomes visible.
        for block in env.get("message", {}).get("content") or []:
            if block.get("type") == "tool_result" and block.get("is_error"):
                return f"TOOL-ERROR: {json.dumps(block.get('content'))[:200]}"
        return None

    return None


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path: Path | None = Path(argv[1]).expanduser()
        if path is not None and not path.is_file():
            print(f"no such logfile: {path}", flush=True)
            return 2
    else:
        path = newest_log()
        if path is None:
            print(f"no logs under {LOG_DIR}", flush=True)
            return 1

    print(f"tailing {path.name}", flush=True)
    seen: set[str] = set()
    last_size = 0
    idle_since = time.monotonic()
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            line = fh.readline()
            if not line:
                if time.monotonic() - idle_since > _IDLE_GIVEUP_S:
                    size = path.stat().st_size
                    if size == last_size:
                        print("STREAM-IDLE: no output for 90s, file size stable", flush=True)
                        return 0
                    last_size = size
                    idle_since = time.monotonic()
                time.sleep(1)
                continue

            idle_since = time.monotonic()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                env = json.loads(stripped)
            except json.JSONDecodeError:
                if any(m in stripped for m in _ERROR_MARKERS):
                    print(f"NON-JSON-ERROR: {stripped[:200]}", flush=True)
                continue
            if not isinstance(env, dict):
                continue
            signal = project(env, seen)
            if signal:
                print(signal, flush=True)
            if env.get("type") == "result":  # terminal envelope: the run is done
                return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
