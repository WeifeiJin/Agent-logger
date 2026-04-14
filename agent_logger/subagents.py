from __future__ import annotations

import json
from typing import Any

from .schema import ActorRef, Event, TargetRef, Visibility, make_event


SUBAGENT_TOOL_NAMES = {
    "spawn_agent",
    "send_input",
    "wait_agent",
    "resume_agent",
    "close_agent",
}


def _best_effort_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def is_subagent_tool(tool_name: Any) -> bool:
    return isinstance(tool_name, str) and tool_name in SUBAGENT_TOOL_NAMES


def _ensure_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _maybe_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _spawn_target_name(arguments: dict[str, Any]) -> str | None:
    for key in ("agent_type", "model", "name"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return "subagent"


def _spawn_output_fields(parsed_output: Any) -> tuple[str | None, str | None]:
    if isinstance(parsed_output, dict):
        agent_id = _maybe_string(parsed_output.get("id")) or _maybe_string(parsed_output.get("agent_id"))
        nickname = (
            _maybe_string(parsed_output.get("nickname"))
            or _maybe_string(parsed_output.get("user_facing_nickname"))
            or _maybe_string(parsed_output.get("name"))
        )
        return agent_id, nickname
    return None, None


def _close_or_resume_target(arguments: dict[str, Any], parsed_output: Any) -> str | None:
    target = _maybe_string(arguments.get("id")) or _maybe_string(arguments.get("target"))
    if target:
        return target
    if isinstance(parsed_output, dict):
        return _maybe_string(parsed_output.get("id")) or _maybe_string(parsed_output.get("target"))
    return None


def _wait_targets(arguments: dict[str, Any], parsed_output: Any) -> list[str]:
    targets = arguments.get("targets")
    if isinstance(targets, list):
        normalized = [target for target in targets if isinstance(target, str) and target]
        if normalized:
            return normalized
    if isinstance(parsed_output, dict):
        output_targets = parsed_output.get("targets")
        if isinstance(output_targets, list):
            normalized = [target for target in output_targets if isinstance(target, str) and target]
            if normalized:
                return normalized
    return []


def subagent_events_for_tool_stage(
    *,
    stage: str,
    session_id: str,
    platform: str,
    tool_name: str | None,
    tool_call_id: str | None,
    arguments: Any = None,
    output: Any = None,
    trace_id: str | None = None,
    parent_event_id: str | None = None,
    timestamp: str | None = None,
    turn_id: str | None = None,
    visibility: Visibility | None = None,
    platform_metadata: dict[str, Any] | None = None,
) -> list[Event]:
    if not is_subagent_tool(tool_name):
        return []

    normalized_arguments = _ensure_dict(_best_effort_json(arguments))
    parsed_output = _best_effort_json(output)
    event_visibility = visibility or Visibility(
        provider_exposed=False,
        runtime_exposed=True,
        user_visible=False,
    )
    metadata = dict(platform_metadata or {})
    if tool_call_id and "tool_call_id" not in metadata:
        metadata["tool_call_id"] = tool_call_id

    if tool_name == "spawn_agent":
        if stage == "requested":
            return [
                make_event(
                    session_id,
                    "subagent_spawn_requested",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=TargetRef(kind="agent", name=_spawn_target_name(normalized_arguments)),
                    content={
                        "tool_call_id": tool_call_id,
                        "agent_type": normalized_arguments.get("agent_type"),
                        "model": normalized_arguments.get("model"),
                        "reasoning_effort": normalized_arguments.get("reasoning_effort"),
                        "fork_context": normalized_arguments.get("fork_context"),
                        "message": normalized_arguments.get("message"),
                        "items": normalized_arguments.get("items"),
                    },
                    visibility=event_visibility,
                    platform_metadata=metadata,
                )
            ]
        if stage == "result":
            agent_id, nickname = _spawn_output_fields(parsed_output)
            return [
                make_event(
                    session_id,
                    "subagent_spawned",
                    timestamp=timestamp,
                    turn_id=turn_id,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="runtime", id="subagent_runtime"),
                    target=TargetRef(kind="agent", name=agent_id or nickname or _spawn_target_name(normalized_arguments)),
                    content={
                        "tool_call_id": tool_call_id,
                        "agent_id": agent_id,
                        "nickname": nickname,
                        "agent_type": normalized_arguments.get("agent_type"),
                        "model": normalized_arguments.get("model"),
                        "message": normalized_arguments.get("message"),
                        "result": parsed_output,
                    },
                    visibility=event_visibility,
                    platform_metadata=metadata,
                )
            ]

    if tool_name == "send_input" and stage in {"requested", "dispatched", "result"}:
        target_agent_id = _maybe_string(normalized_arguments.get("target"))
        content = {
            "tool_call_id": tool_call_id,
            "target_agent_id": target_agent_id,
            "message": normalized_arguments.get("message"),
            "items": normalized_arguments.get("items"),
            "interrupt": normalized_arguments.get("interrupt"),
            "delivery_state": {
                "requested": "requested",
                "dispatched": "dispatched",
                "result": "acknowledged",
            }[stage],
        }
        if stage == "result":
            content["result"] = parsed_output
        actor = (
            ActorRef(kind="assistant", id="assistant")
            if stage == "requested"
            else ActorRef(kind="runtime", id="subagent_runtime")
        )
        return [
            make_event(
                session_id,
                "subagent_message",
                timestamp=timestamp,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                platform=platform,
                actor=actor,
                target=TargetRef(kind="agent", name=target_agent_id),
                content=content,
                visibility=event_visibility,
                platform_metadata=metadata,
            )
        ]

    if tool_name == "wait_agent" and stage == "result":
        targets = _wait_targets(normalized_arguments, parsed_output)
        return [
            make_event(
                session_id,
                "subagent_result",
                timestamp=timestamp,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                platform=platform,
                actor=ActorRef(kind="runtime", id="subagent_runtime"),
                target=TargetRef(kind="agent", name=targets[0] if targets else None),
                content={
                    "tool_call_id": tool_call_id,
                    "targets": targets,
                    "timeout_ms": normalized_arguments.get("timeout_ms"),
                    "result": parsed_output,
                },
                visibility=event_visibility,
                platform_metadata=metadata,
            )
        ]

    if tool_name == "resume_agent" and stage == "result":
        target_agent_id = _close_or_resume_target(normalized_arguments, parsed_output)
        return [
            make_event(
                session_id,
                "subagent_resumed",
                timestamp=timestamp,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                platform=platform,
                actor=ActorRef(kind="runtime", id="subagent_runtime"),
                target=TargetRef(kind="agent", name=target_agent_id),
                content={
                    "tool_call_id": tool_call_id,
                    "agent_id": target_agent_id,
                    "result": parsed_output,
                },
                visibility=event_visibility,
                platform_metadata=metadata,
            )
        ]

    if tool_name == "close_agent" and stage == "result":
        target_agent_id = _close_or_resume_target(normalized_arguments, parsed_output)
        return [
            make_event(
                session_id,
                "subagent_closed",
                timestamp=timestamp,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                platform=platform,
                actor=ActorRef(kind="runtime", id="subagent_runtime"),
                target=TargetRef(kind="agent", name=target_agent_id),
                content={
                    "tool_call_id": tool_call_id,
                    "agent_id": target_agent_id,
                    "result": parsed_output,
                },
                visibility=event_visibility,
                platform_metadata=metadata,
            )
        ]

    return []
