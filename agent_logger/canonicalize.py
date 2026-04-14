from __future__ import annotations

import json
from typing import Any

from .schema import ActorRef, TargetRef, Visibility, make_event, Event
from .subagents import subagent_events_for_tool_stage


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
                elif item_type == "tool_result" and item.get("content"):
                    parts.append(_extract_text(item.get("content")))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return _extract_text(content["content"])
    return str(content)


def _best_effort_arguments(raw_arguments: Any) -> Any:
    if isinstance(raw_arguments, str):
        try:
            return json.loads(raw_arguments)
        except json.JSONDecodeError:
            return raw_arguments
    return raw_arguments


def parse_sse_events(stream_body: bytes | str) -> list[dict[str, Any]]:
    text = stream_body.decode("utf-8", errors="replace") if isinstance(stream_body, bytes) else stream_body
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        data_raw = "\n".join(data_lines).strip()
        payload: Any = data_raw
        if data_raw and data_raw != "[DONE]":
            try:
                payload = json.loads(data_raw)
            except json.JSONDecodeError:
                payload = data_raw
        events.append({"event": event_name, "data": payload, "raw_data": data_raw})
    return events


def canonicalize_request(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None = None,
    parent_event_id: str | None = None,
) -> list[Event]:
    if isinstance(payload.get("input"), list):
        return _canonicalize_responses_api_request(
            payload,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )

    events: list[Event] = []
    model_name = payload.get("model")
    target = TargetRef(kind="model", name=model_name)
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

    system_value = payload.get("system")
    if system_value:
        system_text = _extract_text(system_value)
        if system_text:
            events.append(
                make_event(
                    session_id,
                    "request_system_message",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="system", id="system"),
                    target=target,
                    content={"text": system_text},
                    visibility=visibility,
                )
            )

    for index, message in enumerate(payload.get("messages", [])):
        role = message.get("role", "unknown")
        content_text = _extract_text(message.get("content"))
        event_type = {
            "user": "request_user_message",
            "assistant": "request_assistant_message",
            "system": "request_system_message",
            "tool": "tool_call_result_attached",
        }.get(role, "request_message")
        content = {
            "message_index": index,
            "role": role,
            "text": content_text,
        }
        if role == "tool":
            content["tool_call_id"] = message.get("tool_call_id")
            content["name"] = message.get("name")
        events.append(
            make_event(
                session_id,
                event_type,
                platform=platform,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                actor=ActorRef(kind=role, id=role),
                target=target,
                content=content,
                visibility=visibility,
            )
        )
    return events


def _canonicalize_responses_api_request(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
) -> list[Event]:
    events: list[Event] = []
    tool_name_by_call_id: dict[str, str] = {}
    tool_arguments_by_call_id: dict[str, Any] = {}
    model_name = payload.get("model")
    target = TargetRef(kind="model", name=model_name)
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

    instructions = payload.get("instructions")
    if instructions:
        instructions_text = _extract_text(instructions)
        if instructions_text:
            events.append(
                make_event(
                    session_id,
                    "request_system_message",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="system", id="instructions"),
                    target=target,
                    content={"text": instructions_text},
                    visibility=visibility,
                )
            )

    for index, item in enumerate(payload.get("input", [])):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")

        if item_type == "message":
            role = item.get("role", "unknown")
            content_text = _extract_text(item.get("content"))
            event_type = {
                "user": "request_user_message",
                "assistant": "request_assistant_message",
                "developer": "request_system_message",
                "system": "request_system_message",
            }.get(role, "request_message")
            events.append(
                make_event(
                    session_id,
                    event_type,
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind=role, id=role),
                    target=target,
                    content={"message_index": index, "role": role, "text": content_text},
                    visibility=visibility,
                )
            )
        elif item_type == "reasoning":
            summary_text = _extract_text(item.get("summary"))
            content = {
                "message_index": index,
                "summary_text": summary_text,
                "has_encrypted_content": bool(item.get("encrypted_content")),
            }
            if item.get("id"):
                content["reasoning_id"] = item.get("id")
            events.append(
                make_event(
                    session_id,
                    "assistant_reasoning_final",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=target,
                    content=content,
                    visibility=visibility,
                )
            )
        elif item_type == "function_call":
            tool_call_id = item.get("call_id") or item.get("id")
            tool_name = item.get("name")
            arguments = _best_effort_arguments(item.get("arguments"))
            if isinstance(tool_call_id, str) and isinstance(tool_name, str):
                tool_name_by_call_id[tool_call_id] = tool_name
                tool_arguments_by_call_id[tool_call_id] = arguments
            events.append(
                make_event(
                    session_id,
                    "tool_call_dispatched",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="runtime", id="tool_dispatcher"),
                    target=TargetRef(kind="tool", name=tool_name),
                    content={
                        "message_index": index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    },
                    visibility=visibility,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="dispatched",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    visibility=visibility,
                )
            )
        elif item_type == "function_call_output":
            tool_call_id = item.get("call_id")
            output = item.get("output")
            tool_name = None
            if isinstance(tool_call_id, str):
                tool_name = tool_name_by_call_id.get(tool_call_id)
                output_arguments = tool_arguments_by_call_id.get(tool_call_id)
            else:
                output_arguments = None
            if not tool_name and isinstance(item.get("name"), str):
                tool_name = item.get("name")
            events.append(
                make_event(
                    session_id,
                    "tool_call_result",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="tool", id=tool_call_id),
                    target=target,
                    content={
                        "message_index": index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "output": _extract_text(output),
                    },
                    visibility=visibility,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="result",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=output_arguments,
                    output=output,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    visibility=visibility,
                )
            )

    return events


