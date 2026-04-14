from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from agent_logger.render import build_session_report, generate_session_report_artifact, resolve_session_dir
from agent_logger.schema import ActorRef, TargetRef, make_event
from agent_logger.store import SessionStore


class RenderTest(unittest.TestCase):
    def test_build_session_report_hides_noisy_events_and_keeps_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root, 'sess_render')
            store.write_manifest(
                {
                    'session_id': 'sess_render',
                    'agent': 'codex',
                    'cwd': '/repo',
                    'command': ['codex', '-s', 'workspace-write'],
                    'provider': 'rightcode',
                    'codex_thread_ids': ['thread_1'],
                }
            )
            store.append_event(make_event('sess_render', 'session_started', content={'cwd': '/repo'}))
            store.append_event(make_event('sess_render', 'tty_output_chunk', content={'text': '\x1b[6n'}))
            store.append_event(make_event('sess_render', 'user_input', content={'text': 'run pwd'}))
            store.append_event(make_event('sess_render', 'tool_call_requested', content={'tool_name': 'exec_command', 'arguments': {'cmd': 'pwd'}}))
            store.append_event(make_event('sess_render', 'tool_call_result', content={'tool_name': 'exec_command', 'output': '/repo'}))
            store.append_event(make_event('sess_render', 'assistant_text_final', content={'text': '/repo'}))
            report = build_session_report(session_dir=store.session_dir)
            self.assertIn('# Session Report: sess_render', report)
            self.assertIn('- tty_output_chunk: 1 (hidden by default)', report)
            self.assertIn('User: run pwd', report)
            self.assertIn('Tool requested: exec_command {"cmd": "pwd"}', report)
            self.assertIn('Assistant: /repo', report)
            self.assertNotIn('\x1b[6n', report)

    def test_build_session_report_dedupes_repeated_request_history_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), 'sess_render_dedupe')
            store.write_manifest({'session_id': 'sess_render_dedupe', 'agent': 'codex', 'cwd': '/repo'})
            store.append_event(make_event('sess_render_dedupe', 'request_user_message', content={'text': 'same prompt'}))
            store.append_event(make_event('sess_render_dedupe', 'assistant_reasoning_final', content={'has_encrypted_content': True}))
            store.append_event(make_event('sess_render_dedupe', 'request_user_message', content={'text': 'same prompt'}))
            store.append_event(make_event('sess_render_dedupe', 'assistant_reasoning_final', content={'has_encrypted_content': True}))
            report = build_session_report(session_dir=store.session_dir)
            self.assertEqual(report.count('User: same prompt'), 1)
            self.assertEqual(report.count('Reasoning: [encrypted reasoning content present]'), 1)

    def test_generate_session_report_artifact_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp), 'sess_report_artifact')
            store.write_manifest({'session_id': 'sess_report_artifact', 'agent': 'codex', 'cwd': '/repo'})
            store.append_event(make_event('sess_report_artifact', 'user_input', content={'text': 'hello'}))
            rel = generate_session_report_artifact(store)
            self.assertEqual(rel, 'artifacts/session_report.md')
            report_text = (store.session_dir / rel).read_text(encoding='utf-8')
            self.assertIn('Session Report', report_text)
            self.assertIn('User: hello', report_text)
            events = [json.loads(line) for line in store.events_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            self.assertEqual(events[-1]['event_type'], 'session_report_generated')

    def test_resolve_session_dir_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            SessionStore(root, 'sess_old')
            latest = SessionStore(root, 'sess_latest')
            resolved = resolve_session_dir(root=root, latest=True)
            self.assertEqual(resolved, latest.session_dir)


if __name__ == '__main__':
    unittest.main()
