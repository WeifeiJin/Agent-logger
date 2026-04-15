from __future__ import annotations

import unittest

from agent_logger.canonicalize import (
    canonicalize_request,
    canonicalize_response,
    canonicalize_response_stream,
    canonicalize_claude_sdk_message,
    parse_sse_events,
)


class CanonicalizeTest(unittest.TestCase):
    def test_openai_request_and_response(self) -> None:
        request_events = canonicalize_request(
            {
                "model": "gpt-test",
                "messages": [
                    {"role": "system", "content": "be careful"},
                    {"role": "user", "content": "run tests"},
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "name": "exec_command",
                        "content": "ok",
                    },
                ],
            },
            session_id="sess_1",
            platform="codex",
            parent_event_id="evt_request",
        )
        self.assertEqual(
            [event.event_type for event in request_events],
            ["request_system_message", "request_user_message", "tool_call_result_attached"],
        )

        response_events = canonicalize_response(
            {
                "model": "gpt-test",
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": "{\"cmd\": \"pytest\"}",
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
            session_id="sess_1",
            platform="codex",
            parent_event_id="evt_response",
        )
        self.assertEqual(
            [event.event_type for event in response_events],
            ["assistant_text_final", "tool_call_requested"],
        )
        self.assertEqual(response_events[1].content["arguments"]["cmd"], "pytest")

    def test_anthropic_response(self) -> None:
        events = canonicalize_response(
            {
                "model": "claude-test",
                "content": [
                    {"type": "thinking", "thinking": "inspect repo"},
                    {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}},
                    {"type": "text", "text": "done"},
                ],
            },
            session_id="sess_2",
            platform="claude_code",
            parent_event_id="evt_response",
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["assistant_reasoning_final", "tool_call_requested", "assistant_text_final"],
        )

    def test_anthropic_request_with_tool_result(self) -> None:
        events = canonicalize_request(
            {
                "anthropic-version": "2023-06-01",
                "model": "claude-test",
                "system": [{"type": "text", "text": "stay scoped"}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "list files"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "inspect repo"},
                            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file_a\nfile_b"},
                        ],
                    },
                ],
            },
            session_id="sess_anthropic_req",
            platform="claude_code",
            parent_event_id="evt_req",
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                "request_system_message",
                "request_user_message",
                "assistant_reasoning_final",
                "tool_call_requested",
                "tool_call_result_attached",
            ],
        )
        self.assertEqual(events[3].content["tool_name"], "bash")
        self.assertEqual(events[4].content["tool_call_id"], "toolu_1")

    def test_responses_sse_stream(self) -> None:
        stream = (
            "event: response.output_text.delta\n"
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"OK\",\"item_id\":\"msg_1\",\"output_index\":0,\"content_index\":0}\n\n"
            "event: response.output_text.done\n"
            "data: {\"type\":\"response.output_text.done\",\"text\":\"OK\",\"item_id\":\"msg_1\",\"output_index\":0,\"content_index\":0}\n\n"
            "event: response.output_item.done\n"
            "data: {\"type\":\"response.output_item.done\",\"item\":{\"id\":\"call_1\",\"type\":\"function_call\",\"name\":\"exec_command\",\"arguments\":\"{\\\"cmd\\\":\\\"pytest\\\"}\"}}\n\n"
        )
        parsed = parse_sse_events(stream)
        self.assertEqual(parsed[0]["event"], "response.output_text.delta")

        events = canonicalize_response_stream(
            stream,
            session_id="sess_stream",
            platform="codex",
            parent_event_id="evt_stream",
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["assistant_text_delta", "assistant_text_final", "tool_call_requested"],
        )
        self.assertEqual(events[2].content["arguments"]["cmd"], "pytest")

    def test_anthropic_response_stream(self) -> None:
        stream = (
            "event: message_start\n"
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_1\",\"model\":\"claude-test\"}}\n\n"
            "event: content_block_start\n"
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"thinking\",\"thinking\":\"inspect\"}}\n\n"
            "event: content_block_stop\n"
            "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
            "event: content_block_start\n"
            "data: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"bash\"}}\n\n"
            "event: content_block_delta\n"
            "data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"cmd\\\":\\\"ls\\\"}\"}}\n\n"
            "event: content_block_stop\n"
            "data: {\"type\":\"content_block_stop\",\"index\":1}\n\n"
            "event: content_block_start\n"
            "data: {\"type\":\"content_block_start\",\"index\":2,\"content_block\":{\"type\":\"text\",\"text\":\"done\"}}\n\n"
            "event: content_block_stop\n"
            "data: {\"type\":\"content_block_stop\",\"index\":2}\n\n"
        )
        events = canonicalize_response_stream(
            stream,
            session_id="sess_anthropic_stream",
            platform="claude_code",
            parent_event_id="evt_stream",
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["assistant_reasoning_final", "tool_call_requested", "assistant_text_final"],
        )
        self.assertEqual(events[1].content["arguments"]["cmd"], "ls")

    def test_canonicalize_claude_sdk_message(self) -> None:
        events = canonicalize_claude_sdk_message(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "inspect"},
                        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "pwd"}},
                        {"type": "text", "text": "done"},
                    ],
                },
            },
            session_id="sess_claude_sdk",
            parent_event_id="evt_sdk",
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["assistant_reasoning_final", "tool_call_requested", "assistant_text_final"],
        )
        tool_result_events = canonicalize_claude_sdk_message(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "pwd-output"},
                    ],
                },
            },
            session_id="sess_claude_sdk",
            parent_event_id="evt_sdk_result",
        )
        self.assertEqual([event.event_type for event in tool_result_events], ["tool_call_result"])
        self.assertEqual(tool_result_events[0].content["tool_call_id"], "toolu_1")

    def test_responses_request_with_tool_result(self) -> None:
        events = canonicalize_request(
            {
                "model": "gpt-test",
                "instructions": "be careful",
                "input": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "system-ish"}],
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run pwd"}],
                    },
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call_1",
                        "arguments": "{\"cmd\":\"pwd\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "ok",
                    },
                ],
            },
            session_id="sess_responses_req",
            platform="codex",
            parent_event_id="evt_req",
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                "request_system_message",
                "request_system_message",
                "request_user_message",
                "tool_call_dispatched",
                "tool_call_result",
            ],
        )
        self.assertEqual(events[3].content["arguments"]["cmd"], "pwd")
        self.assertEqual(events[4].content["tool_call_id"], "call_1")

    def test_subagent_events_in_responses_request(self) -> None:
        events = canonicalize_request(
            {
                "model": "gpt-test",
                "input": [
                    {
                        "type": "function_call",
                        "name": "send_input",
                        "call_id": "call_send",
                        "arguments": "{\"target\":\"agent_1\",\"message\":\"check this\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_send",
                        "output": "{\"queued\":true}",
                    },
                    {
                        "type": "function_call",
                        "name": "wait_agent",
                        "call_id": "call_wait",
                        "arguments": "{\"targets\":[\"agent_1\"],\"timeout_ms\":1000}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": "{\"id\":\"agent_1\",\"status\":\"completed\",\"final_message\":\"done\"}",
                    },
                    {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": "call_spawn",
                        "arguments": "{\"agent_type\":\"worker\",\"message\":\"fix tests\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": "{\"id\":\"agent_2\",\"nickname\":\"Worker 2\"}",
                    },
                ],
            },
            session_id="sess_subagent_req",
            platform="codex",
            parent_event_id="evt_req",
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                "tool_call_dispatched",
                "subagent_message",
                "tool_call_result",
                "subagent_message",
                "tool_call_dispatched",
                "tool_call_result",
                "subagent_result",
                "tool_call_dispatched",
                "tool_call_result",
                "subagent_spawned",
            ],
        )
        self.assertEqual(events[1].content["delivery_state"], "dispatched")
        self.assertEqual(events[3].content["delivery_state"], "acknowledged")
        self.assertEqual(events[6].content["targets"], ["agent_1"])
        self.assertEqual(events[9].content["agent_id"], "agent_2")

    def test_subagent_spawn_requested_in_stream(self) -> None:
        stream = (
            "event: response.output_item.done\n"
            "data: {\"type\":\"response.output_item.done\",\"item\":{\"id\":\"call_1\",\"type\":\"function_call\",\"name\":\"spawn_agent\",\"arguments\":\"{\\\"agent_type\\\":\\\"worker\\\",\\\"message\\\":\\\"investigate\\\"}\"}}\n\n"
        )
        events = canonicalize_response_stream(
            stream,
            session_id="sess_subagent_stream",
            platform="codex",
            parent_event_id="evt_stream",
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["tool_call_requested", "subagent_spawn_requested"],
        )
        self.assertEqual(events[1].content["agent_type"], "worker")
        self.assertEqual(events[1].content["message"], "investigate")


if __name__ == "__main__":
    unittest.main()