def canonicalize_response(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None = None,
    parent_event_id: str | None = None,
) -> list[Event]:
    if "choices" in payload:
        return _canonicalize_openai_response(
            payload,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )
    if "content" in payload and isinstance(payload.get("content"), list):
        return _canonicalize_anthropic_response(
            payload,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )
    if payload.get("object") == "response" and isinstance(payload.get("output"), list):
        return _canonicalize_responses_api_response(
            payload,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )
    return []


def canonicalize_response_stream(
    stream_body: bytes | str,
    *,
    session_id: str,
    platform: str,
    trace_id: str | None = None,
    parent_event_id: str | None = None,
) -> list[Event]:
    parsed = parse_sse_events(stream_body)
    events: list[Event] = []
    target = TargetRef(kind="model", name=None)
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)
    final_message_ids: set[str] = set()

    for entry in parsed:
        event_name = entry.get("event")
        payload = entry.get("data")
        if not isinstance(payload, dict):
            continue

        if event_name == "response.output_text.delta":
            delta = payload.get("delta")
            if delta:
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_delta",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={
                            "text": delta,
                            "item_id": payload.get("item_id"),
                            "output_index": payload.get("output_index"),
                            "content_index": payload.get("content_index"),
                        },
                        visibility=visibility,
                    )
                )
        elif event_name == "response.output_text.done":
            text = payload.get("text")
            item_id = payload.get("item_id")
            if isinstance(item_id, str):
                final_message_ids.add(item_id)
            if text:
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={
                            "text": text,
                            "item_id": item_id,
                            "output_index": payload.get("output_index"),
                            "content_index": payload.get("content_index"),
                        },
                        visibility=visibility,
                    )
                )
        elif event_name == "response.reasoning_summary_text.delta":
            delta = payload.get("delta")
            if delta:
                events.append(
                    make_event(
                        session_id,
                        "assistant_reasoning_delta",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={"text": delta, "item_id": payload.get("item_id")},
                        visibility=visibility,
                    )
                )
        elif event_name == "response.reasoning_summary_text.done":
            text = payload.get("text")
            if text:
                events.append(
                    make_event(
                        session_id,
                        "assistant_reasoning_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={"text": text, "item_id": payload.get("item_id")},
                        visibility=visibility,
                    )
                )
        elif event_name == "response.output_item.done":
            item = payload.get("item", {})
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                tool_call_id = item.get("call_id") or item.get("id")
                tool_name = item.get("name")
                arguments = _best_effort_arguments(item.get("arguments"))
                events.append(
                    make_event(
                        session_id,
                        "tool_call_requested",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=TargetRef(kind="tool", name=tool_name),
                        content={
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "arguments": arguments,
                        },
                        visibility=visibility,
                    )
                )
                events.extend(
                    subagent_events_for_tool_stage(
                        stage="requested",
                        session_id=session_id,
                        platform=platform,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        arguments=arguments,
                        trace_id=trace_id,
                        parent_event_id=parent_event_id,
                        visibility=visibility,
                    )
                )
            elif item_type == "message":
                item_id = item.get("id")
                text = _extract_text(item.get("content"))
                if item.get("role") == "assistant" and text and item_id not in final_message_ids:
                    events.append(
                        make_event(
                            session_id,
                            "assistant_text_final",
                            platform=platform,
                            parent_event_id=parent_event_id,
                            trace_id=trace_id,
                            actor=ActorRef(kind="assistant", id="assistant"),
                            target=target,
                            content={"text": text, "item_id": item_id},
                            visibility=visibility,
                        )
                    )
            elif item_type == "reasoning":
                summary_text = _extract_text(item.get("summary"))
                if summary_text:
                    events.append(
                        make_event(
                            session_id,
                            "assistant_reasoning_final",
                            platform=platform,
                            parent_event_id=parent_event_id,
                            trace_id=trace_id,
                            actor=ActorRef(kind="assistant", id="assistant"),
                            target=target,
                            content={"text": summary_text, "item_id": item.get("id")},
                            visibility=visibility,
                        )
                    )
        elif event_name == "response.completed":
            response = payload.get("response")
            if isinstance(response, dict):
                events.extend(
                    _canonicalize_responses_api_response(
                        response,
                        session_id=session_id,
                        platform=platform,
                        trace_id=trace_id,
                        parent_event_id=parent_event_id,
                    )
                )
    return events


