from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.codex_adapter import (
    CodexPaths,
    CodexSnapshot,
    build_local_proxy_bypass_env,
    build_codex_provider_override_args,
    extract_codex_thread_ids_from_session_events,
    filter_history_entries,
    get_active_provider_config,
    read_appended_history,
)
from agent_logger.store import SessionStore


class CodexAdapterTest(unittest.TestCase):
    def test_get_active_provider_config(self) -> None:
        name, provider = get_active_provider_config(
            {
                "model_provider": "rightcode",
                "model_providers": {
                    "rightcode": {
                        "name": "rightcode",
                        "base_url": "https://right.codes/codex/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": True,
                    }
                },
            }
        )
        self.assertEqual(name, "rightcode")
        self.assertEqual(provider["wire_api"], "responses")

    def test_build_codex_provider_override_args(self) -> None:
        args = build_codex_provider_override_args(
            proxy_url="http://127.0.0.1:9000",
            active_provider_name="rightcode",
            active_provider_config={
                "name": "rightcode",
                "base_url": "https://right.codes/codex/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
        )
        joined = " ".join(args)
        self.assertIn('model_provider="asg_proxy"', joined)
        self.assertIn('model_providers.asg_proxy.base_url="http://127.0.0.1:9000"', joined)
        self.assertIn("model_providers.asg_proxy.requires_openai_auth=true", joined)

    def test_read_appended_history_from_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            history_path = home / "history.jsonl"
            initial = {"session_id": "old", "ts": 1, "text": "before"}
            history_path.write_text(json.dumps(initial) + "\n", encoding="utf-8")
            snapshot = CodexSnapshot(history_size=history_path.stat().st_size, started_at_epoch=1)
            appended = [
                {"session_id": "new_1", "ts": 2, "text": "hello"},
                {"session_id": "new_1", "ts": 3, "text": "again"},
            ]
            with history_path.open("a", encoding="utf-8") as handle:
                for row in appended:
                    handle.write(json.dumps(row) + "\n")
            paths = CodexPaths(
                home=home,
                config_path=home / "config.toml",
                history_path=history_path,
                state_db_path=home / "state_5.sqlite",
                logs_db_path=home / "logs_2.sqlite",
            )
            result = read_appended_history(paths, snapshot)
            self.assertEqual(result, appended)

    def test_build_local_proxy_bypass_env(self) -> None:
        env = build_local_proxy_bypass_env({"NO_PROXY": ".example.com"})
        self.assertIn(".example.com", env["NO_PROXY"])
        self.assertIn("127.0.0.1", env["NO_PROXY"])
        self.assertIn("localhost", env["NO_PROXY"])
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])

    def test_extract_codex_thread_ids_from_session_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), "sess_extract")
            store.events_path.write_text(
                "\n".join(
                    [
                        json.dumps({"platform_metadata": {"codex_thread_id": "thread_1"}}),
                        json.dumps({"platform_metadata": {"codex_thread_id": "thread_2"}}),
                        json.dumps({"platform_metadata": {"codex_thread_id": "thread_1"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                extract_codex_thread_ids_from_session_events(store),
                ["thread_1", "thread_2"],
            )

    def test_filter_history_entries(self) -> None:
        entries = [
            {"session_id": "thread_1", "ts": 10, "text": "a"},
            {"session_id": "thread_2", "ts": 20, "text": "b"},
            {"session_id": "thread_3", "ts": 30, "text": "c"},
        ]
        self.assertEqual(
            filter_history_entries(entries, thread_ids=["thread_2", "thread_3"], started_at_epoch=15),
            [
                {"session_id": "thread_2", "ts": 20, "text": "b"},
                {"session_id": "thread_3", "ts": 30, "text": "c"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
