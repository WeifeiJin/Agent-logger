from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.codex_rollout import (
    RolloutCursorState,
    canonicalize_rollout_delta,
    canonicalize_rollout_entries,
    find_rollout_paths,
    read_rollout_entries,
)


class CodexRolloutTest(unittest.TestCase):
    def test_find_rollout_paths_by_thread_id_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp)
            explicit = sessions_root / "2026" / "04" / "13" / "rollout-2026-04-13T23-13-23-thread_1.jsonl"
            fallback = sessions_root / "2026" / "04" / "13" / "rollout-2026-04-13T23-15-23-thread_2.jsonl"
            explicit.parent.mkdir(parents=True, exist_ok=True)

            explicit.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-13T15:13:23.174Z",
                        "type": "session_meta",
                        "payload": {"id": "thread_1", "cwd": "/repo"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fallback.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-13T15:15:23.174Z",
                        "type": "session_meta",
                        "payload": {"id": "thread_2", "cwd": "/repo"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                find_rollout_paths(sessions_root, thread_ids=["thread_1"]),
                [explicit],
            )
            self.assertEqual(
                find_rollout_paths(
                    sessions_root,
                    thread_ids=["thread_1"],
                    cwd="/repo",
                    started_at_epoch=1776093300,
                ),
                [explicit],
            )
            self.assertEqual(
                find_rollout_paths(
                    sessions_root,
                    thread_ids=[],
                    cwd="/repo",
                    started_at_epoch=1776093300,
                ),
                [fallback],
            )

    def test_read_rollout_entries_tolerates_non_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_bytes(
                b'{"timestamp":"2026-04-13T15:13:23.174Z","type":"session_meta","payload":{"id":"thread_1","cwd":"'
                + bytes([0x96])
                + b'"}}\n'
            )
            entries = read_rollout_entries(path)
            self.assertEqual(entries[0]["type"], "session_meta")

    def test_canonicalize_rollout_entries_imports_runtime_and_custom_tool_events(self) -> None:
        entries = [
            {
                "timestamp": "2026-04-13T15:13:23.174Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread_1",
                    "cwd": "/repo",
                    "originator": "codex_exec",
                    "cli_version": "0.120.0",
                    "source": "exec",
                    "model_provider": "asg_proxy",
                    "base_instructions": {"text": "base"},
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.175Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn_1",
                    "cwd": "/repo",
                    "model": "gpt-5.4",
                    "approval_policy": "never",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.176Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn_1",
                    "started_at": 1776093203,
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.177Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "run pwd",
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.178Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                    "status": "completed",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.179Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "call_patch",
                    "turn_id": "turn_1",
                    "stdout": "Success",
                    "stderr": "",
                    "success": True,
                    "changes": {"/repo/a.txt": {"type": "add"}},
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.180Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_patch",
                    "output": "Success",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.181Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "working",
                    "phase": "commentary",
                    "memory_citation": None,
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.182Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn_1",
                    "last_agent_message": "done",
                    "completed_at": 1776093204,
                    "duration_ms": 123,
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.183Z",
                "type": "event_msg",
                "payload": {"type": "context_compacted"},
            },
        ]

        events = canonicalize_rollout_entries(
            entries,
            session_id="sess_rollout",
            trace_id="trace_1",
            rollout_path="/tmp/rollout.jsonl",
        )

        event_types = [event.event_type for event in events]
        self.assertIn("codex_session_meta", event_types)
        self.assertIn("codex_turn_context", event_types)
        self.assertIn("codex_task_started", event_types)
        self.assertIn("user_input", event_types)
        self.assertIn("tool_call_requested", event_types)
        self.assertIn("tool_call_dispatched", event_types)
        self.assertIn("tool_call_stdout", event_types)
        self.assertIn("tool_call_result", event_types)
        self.assertIn("assistant_text_final", event_types)
        self.assertIn("final_output", event_types)
        self.assertIn("codex_task_complete", event_types)
        self.assertIn("codex_context_compacted", event_types)

    def test_canonicalize_rollout_entries_backfills_provider_items(self) -> None:
        entries = [
            {
                "timestamp": "2026-04-13T15:13:23.174Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn_1", "model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-13T15:13:23.175Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "rules"}],
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.176Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "run pwd"}],
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.177Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "output_text", "text": "inspect repo"}],
                    "encrypted_content": "opaque",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.178Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "exec_command",
                    "arguments": "{\"cmd\":\"pwd\"}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.179Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "/repo",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.180Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
        ]

        events = canonicalize_rollout_entries(
            entries,
            session_id="sess_rollout",
            trace_id="trace_1",
            include_request_backfill=True,
            include_response_backfill=True,
        )

        self.assertEqual(
            [event.event_type for event in events],
            [
                "codex_turn_context",
                "request_system_message",
                "request_user_message",
                "assistant_reasoning_final",
                "tool_call_requested",
                "tool_call_dispatched",
                "tool_call_result",
                "assistant_text_final",
            ],
        )
        self.assertEqual(events[4].content["arguments"]["cmd"], "pwd")
        self.assertEqual(events[6].content["tool_name"], "exec_command")

    def test_canonicalize_rollout_entries_backfills_subagent_events(self) -> None:
        entries = [
            {
                "timestamp": "2026-04-13T15:13:23.174Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn_1", "model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-13T15:13:23.175Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_spawn",
                    "name": "spawn_agent",
                    "arguments": "{\"agent_type\":\"worker\",\"message\":\"fix tests\"}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.176Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_spawn",
                    "output": "{\"id\":\"agent_2\",\"nickname\":\"Worker 2\"}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.177Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_send",
                    "name": "send_input",
                    "arguments": "{\"target\":\"agent_2\",\"message\":\"continue\"}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.178Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_send",
                    "output": "{\"queued\":true}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.179Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_wait",
                    "name": "wait_agent",
                    "arguments": "{\"targets\":[\"agent_2\"],\"timeout_ms\":1000}",
                },
            },
            {
                "timestamp": "2026-04-13T15:13:23.180Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_wait",
                    "output": "{\"id\":\"agent_2\",\"status\":\"completed\"}",
                },
            },
        ]

        events = canonicalize_rollout_entries(
            entries,
            session_id="sess_rollout_subagent",
            trace_id="trace_1",
            include_request_backfill=True,
            include_response_backfill=True,
        )
        event_types = [event.event_type for event in events]
        self.assertIn("subagent_spawn_requested", event_types)
        self.assertIn("subagent_spawned", event_types)
        self.assertIn("subagent_message", event_types)
        self.assertIn("subagent_result", event_types)

    def test_canonicalize_rollout_delta_preserves_tool_state_between_chunks(self) -> None:
        first_entries = [
            {
                "timestamp": "2026-04-13T15:13:23.174Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn_1", "model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-13T15:13:23.175Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "exec_command",
                    "arguments": "{\"cmd\":\"pwd\"}",
                },
            },
        ]
        second_entries = [
            {
                "timestamp": "2026-04-13T15:13:23.176Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "/repo",
                },
            },
        ]

        first_events, state = canonicalize_rollout_delta(
            first_entries,
            session_id="sess_rollout",
            trace_id="trace_1",
            include_request_backfill=True,
            include_response_backfill=True,
        )
        second_events, next_state = canonicalize_rollout_delta(
            second_entries,
            session_id="sess_rollout",
            trace_id="trace_1",
            include_request_backfill=True,
            include_response_backfill=True,
            state=state,
            entry_index_offset=len(first_entries),
        )

        self.assertIsInstance(state, RolloutCursorState)
        self.assertEqual(first_events[-1].event_type, "tool_call_dispatched")
        self.assertEqual(second_events[0].event_type, "tool_call_result")
        self.assertEqual(second_events[0].content["tool_name"], "exec_command")
        self.assertEqual(next_state.tool_name_by_call_id["call_1"], "exec_command")


if __name__ == "__main__":
    unittest.main()
