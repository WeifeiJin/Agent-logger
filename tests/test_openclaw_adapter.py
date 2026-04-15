from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.openclaw_adapter import (
    OpenClawProxyConfig,
    _canonicalize_openclaw_cache_trace_row,
    _write_openclaw_overlay_config,
)


class OpenClawAdapterTest(unittest.TestCase):
    def test_write_openclaw_overlay_config_with_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_config_path = root / "openclaw.base.json"
            base_config_path.write_text("{\"agents\":{\"defaults\":{}}}\n", encoding="utf-8")
            overlay_config_path = root / "overlay" / "openclaw.json"
            cache_trace_path = root / "overlay" / "cache-trace.jsonl"

            _write_openclaw_overlay_config(
                overlay_config_path=overlay_config_path,
                base_config_path=base_config_path,
                cache_trace_path=cache_trace_path,
                proxy_url="http://127.0.0.1:8787",
                proxy=OpenClawProxyConfig(
                    upstream_url="https://api.anthropic.com/v1",
                    provider_api="anthropic-messages",
                    model_id="claude-sonnet-4-5",
                    provider_id="agent_logger_proxy",
                    api_key_env="ANTHROPIC_API_KEY",
                ),
            )
            payload = json.loads(overlay_config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["$include"], "./base.openclaw.json")
            self.assertTrue(payload["diagnostics"]["cacheTrace"]["enabled"])
            self.assertEqual(
                payload["models"]["providers"]["agent_logger_proxy"]["baseUrl"],
                "http://127.0.0.1:8787",
            )
            self.assertEqual(
                payload["agents"]["defaults"]["model"]["primary"],
                "agent_logger_proxy/claude-sonnet-4-5",
            )

    def test_canonicalize_openclaw_cache_trace_row(self) -> None:
        events = _canonicalize_openclaw_cache_trace_row(
            {
                "model": "claude-test",
                "system": [{"type": "text", "text": "stay scoped"}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "run pwd"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "pwd"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "/repo"},
                        ],
                    },
                ],
            },
            session_id="sess_openclaw",
            trace_id="trace_openclaw",
            parent_event_id="evt_openclaw",
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                "request_system_message",
                "request_user_message",
                "tool_call_requested",
                "tool_call_result_attached",
            ],
        )
        self.assertEqual(events[2].platform, "openclaw")
        self.assertEqual(events[2].platform_metadata["source"], "openclaw_cache_trace.messages")


if __name__ == "__main__":
    unittest.main()