def _canonicalize_openai_response(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
) -> list[Event]:
    events: list[Event] = []
    model_name = payload.get("model")
    target = TargetRef(kind="model", name=model_name)
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

    for choice_index, choice in enumerate(payload.get("choices", [])):
        message = choice.get("message", {})
        text = _extract_text(message.get("content"))
        if text:
            events.append(
                make_event(
                    session_id,
                    "assistant_text_final",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=target,
                    content={"choice_index": choice_index, "text": text},
                    visibility=visibility,
                )
            )
        reasoning = message.get("reasoning") or choice.get("reasoning")
        reasoning_text = _extract_text(reasoning)
        if reasoning_text:
            events.append(
                make_event(
                    session_id,
                    "assistant_reasoning_final",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=target,
                    content={"choice_index": choice_index, "text": reasoning_text},
                    visibility=visibility,
                )
            )
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            tool_name = function.get("name")
            tool_call_id = tool_call.get("id")
            arguments = _best_effort_arguments(function.get("arguments"))
            events.append(
                make_event(
                    session_id,
                    "tool_call_requested",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=TargetRef(kind="tool", name=tool_name),
                    content={
                        "choice_index": choice_index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    },
                    visibility=visibility,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="requested",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    visibility=visibility,
                )
            )
    return events


def _canonicalize_anthropic_response(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
) -> list[Event]:
    events: list[Event] = []
    target = TargetRef(kind="model", name=payload.get("model"))
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

    for block_index, block in enumerate(payload.get("content", [])):
        block_type = block.get("type")
        if block_type == "text":
            text = _extract_text(block.get("text"))
            if text:
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={"block_index": block_index, "text": text},
                        visibility=visibility,
                    )
                )
        elif block_type in {"thinking", "redacted_thinking"}:
            thinking = block.get("thinking") or block.get("text") or ""
            if thinking:
                events.append(
                    make_event(
                        session_id,
                        "assistant_reasoning_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={"block_index": block_index, "text": thinking},
                        visibility=visibility,
                    )
                )
        elif block_type == "tool_use":
            tool_name = block.get("name")
            tool_call_id = block.get("id")
            arguments = block.get("input")
            events.append(
                make_event(
                    session_id,
                    "tool_call_requested",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=TargetRef(kind="tool", name=tool_name),
                    content={
                        "block_index": block_index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    },
                    visibility=visibility,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="requested",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    visibility=visibility,
                )
            )
    return events


def _canonicalize_responses_api_response(
    payload: dict[str, Any],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
) -> list[Event]:
    events: list[Event] = []
    target = TargetRef(kind="model", name=payload.get("model"))
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)

    for output_index, item in enumerate(payload.get("output", [])):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text = _extract_text(item.get("content"))
            if text:
                events.append(
                    make_event(
                        session_id,
                        "assistant_text_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content={"output_index": output_index, "text": text, "item_id": item.get("id")},
                        visibility=visibility,
                    )
                )
        elif item_type == "function_call":
            tool_call_id = item.get("call_id") or item.get("id")
            tool_name = item.get("name")
            arguments = _best_effort_arguments(item.get("arguments"))
            events.append(
                make_event(
                    session_id,
                    "tool_call_requested",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=TargetRef(kind="tool", name=tool_name),
                    content={
                        "output_index": output_index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    },
                    visibility=visibility,
                )
            )
            events.extend(
                subagent_events_for_tool_stage(
                    stage="requested",
                    session_id=session_id,
                    platform=platform,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    visibility=visibility,
                )
            )
    return events
