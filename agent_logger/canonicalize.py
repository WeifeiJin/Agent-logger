from __future__ import annotations

import json
from typing import Any

from .schema import ActorRef, TargetRef, Visibility, make_event, Event
from .subagents import subagent_events_for_tool_stage


_ANTHROPIC_REQUEST_BLOCK_TYPES = {
    "tool_use",
    "tool_result",
    "thinking",
    "redacted_thinking",
}

_ANTHROPIC_STREAM_EVENT_NAMES = {
    "message_start",
    "message_delta",
    "message_stop",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
}


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


def _normalize_anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                normalized.append(item)
            elif isinstance(item, str):
                normalized.append({"type": "text", "text": item})
        return normalized
    return [{"type": "text", "text": str(content)}]


def _looks_like_anthropic_request(payload: dict[str, Any]) -> bool:
    if "anthropic-version" in payload or "anthropic_version" in payload:
        return True
    if isinstance(payload.get("system"), (list, dict)):
        return True
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and "input_schema" in tool:
                return True
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        for block in _normalize_anthropic_blocks(message.get("content")):
            block_type = block.get("type")
            if block_type in _ANTHROPIC_REQUEST_BLOCK_TYPES:
                return True
            if isinstance(block, dict) and block.get("tool_use_id"):
                return True
    return False


