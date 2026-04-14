from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


NOISY_EVENT_TYPES = {
    "tty_output_chunk",
    "tty_input_chunk",
    "assistant_text_delta",
    "assistant_reasoning_delta",
    "codex_token_count",
}

DEFAULT_INCLUDED_EVENT_TYPES = {
    "session_started",
    "session_ended",
    "user_input",
    "request_user_message",
    "assistant_text_final",
    "assistant_reasoning_final",
    "tool_call_requested",
    "tool_call_dispatched",
    "tool_call_stdout",
    "tool_call_stderr",
    "tool_call_result",
    "tool_call_error",
    "subagent_spawn_requested",
    "subagent_spawned",
    "subagent_message",
    "subagent_result",
    "subagent_resumed",
    "subagent_closed",
    "final_output",
    "codex_rollout_imported",
    "codex_turn_context",
    "codex_task_started",
    "codex_task_complete",
    "codex_turn_aborted",
    "codex_context_compacted",
}

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEDUPE_BY_RENDERED_LINE_EVENT_TYPES = {
    "request_user_message",
    "assistant_reasoning_final",
}


def load_manifest(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.jsonl"
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def resolve_session_dir(
    *,
    root: Path,
    session_id: str | None = None,
    session_dir: Path | None = None,
    latest: bool = False,
) -> Path:
    if session_dir is not None:
        if not session_dir.exists():
            raise FileNotFoundError(f"Session directory not found: {session_dir}")
        return session_dir

    sessions_root = root / "sessions"
    if not sessions_root.exists():
        raise FileNotFoundError(f"Sessions root not found: {sessions_root}")

    if session_id:
        resolved = sessions_root / session_id
        if not resolved.exists():
            raise FileNotFoundError(f"Session directory not found: {resolved}")
        return resolved
    if latest:
        sessions = sorted(
            (path for path in sessions_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not sessions:
            raise FileNotFoundError(f"No sessions found under {sessions_root}")
        return sessions[0]
    raise ValueError("render requires --session-id, --session-dir, or --latest")


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truncate(text: str, limit: int = 240) -> str:
    compact = " ".join(_ANSI_ESCAPE_RE.sub("", text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _format_json(value: Any, *, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return _truncate(text, limit=limit)


def _event_line(event: dict[str, Any]) -> str | None:
    event_type = event.get("event_type")
    content = event.get("content", {})
    if not isinstance(content, dict):
        content = {}

    if event_type in {"user_input", "request_user_message"}:
        return f"User: {_truncate(_first_nonempty(content.get('text')))}"
    if event_type == "assistant_text_final":
        phase = content.get("phase")
        prefix = "Assistant"
        if isinstance(phase, str) and phase:
            prefix = f"Assistant[{phase}]"
        return f"{prefix}: {_truncate(_first_nonempty(content.get('text')))}"
    if event_type == "final_output":
        return f"Final: {_truncate(_first_nonempty(content.get('text')))}"
    if event_type == "assistant_reasoning_final":
        text = _first_nonempty(content.get("text"), content.get("summary_text"))
        if content.get("has_encrypted_content") and not text:
            text = "[encrypted reasoning content present]"
        if not text:
            return None
        return f"Reasoning: {_truncate(text)}"
    if event_type == "tool_call_requested":
        return (
            "Tool requested: "
            f"{content.get('tool_name') or '?'} "
            f"{_format_json(content.get('arguments'))}"
        )
    if event_type == "tool_call_dispatched":
        return (
            "Tool dispatched: "
            f"{content.get('tool_name') or '?'} "
            f"{_format_json(content.get('arguments'))}"
        )
    if event_type == "tool_call_stdout":
        return (
            "Tool stdout: "
            f"{content.get('tool_name') or '?'} "
            f"{_truncate(_first_nonempty(content.get('text')))}"
        )
    if event_type == "tool_call_stderr":
        return (
            "Tool stderr: "
            f"{content.get('tool_name') or '?'} "
            f"{_truncate(_first_nonempty(content.get('text')))}"
        )
    if event_type == "tool_call_result":
        output = _first_nonempty(content.get("output"))
        if not output:
            output = _format_json(content.get("result") or content)
        return f"Tool result: {content.get('tool_name') or '?'} {_truncate(output)}"
    if event_type == "tool_call_error":
        return f"Tool error: {content.get('tool_name') or '?'} {_truncate(_first_nonempty(content.get('error')))}"
    if event_type == "subagent_spawn_requested":
        return (
            "Subagent spawn requested: "
            f"type={content.get('agent_type') or '?'} "
            f"message={_truncate(_first_nonempty(content.get('message')))}"
        )
    if event_type == "subagent_spawned":
        return (
            "Subagent spawned: "
            f"agent_id={content.get('agent_id') or '?'} "
            f"nickname={content.get('nickname') or '?'}"
        )
    if event_type == "subagent_message":
        return (
            "Subagent message: "
            f"target={content.get('target_agent_id') or '?'} "
            f"state={content.get('delivery_state') or '?'} "
            f"message={_truncate(_first_nonempty(content.get('message')))}"
        )
    if event_type == "subagent_result":
        return (
            "Subagent result: "
            f"targets={_format_json(content.get('targets'))} "
            f"result={_truncate(_format_json(content.get('result')))}"
        )
    if event_type == "subagent_resumed":
        return f"Subagent resumed: agent_id={content.get('agent_id') or '?'}"
    if event_type == "subagent_closed":
        return f"Subagent closed: agent_id={content.get('agent_id') or '?'}"
    if event_type == "codex_rollout_imported":
        return (
            "Codex rollout imported: "
            f"path={content.get('path') or '?'} count={content.get('count')}"
        )
    if event_type == "codex_turn_context":
        return (
            "Codex turn context: "
            f"cwd={content.get('cwd') or '?'} "
            f"model={content.get('model') or '?'} "
            f"approval={content.get('approval_policy') or '?'}"
        )
    if event_type == "codex_task_started":
        return f"Codex task started: turn_id={content.get('turn_id') or '?'}"
    if event_type == "codex_task_complete":
        return (
            "Codex task complete: "
            f"turn_id={content.get('turn_id') or '?'} "
            f"duration_ms={content.get('duration_ms')}"
        )
    if event_type == "codex_turn_aborted":
        return f"Codex turn aborted: reason={content.get('reason') or '?'}"
    if event_type == "codex_context_compacted":
        return "Codex context compacted"
    if event_type == "session_started":
        return f"Session started: cwd={content.get('cwd') or '?'}"
    if event_type == "session_ended":
        return f"Session ended: exit_code={content.get('exit_code')}"
    return None


def build_session_report(*, session_dir: Path, include_noisy: bool = False) -> str:
    manifest = load_manifest(session_dir)
    events = load_events(session_dir)
    counter = Counter(event.get("event_type") for event in events)
    included_events: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("event_type")
        if not include_noisy and event_type in NOISY_EVENT_TYPES:
            continue
        if event_type in DEFAULT_INCLUDED_EVENT_TYPES:
            included_events.append(event)

    lines: list[str] = []
    lines.append(f"# Session Report: {session_dir.name}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- session_id: {manifest.get('session_id') or session_dir.name}")
    lines.append(f"- agent: {manifest.get('agent') or '?'}")
    lines.append(f"- cwd: {manifest.get('cwd') or '?'}")
    lines.append(f"- command: {_format_json(manifest.get('command'))}")
    lines.append(f"- provider: {manifest.get('provider') or '?'}")
    lines.append(f"- total_events: {len(events)}")
    lines.append(
        f"- noisy_events_hidden: {sum(counter.get(t, 0) for t in NOISY_EVENT_TYPES) if not include_noisy else 0}"
    )
    if manifest.get("codex_thread_ids"):
        lines.append(f"- codex_thread_ids: {_format_json(manifest.get('codex_thread_ids'))}")
    if manifest.get("codex_rollout_paths"):
        lines.append(f"- codex_rollout_paths: {_format_json(manifest.get('codex_rollout_paths'))}")
    lines.append("")
    lines.append("## Event Counts")
    lines.append("")
    for event_type, count in counter.most_common():
        marker = " (hidden by default)" if event_type in NOISY_EVENT_TYPES and not include_noisy else ""
        lines.append(f"- {event_type}: {count}{marker}")
    lines.append("")
    lines.append("## Timeline")
    lines.append("")

    last_line: str | None = None
    seen_rendered_lines: set[tuple[str, str]] = set()
    timeline_count = 0
    for event in included_events:
        rendered = _event_line(event)
        if not rendered:
            continue
        event_type = event.get("event_type") or "?"
        if event_type in _DEDUPE_BY_RENDERED_LINE_EVENT_TYPES:
            dedupe_key = (event_type, rendered)
            if dedupe_key in seen_rendered_lines:
                continue
            seen_rendered_lines.add(dedupe_key)
        timestamp = event.get("timestamp") or "?"
        line = f"- {timestamp} [{event_type}] {rendered}"
        if line == last_line:
            continue
        lines.append(line)
        last_line = line
        timeline_count += 1

    if timeline_count == 0:
        lines.append("- [no included timeline events]")

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- `events.jsonl` is machine-oriented and intentionally verbose.")
    lines.append("- Large counts of `tty_output_chunk` and `assistant_text_delta` are normal in interactive sessions.")
    lines.append("- This report hides low-signal events by default so humans can read the session quickly.")
    return "\n".join(lines) + "\n"


def generate_session_report_artifact(store: SessionStore, *, include_noisy: bool = False) -> str:
    report = build_session_report(session_dir=store.session_dir, include_noisy=include_noisy)
    artifact_ref = store.write_text_artifact("session_report.md", report)
    store.append_event(
        make_event(
            store.session_id,
            "session_report_generated",
            actor=ActorRef(kind="runtime", id="renderer"),
            target=TargetRef(kind="artifact", name="session_report.md"),
            content={"artifact": artifact_ref, "include_noisy": include_noisy},
            artifacts=[artifact_ref],
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )
    return artifact_ref
