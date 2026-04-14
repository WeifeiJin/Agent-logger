from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent_logger.schema import ActorRef, make_event
from agent_logger.store import SessionStore


class SessionStoreTest(unittest.TestCase):
    def test_store_creates_layout_and_writes_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), "sess_test")
            store.write_manifest({"session_id": "sess_test"})
            event = make_event(
                "sess_test",
                "session_started",
                actor=ActorRef(kind="runtime", id="test"),
                content={"hello": "world"},
            )
            store.append_event(event)

            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["session_id"], "sess_test")

            lines = store.events_path.read_text(encoding="utf-8").strip().splitlines()
            payload = json.loads(lines[0])
            self.assertEqual(payload["event_type"], "session_started")
            self.assertEqual(payload["content"]["hello"], "world")

    def test_artifact_helpers_return_session_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), "sess_artifacts")
            rel = store.write_text_artifact("example.txt", "abc")
            self.assertEqual(rel, "artifacts/example.txt")
            self.assertEqual(
                (store.session_dir / rel).read_text(encoding="utf-8"),
                "abc",
            )


if __name__ == "__main__":
    unittest.main()

