from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.codex_monitor import prime_codex_monitor_state, reconcile_codex_runtime
from agent_logger.store import SessionStore


class CodexMonitorTest(unittest.TestCase):
    def test_reconcile_codex_runtime_imports_rollout_and_history_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "asg"
            codex_home = Path(tmp) / "codex_home"
            history_path = codex_home / "history.jsonl"
            rollout_path = codex_home / "sessions" / "2026" / "04" / "14" / "rollout-2026-04-14T12-00-00-thread_1.jsonl"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            rollout_path.parent.mkdir(parents=True, exist_ok=True)

            history_path.write_text(
                json.dumps({"session_id": "thread_1", "ts": 1776168001, "text": "run pwd"}) + "\n",
                encoding="utf-8",
            )
            rollout_entries = [
                {
                    "timestamp": "2026-04-14T12:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "thread_1", "cwd": "/repo"},
                },
                {
                    "timestamp": "2026-04-14T12:00:01.000Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn_1", "model": "gpt-5.4", "cwd": "/repo"},
                },
                {
                    "timestamp": "2026-04-14T12:00:02.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"pwd\"}",
                    },
                },
            ]
            rollout_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rollout_entries),
                encoding="utf-8",
            )

            store = SessionStore(root, "sess_live")
            store.write_manifest({"session_id": "sess_live", "agent": "codex", "cwd": "/repo"})
            prime_codex_monitor_state(store, initial_history_offset=0)

            summary = reconcile_codex_runtime(
                store,
                session_id="sess_live",
                trace_id="trace_live",
                codex_home=codex_home,
                history_path=history_path,
                cwd=Path("/repo"),
                started_at_epoch=1776168000,
                initial_history_offset=0,
                final=False,
            )

            event_rows = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_types = [row["event_type"] for row in event_rows]
            self.assertIn("codex_session_meta", event_types)
            self.assertIn("tool_call_requested", event_types)
            self.assertIn("user_input", event_types)
            self.assertNotIn("authz_cases_generated", event_types)
            self.assertTrue((store.session_dir / "artifacts" / "authz_cases.jsonl").exists())
            self.assertTrue((store.session_dir / "artifacts" / "authz_review.md").exists())
            self.assertEqual(summary["thread_ids"], ["thread_1"])
            self.assertEqual(summary["history_entries_imported"], 1)
            self.assertEqual(summary["rollout_entries_imported"], 3)
            self.assertTrue(summary["authz_refreshed"])

    def test_reconcile_codex_runtime_final_appends_generation_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "asg"
            codex_home = Path(tmp) / "codex_home"
            history_path = codex_home / "history.jsonl"
            rollout_path = codex_home / "sessions" / "2026" / "04" / "14" / "rollout-2026-04-14T12-00-00-thread_1.jsonl"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            rollout_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text("", encoding="utf-8")
            rollout_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T12:00:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call_patch",
                            "name": "apply_patch",
                            "input": "*** Begin Patch",
                            "status": "completed",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            store = SessionStore(root, "sess_final")
            store.write_manifest({"session_id": "sess_final", "agent": "codex", "cwd": "/repo"})

            reconcile_codex_runtime(
                store,
                session_id="sess_final",
                trace_id="trace_final",
                codex_home=codex_home,
                history_path=history_path,
                cwd=Path("/repo"),
                started_at_epoch=1776168000,
                initial_history_offset=0,
                final=True,
            )

            event_rows = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_types = [row["event_type"] for row in event_rows]
            self.assertIn("authz_cases_generated", event_types)
            self.assertIn("codex_runtime_reconciled", event_types)


if __name__ == "__main__":
    unittest.main()
