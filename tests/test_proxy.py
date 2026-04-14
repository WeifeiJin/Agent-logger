from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.proxy import _append_canonical_events
from agent_logger.schema import ActorRef, TargetRef, Visibility, make_event
from agent_logger.store import SessionStore


class ProxyTest(unittest.TestCase):
    def test_append_canonical_events_dedupes_subagent_tool_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), "sess_proxy")
            first = make_event(
                "sess_proxy",
                "subagent_spawned",
                actor=ActorRef(kind="runtime", id="subagent_runtime"),
                target=TargetRef(kind="agent", name="agent_1"),
                content={"tool_call_id": "call_1", "agent_id": "agent_1"},
                visibility=Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False),
            )
            duplicate = make_event(
                "sess_proxy",
                "subagent_spawned",
                actor=ActorRef(kind="runtime", id="subagent_runtime"),
                target=TargetRef(kind="agent", name="agent_1"),
                content={"tool_call_id": "call_1", "agent_id": "agent_1"},
                visibility=Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False),
            )
            dedupe_keys: set[tuple[str, str, str]] = set()
            _append_canonical_events(store, dedupe_keys, [first, duplicate])

            lines = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["event_type"], "subagent_spawned")


if __name__ == "__main__":
    unittest.main()
