from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_logger.context import collect_session_context


class ContextTest(unittest.TestCase):
    def test_collect_session_context_handles_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = collect_session_context(Path(tmp))
            self.assertEqual(snapshot["cwd"], str(Path(tmp).resolve()))
            self.assertIsNone(snapshot["repo_root"])
            self.assertEqual(snapshot["git_status"], [])
            self.assertIn("hostname_hash", snapshot)


if __name__ == "__main__":
    unittest.main()

