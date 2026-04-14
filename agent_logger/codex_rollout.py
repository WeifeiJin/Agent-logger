from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
from typing import Any

from .schema import ActorRef, Event, TargetRef, Visibility, make_event
from .subagents import subagent_events_for_tool_stage


@dataclass(slots=True)
class RolloutCursorState:
    current_turn_id: str | None = None
    current_model: str | None = None
    tool_name_by_call_id: dict[str, str] = field(default_factory=dict)
    tool_arguments_by_call_id: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> "RolloutCursorState":
        return RolloutCursorState(
            current_turn_id=self.current_turn_id,
            current_model=self.current_model,
            tool_name_by_call_id=dict(self.tool_name_by_call_id),
            tool_arguments_by_call_id=dict(self.tool_arguments_by_call_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_turn_id": self.current_turn_id,
            "current_model": self.current_model,
            "tool_name_by_call_id": dict(self.tool_name_by_call_id),
            "tool_arguments_by_call_id": dict(self.tool_arguments_by_call_id),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RolloutCursorState":
        if not isinstance(payload, dict):
            return cls()
        tool_name_by_call_id = payload.get("tool_name_by_call_id", {})
        tool_arguments_by_call_id = payload.get("tool_arguments_by_call_id", {})
        if not isinstance(tool_name_by_call_id, dict):
            tool_name_by_call_id = {}
        if not isinstance(tool_arguments_by_call_id, dict):
            tool_arguments_by_call_id = {}
        return cls(
            current_turn_id=payload.get("current_turn_id") if isinstance(payload.get("current_turn_id"), str) else None,
            current_model=payload.get("current_model") if isinstance(payload.get("current_model"), str) else None,
            tool_name_by_call_id={
                str(key): str(value)
                for key, value in tool_name_by_call_id.items()
                if isinstance(key, str) and isinstance(value, str)
            },
            tool_arguments_by_call_id={
                str(key): value
                for key, value in tool_arguments_by_call_id.items()
                if isinstance(key, str)
            },
        )


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"} and item.get("text"):
                    parts.append(str(item["text"]))
                elif item_type == "thinking" and item.get("thinking"):
                    parts.append(str(item["thinking"]))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return _extract_text(content["content"])
    return str(content)


def _best_effort_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    result = dict(payload)
                    result.setdefault("timestamp", entry.get("timestamp"))
                    return result
    except OSError:
        return None
    return None


def find_rollout_paths(
    sessions_root: Path,
    *,
    thread_ids: list[str] | None = None,
    cwd: str | None = None,
    started_at_epoch: int | None = None,
    fallback_limit: int = 1,
) -> list[Path]:
    resolved_root = Path(sessions_root)
    matches: list[Path] = []
    seen: set[Path] = set()

    for thread_id in thread_ids or []:
        for path in sorted(resolved_root.rglob(f"rollout-*{thread_id}.jsonl")):
            if path not in seen:
                seen.add(path)
                matches.append(path)

    if matches:
        return matches

    if cwd and started_at_epoch is not None:
        fallback_candidates: list[tuple[float, float, Path]] = []
        for path in resolved_root.rglob("rollout-*.jsonl"):
            if path in seen:
                continue
            meta = _read_session_meta(path)
            if not meta or meta.get("cwd") != cwd:
                continue
            meta_epoch = _parse_iso_timestamp(meta.get("timestamp"))
            if meta_epoch is None:
                continue
            if meta_epoch < started_at_epoch - 300 or meta_epoch > started_at_epoch + 3600:
                continue
            fallback_candidates.append((abs(meta_epoch - started_at_epoch), meta_epoch, path))
        fallback_candidates.sort(key=lambda item: (item[0], item[1]))
        for _, _, path in fallback_candidates[:fallback_limit]:
            if path not in seen:
                seen.add(path)
                matches.append(path)

    return matches


def read_rollout_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
    return entries


def extract_rollout_thread_id(entries: list[dict[str, Any]]) -> str | None:
    for entry in entries:
        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        thread_id = payload.get("id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


def _rollout_metadata(
    *,
    rollout_path: str | None,
    thread_id: str | None,
    entry_index: int,
    entry_type: str,
    payload_type: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "codex_rollout",
        "rollout_entry_index": entry_index,
        "rollout_entry_type": entry_type,
    }
    if rollout_path:
        metadata["rollout_path"] = rollout_path
    if thread_id:
        metadata["codex_thread_id"] = thread_id
    if payload_type:
        metadata["rollout_payload_type"] = payload_type
    return metadata


def _tool_result_events(
    *,
    session_id: str,
    trace_id: str | None,
    platform: str,
    timestamp: str | None,
    turn_id: str | None,
    payload: dict[str, Any],
    tool_name: str | None,
    platform_metadata: dict[str, Any],
) -> list[Event]:
    events: list[Event] = []
    call_id = payload.get("call_id")
    success = payload.get("success")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    aggregated_output = payload.get("aggregated_output")
    result_output = aggregated_output or stdout or payload.get("output")

    if not tool_name and payload.get("type") == "patch_apply_end":
        tool_name = "apply_patch"
    if not tool_name and payload.get("type") == "exec_command_end":
        tool_name = "exec_command"

    target = TargetRef(kind="agent", name="codex")
    actor = ActorRef(kind="tool", id=str(call_id) if call_id else tool_name)
    visibility = Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False)

    if stdout or aggregated_output:
        events.append(
            make_event(
                session_id,
                "tool_call_stdout",
                timestamp=timestamp,
                turn_id=turn_id,
                trace_id=trace_id,
                platform=platform,
                actor=actor,
                target=target,
                content={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "text": stdout or aggregated_output,
                },
                visibility=visibility,
                platform_metadata=platform_metadata,
            )
        )

    if stderr:
        events.append(
            make_event(
                session_id,
                "tool_call_stderr",
                timestamp=timestamp,
                turn_id=turn_id,
                trace_id=trace_id,
                platform=platform,
                actor=actor,
                target=target,
                content={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "text": stderr,
                },
                visibility=visibility,
                platform_metadata=platform_metadata,
            )
        )

    result_content = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "success": success,
        "output": result_output,
    }
    for key in (
        "command",
        "cwd",
        "process_id",
        "parsed_cmd",
        "source",
        "changes",
        "duration_ms",
        "completed_at",
    ):
        if key in payload:
            result_content[key] = payload.get(key)

    events.append(
        make_event(
            session_id,
            "tool_call_result",
            timestamp=timestamp,
            turn_id=turn_id,
            trace_id=trace_id,
            platform=platform,
            actor=actor,
            target=target,
            content=result_content,
            visibility=visibility,
            platform_metadata=platform_metadata,
        )
    )

    if success is False:
        events.append(
            make_event(
                session_id,
                "tool_call_error",
                timestamp=timestamp,
                turn_id=turn_id,
                trace_id=trace_id,
                platform=platform,
                actor=actor,
                target=target,
                content={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "error": stderr or result_output,
                },
                visibility=visibility,
                platform_metadata=platform_metadata,
            )
        )
    return events


def canonicalize_rollout_delta(
    entries: list[dict[str, Any]],
    *,
    session_id: str,
    platform: str = "codex",
    trace_id: str | None = None,
    thread_id: str | None = None,
    rollout_path: str | None = None,
    entry_index_offset: int = 0,
    include_request_backfill: bool = False,
    include_response_backfill: bool = False,
    state: RolloutCursorState | None = None,
) -> tuple[list[Event], RolloutCursorState]:
    events: list[Event] = []
    cursor = state.copy() if state is not None else RolloutCursorState()
    effective_thread_id = thread_id or extract_rollout_thread_id(entries)

    for entry_index, entry in enumerate(entries):
        timestamp = entry.get("timestamp")
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get("type")
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else cursor.current_turn_id
        if isinstance(payload.get("turn_id"), str) and payload.get("turn_id"):
            cursor.current_turn_id = str(payload["turn_id"])
            turn_id = cursor.current_turn_id

        platform_metadata = _rollout_metadata(
            rollout_path=rollout_path,
            thread_id=effective_thread_id,
            entry_index=entry_index_offset + entry_index,
            entry_type=str(entry_type),
            payload_type=str(payload_type) if isinstance(payload_type, str) else None,
        )

        if entry_type == "session_meta":
            cursor.current_model = cursor.current_model or str(payload.get("model") or "")
            events.append(
                make_event(
                    session_id,
                    "codex_session_meta",
                    timestamp=timestamp,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="runtime", id="codex_rollout"),
                    target=TargetRef(kind="agent", name="codex"),
                    content={
                        "codex_thread_id": payload.get("id"),
                        "cwd": payload.get("cwd"),
                        "originator": payload.get("originator"),
                        "cli_version": payload.get("cli_version"),
                        "source": payload.get("source"),
                        "model_provider": payload.get("model_provider"),
                        "has_base_instructions": bool(
                            isinstance(payload.get("base_instructions"), dict)
                            and payload["base_instructions"].get("text")
                        ),
                    },
                    context_ref={"cwd": payload.get("cwd")},
                    visibility=Visibility(
                        provider_exposed=False,
                        runtime_exposed=True,
                        user_visible=False,
                    ),
                    platform_metadata=platform_metadata,
                )
            )
            continue

        if entry_type == "turn_context":
            if isinstance(payload.get("model"), str) and payload.get("model"):
                cursor.current_model = str(payload["model"])
            events.append(
                make_event(
                    session_id,
                    "codex_turn_context",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="runtime", id="codex_rollout"),
                    target=TargetRef(kind="agent", name="codex"),
                    content=payload,
                    context_ref={"cwd": payload.get("cwd")},
                    visibility=Visibility(
                        provider_exposed=False,
                        runtime_exposed=True,
                        user_visible=False,
                    ),
                    platform_metadata=platform_metadata,
                )
            )
            continue

        if entry_type == "event_msg":
            event_name = payload.get("type")
            if event_name == "task_started":
                events.append(
                    make_event(
                        session_id,
                        "codex_task_started",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="codex_rollout"),
                        target=TargetRef(kind="agent", name="codex"),
                        content=payload,
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "task_complete":
                last_message = payload.get("last_agent_message")
                if isinstance(last_message, str) and last_message:
                    events.append(
                        make_event(
                            session_id,
                            "final_output",
                            timestamp=timestamp,
                            turn_id=turn_id,
                            trace_id=trace_id,
                            platform=platform,
                            actor=ActorRef(kind="assistant", id="codex"),
                            target=TargetRef(kind="user", name="chat"),
                            content={"text": last_message},
                            visibility=Visibility(
                                provider_exposed=False,
                                runtime_exposed=True,
                                user_visible=True,
                            ),
                            platform_metadata=platform_metadata,
                        )
                    )
                events.append(
                    make_event(
                        session_id,
                        "codex_task_complete",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="codex_rollout"),
                        target=TargetRef(kind="agent", name="codex"),
                        content=payload,
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "turn_aborted":
                events.append(
                    make_event(
                        session_id,
                        "codex_turn_aborted",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="codex_rollout"),
                        target=TargetRef(kind="agent", name="codex"),
                        content=payload,
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "context_compacted":
                events.append(
                    make_event(
                        session_id,
                        "codex_context_compacted",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="codex_rollout"),
                        target=TargetRef(kind="agent", name="codex"),
                        content=payload,
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "token_count":
                events.append(
                    make_event(
                        session_id,
                        "codex_token_count",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="codex_rollout"),
                        target=TargetRef(kind="agent", name="codex"),
                        content=payload,
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "user_message":
                events.append(
                    make_event(
                        session_id,
                        "user_input",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="user", id="codex_user"),
                        target=TargetRef(kind="agent", name="codex"),
                        content={
                            "text": payload.get("message", ""),
                            "images": payload.get("images"),
                            "local_images": payload.get("local_images"),
                            "text_elements": payload.get("text_elements"),
                        },
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=True,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name == "agent_message":
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_final",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="assistant", id="codex"),
                        target=TargetRef(kind="user", name="chat"),
                        content={
                            "text": payload.get("message", ""),
                            "phase": payload.get("phase"),
                            "memory_citation": payload.get("memory_citation"),
                        },
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=True,
                        ),
                        platform_metadata=platform_metadata,
                    )
                )
            elif event_name in {"exec_command_end", "patch_apply_end"}:
                tool_name = None
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    tool_name = cursor.tool_name_by_call_id.get(call_id)
                events.extend(
                    _tool_result_events(
                        session_id=session_id,
                        trace_id=trace_id,
                        platform=platform,
                        timestamp=timestamp,
                        turn_id=turn_id,
                        payload=payload,
                        tool_name=tool_name,
                        platform_metadata=platform_metadata,
                    )
                )
            continue

        if entry_type != "response_item":
            continue

        target_model = TargetRef(kind="model", name=cursor.current_model or None)
        visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

        if payload_type == "message":
            role = payload.get("role")
            if role in {"developer", "system", "user"} and not include_request_backfill:
                continue
            if role == "assistant" and not include_response_backfill:
                continue
            text = _extract_text(payload.get("content"))
            if not text:
                continue
            if role == "assistant":
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_final",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target_model,
                        content={"text": text, "role": role},
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
            else:
                event_type = {
                    "developer": "request_system_message",
                    "system": "request_system_message",
                    "user": "request_user_message",
                }.get(role, "request_message")
                events.append(
                    make_event(
                        session_id,
                        event_type,
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind=str(role), id=str(role)),
                        target=target_model,
                        content={"text": text, "role": role},
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
        elif payload_type == "reasoning":
            summary_text = _extract_text(payload.get("summary"))
            has_encrypted_content = bool(payload.get("encrypted_content"))
            if not include_response_backfill and not has_encrypted_content:
                continue
            events.append(
                make_event(
                    session_id,
                    "assistant_reasoning_final",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=target_model,
                    content={
                        "summary_text": summary_text,
                        "has_encrypted_content": has_encrypted_content,
                    },
                    visibility=visibility,
                    platform_metadata=platform_metadata,
                )
            )
        elif payload_type == "function_call":
            if not include_response_backfill and not include_request_backfill:
                continue
            tool_name = payload.get("name")
            call_id = payload.get("call_id") or payload.get("id")
            arguments = _best_effort_json(payload.get("arguments"))
            if isinstance(call_id, str) and isinstance(tool_name, str):
                cursor.tool_name_by_call_id[call_id] = tool_name
                cursor.tool_arguments_by_call_id[call_id] = arguments
            if include_response_backfill:
                events.append(
                    make_event(
                        session_id,
                        "tool_call_requested",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=TargetRef(kind="tool", name=tool_name),
                        content={
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "arguments": arguments,
                        },
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
                events.extend(
                    subagent_events_for_tool_stage(
                        stage="requested",
                        session_id=session_id,
                        platform=platform,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        arguments=cursor.tool_arguments_by_call_id.get(str(call_id)) if call_id is not None else None,
                        trace_id=trace_id,
                        timestamp=timestamp,
                        turn_id=turn_id,
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
            if include_request_backfill:
                events.append(
                    make_event(
                        session_id,
                        "tool_call_dispatched",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="runtime", id="tool_dispatcher"),
                        target=TargetRef(kind="tool", name=tool_name),
                        content={
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "arguments": arguments,
                        },
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
                events.extend(
                    subagent_events_for_tool_stage(
                        stage="dispatched",
                        session_id=session_id,
                        platform=platform,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        arguments=cursor.tool_arguments_by_call_id.get(str(call_id)) if call_id is not None else None,
                        trace_id=trace_id,
                        timestamp=timestamp,
                        turn_id=turn_id,
                        visibility=visibility,
                        platform_metadata=platform_metadata,
                    )
                )
        elif payload_type == "function_call_output":
            if not include_request_backfill:
                continue
            call_id = payload.get("call_id")
            tool_name = cursor.tool_name_by_call_id.get(str(call_id)) if call_id is not None else None
            tool_arguments = cursor.tool_arguments_by_call_id.get(str(call_id)) if call_id is not None else None
            events.append(
                make_event(
                    session_id,
                    "tool_call_result",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="tool", id=str(call_id) if call_id else None),
                    target=target_model,
                    content={
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "output": _extract_text(payload.get("output")),
                    },
                    visibility=visibility,
                    platform_metadata=platform_metadata,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="result",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=call_id,
                    arguments=tool_arguments,
                    output=payload.get("output"),
                    trace_id=trace_id,
                    timestamp=timestamp,
                    turn_id=turn_id,
                    visibility=visibility,
                    platform_metadata=platform_metadata,
                )
            )
        elif payload_type == "custom_tool_call":
            tool_name = payload.get("name")
            call_id = payload.get("call_id") or payload.get("id")
            if isinstance(call_id, str) and isinstance(tool_name, str):
                cursor.tool_name_by_call_id[call_id] = tool_name
            target = TargetRef(kind="tool", name=tool_name)
            content = {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "arguments": _best_effort_json(payload.get("input")),
                "status": payload.get("status"),
            }
            runtime_visibility = Visibility(
                provider_exposed=False,
                runtime_exposed=True,
                user_visible=False,
            )
            events.append(
                make_event(
                    session_id,
                    "tool_call_requested",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=target,
                    content=content,
                    visibility=runtime_visibility,
                    platform_metadata=platform_metadata,
                )
            )
            events.append(
                make_event(
                    session_id,
                    "tool_call_dispatched",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="runtime", id="tool_dispatcher"),
                    target=target,
                    content=content,
                    visibility=runtime_visibility,
                    platform_metadata=platform_metadata,
                )
            )
        elif payload_type == "custom_tool_call_output":
            tool_name = None
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                tool_name = cursor.tool_name_by_call_id.get(call_id)
            runtime_visibility = Visibility(
                provider_exposed=False,
                runtime_exposed=True,
                user_visible=False,
            )
            output_text = _extract_text(payload.get("output"))
            events.append(
                make_event(
                    session_id,
                    "tool_call_result",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="tool", id=str(call_id) if call_id else tool_name),
                    target=TargetRef(kind="agent", name="codex"),
                    content={
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "output": output_text,
                    },
                    visibility=runtime_visibility,
                    platform_metadata=platform_metadata,
                )
            )
            if output_text.lower().startswith("execution error"):
                events.append(
                    make_event(
                        session_id,
                        "tool_call_error",
                        timestamp=timestamp,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        platform=platform,
                        actor=ActorRef(kind="tool", id=str(call_id) if call_id else tool_name),
                        target=TargetRef(kind="agent", name="codex"),
                        content={
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "error": output_text,
                        },
                        visibility=runtime_visibility,
                        platform_metadata=platform_metadata,
                    )
                )

    return events, cursor


def canonicalize_rollout_entries(
    entries: list[dict[str, Any]],
    *,
    session_id: str,
    platform: str = "codex",
    trace_id: str | None = None,
    thread_id: str | None = None,
    rollout_path: str | None = None,
    include_request_backfill: bool = False,
    include_response_backfill: bool = False,
) -> list[Event]:
    events, _ = canonicalize_rollout_delta(
        entries,
        session_id=session_id,
        platform=platform,
        trace_id=trace_id,
        thread_id=thread_id,
        rollout_path=rollout_path,
        include_request_backfill=include_request_backfill,
        include_response_backfill=include_response_backfill,
    )
    return events