def _emit_anthropic_message_events(
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
    role: str,
    content: Any,
    target: TargetRef,
    visibility: Visibility,
    message_index: int | None = None,
    text_event_type: str | None = None,
    tool_result_event_type: str = "tool_call_result_attached",
) -> list[Event]:
    events: list[Event] = []
    blocks = _normalize_anthropic_blocks(content)
    text_parts: list[str] = []

    for block_index, block in enumerate(blocks):
        block_type = block.get("type")
        if block_type in {None, "text", "input_text", "output_text"}:
            text = _extract_text(block.get("text") if block_type else block)
            if text:
                text_parts.append(text)
            continue
        if block_type in {"thinking", "redacted_thinking"}:
            reasoning_text = _extract_text(block.get("thinking") or block.get("text"))
            reasoning_content = {
                "block_index": block_index,
                "message_index": message_index,
            }
            if block_type == "redacted_thinking" and not reasoning_text:
                reasoning_content["has_encrypted_content"] = True
            if reasoning_text:
                reasoning_content["text"] = reasoning_text
            if reasoning_text or reasoning_content.get("has_encrypted_content"):
                events.append(
                    make_event(
                        session_id,
                        "assistant_reasoning_final",
                        platform=platform,
                        parent_event_id=parent_event_id,
                        trace_id=trace_id,
                        actor=ActorRef(kind="assistant", id="assistant"),
                        target=target,
                        content=reasoning_content,
                        visibility=visibility,
                    )
                )
            continue
        if block_type == "tool_use":
            tool_name = block.get("name")
            tool_call_id = block.get("id") or block.get("tool_use_id")
            arguments = block.get("input")
            content_payload = {
                "block_index": block_index,
                "message_index": message_index,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
            }
            events.append(
                make_event(
                    session_id,
                    "tool_call_requested",
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind="assistant", id="assistant"),
                    target=TargetRef(kind="tool", name=tool_name),
                    content=content_payload,
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
            continue
        if block_type == "tool_result":
            tool_call_id = block.get("tool_use_id") or block.get("tool_call_id") or block.get("id")
            output = _extract_text(block.get("content"))
            events.append(
                make_event(
                    session_id,
                    tool_result_event_type,
                    platform=platform,
                    parent_event_id=parent_event_id,
                    trace_id=trace_id,
                    actor=ActorRef(kind=role or "user", id=role or "user"),
                    target=target,
                    content={
                        "block_index": block_index,
                        "message_index": message_index,
                        "tool_call_id": tool_call_id,
                        "output": output,
                        "is_error": bool(block.get("is_error")),
                    },
                    visibility=visibility,
                )
            )
            continue

    if text_event_type and text_parts:
        text_content = {
            "text": "\n".join(text_parts),
        }
        if message_index is not None:
            text_content["message_index"] = message_index
        events.append(
            make_event(
                session_id,
                text_event_type,
                platform=platform,
                parent_event_id=parent_event_id,
                trace_id=trace_id,
                actor=ActorRef(kind=role, id=role),
                target=target,
                content=text_content,
                visibility=visibility,
            )
        )

    return events


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
    if _looks_like_anthropic_request(payload):
        return _canonicalize_anthropic_request(
            payload,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )
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


def _canonicalize_anthropic_request(
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
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        text_event_type = {
            "user": "request_user_message",
            "assistant": "request_assistant_message",
            "system": "request_system_message",
            "developer": "request_system_message",
        }.get(role, "request_message")
        events.extend(
            _emit_anthropic_message_events(
                session_id=session_id,
                platform=platform,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                role=role,
                content=message.get("content"),
                target=target,
                visibility=visibility,
                message_index=index,
                text_event_type=text_event_type,
                tool_result_event_type="tool_call_result_attached",
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
    if any(entry.get("event") in _ANTHROPIC_STREAM_EVENT_NAMES for entry in parsed):
        return _canonicalize_anthropic_response_stream(
            parsed,
            session_id=session_id,
            platform=platform,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
        )
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


def _canonicalize_anthropic_response_stream(
    parsed: list[dict[str, Any]],
    *,
    session_id: str,
    platform: str,
    trace_id: str | None,
    parent_event_id: str | None,
) -> list[Event]:
    events: list[Event] = []
    model_name: str | None = None
    visibility = Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False)
    block_state: dict[int, dict[str, Any]] = {}

    for entry in parsed:
        event_name = entry.get("event")
        payload = entry.get("data")
        if not isinstance(payload, dict):
            continue

        if event_name == "message_start":
            message = payload.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                model_name = message.get("model")
            continue

        if event_name == "content_block_start":
            index = payload.get("index")
            content_block = payload.get("content_block")
            if not isinstance(index, int) or not isinstance(content_block, dict):
                continue
            block_type = content_block.get("type")
            block_state[index] = {
                "type": block_type,
                "tool_call_id": content_block.get("id"),
                "tool_name": content_block.get("name"),
                "text": "",
                "reasoning": "",
                "arguments": "",
                "has_encrypted_content": block_type == "redacted_thinking",
            }
            if block_type == "text":
                block_state[index]["text"] = _extract_text(content_block.get("text"))
            elif block_type in {"thinking", "redacted_thinking"}:
                block_state[index]["reasoning"] = _extract_text(
                    content_block.get("thinking") or content_block.get("text")
                )
            elif block_type == "tool_use":
                input_value = content_block.get("input")
                if isinstance(input_value, str):
                    block_state[index]["arguments"] = input_value
                elif input_value is not None:
                    block_state[index]["arguments"] = json.dumps(input_value, ensure_ascii=False)
            continue

        if event_name == "content_block_delta":
            index = payload.get("index")
            delta = payload.get("delta")
            if not isinstance(index, int) or not isinstance(delta, dict):
                continue
            state = block_state.setdefault(index, {"type": None, "text": "", "reasoning": "", "arguments": ""})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                state["text"] = str(state.get("text") or "") + str(delta.get("text") or "")
            elif delta_type == "thinking_delta":
                state["reasoning"] = str(state.get("reasoning") or "") + str(delta.get("thinking") or "")
            elif delta_type == "input_json_delta":
                state["arguments"] = str(state.get("arguments") or "") + str(delta.get("partial_json") or "")
            continue

        if event_name != "content_block_stop":
            continue

        index = payload.get("index")
        if not isinstance(index, int):
            continue
        state = block_state.pop(index, None)
        if not isinstance(state, dict):
            continue

        target = TargetRef(kind="model", name=model_name)
        block_type = state.get("type")
        if block_type == "text":
            text = str(state.get("text") or "").strip()
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
                        content={"block_index": index, "text": text},
                        visibility=visibility,
                    )
                )
        elif block_type in {"thinking", "redacted_thinking"}:
            reasoning_text = str(state.get("reasoning") or "").strip()
            content = {"block_index": index}
            if reasoning_text:
                content["text"] = reasoning_text
            if state.get("has_encrypted_content"):
                content["has_encrypted_content"] = True
            if content.get("text") or content.get("has_encrypted_content"):
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
        elif block_type == "tool_use":
            raw_arguments = str(state.get("arguments") or "")
            arguments = _best_effort_arguments(raw_arguments)
            tool_name = state.get("tool_name")
            tool_call_id = state.get("tool_call_id")
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
                        "block_index": index,
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
    return _emit_anthropic_message_events(
        session_id=session_id,
        platform=platform,
        trace_id=trace_id,
        parent_event_id=parent_event_id,
        role="assistant",
        content=payload.get("content"),
        target=TargetRef(kind="model", name=payload.get("model")),
        visibility=Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False),
        text_event_type="assistant_text_final",
        tool_result_event_type="tool_call_result",
    )


def canonicalize_claude_sdk_message(
    payload: dict[str, Any],
    *,
    session_id: str,
    trace_id: str | None = None,
    parent_event_id: str | None = None,
) -> list[Event]:
    message_type = payload.get("type")
    message = payload.get("message")
    if message_type not in {"assistant", "user"} or not isinstance(message, dict):
        return []

    role = str(message.get("role") or message_type)
    text_event_type = "assistant_text_final" if role == "assistant" else "request_user_message"
    tool_result_event_type = "tool_call_result" if role == "user" else "tool_call_result_attached"
    return _emit_anthropic_message_events(
        session_id=session_id,
        platform="claude_code",
        trace_id=trace_id,
        parent_event_id=parent_event_id,
        role=role,
        content=message.get("content"),
        target=TargetRef(kind="agent", name="claude_code"),
        visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=True),
        text_event_type=text_event_type,
        tool_result_event_type=tool_result_event_type,
    )


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
