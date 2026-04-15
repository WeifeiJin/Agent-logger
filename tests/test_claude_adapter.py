from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.claude_adapter import ClaudeTranscriptMonitor, build_claude_command_plan
from agent_logger.store import SessionStore


class ClaudeAdapterTest(unittest.TestCase):
    def test_build_claude_command_plan_auto_enables_stream_json_for_headless(self) -> None:
        plan = build_claude_command_plan(["-p", "Reply with exactly OK."])
        self.assertEqual(
            plan.command,
            ["claude", "-p", "Reply with exactly OK.", "--output-format", "stream-json"],
        )
        self.assertTrue(plan.headless)
        self.assertTrue(plan.structured_stream)
        self.assertEqual(plan.output_format, "stream-json")

    def test_build_claude_command_plan_respects_explicit_output_format(self) -> None:
        plan = build_claude_command_plan(
            ["-p", "Reply with exactly OK.", "--output-format", "text"],
            structured_output="on",
        )
        self.assertFalse(plan.structured_stream)
        self.assertEqual(plan.output_format, "text")

    def test_claude_transcript_monitor_appends_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), "sess_claude")
            monitor = ClaudeTranscriptMonitor(
                store=store,
                session_id="sess_claude",
                trace_id="trace_claude",
                enabled=True,
            )
            monitor._append_payload(  # noqa: SLF001
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "pwd"}},
                            {"type": "text", "text": "done"},
                        ],
                    },
                }
            )
            monitor._append_payload(  # noqa: SLF001
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "/repo"},
                        ],
                    },
                }
            )
            monitor._append_payload(  # noqa: SLF001
                {
                    "type": "result",
                    "result": "final answer",
                }
            )
            events = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [event["event_type"] for event in events],
                ["tool_call_requested", "assistant_text_final", "tool_call_result", "final_output"],
            )


if __name__ == "__main__":
    unittest.main()
