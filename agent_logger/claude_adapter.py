from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import threading
import time
from typing import Any

from .canonicalize import canonicalize_claude_sdk_message
from .launcher import LaunchConfig, LaunchResult, launch_session
from .render import generate_session_report_artifact
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


@dataclass(slots=True)
class ClaudeCommandPlan:
    command: list[str]
    headless: bool
    structured_stream: bool
    output_format: str | None


def _extract_option_value(args: list[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def build_claude_command_plan(
    user_args: list[str],
    *,
    structured_output: str = "auto",
) -> ClaudeCommandPlan:
    if structured_output not in {"auto", "on", "off"}:
        raise ValueError("structured_output must be one of: auto, on, off")

    args = list(user_args)
    headless = any(arg in {"-p", "--print"} for arg in args)
    output_format = _extract_option_value(args, "--output-format")

    if structured_output in {"auto", "on"} and headless and output_format is None:
        args.extend(["--output-format", "stream-json"])
        output_format = "stream-json"

    structured_stream = headless and output_format == "stream-json"
    return ClaudeCommandPlan(
        command=["claude", *args],
        headless=headless,
        structured_stream=structured_stream and structured_output != "off",
        output_format=output_format,
    )


def _transcript_stdout_path(store: SessionStore) -> Path:
    return store.artifact_path("tty.stdout.log")


class ClaudeTranscriptMonitor:
    def __init__(
        self,
        *,
        store: SessionStore,
        session_id: str,
        trace_id: str,
        enabled: bool,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.trace_id = trace_id
        self.enabled = enabled
        self.transcript_path = _transcript_stdout_path(store)
        self._offset = 0
        self._buffer = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._drain_once()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._drain_once()
            self._stop_event.wait(0.25)

    def _drain_once(self) -> None:
        if not self.transcript_path.exists():
            return
        with self.transcript_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if self._offset > size:
                self._offset = 0
                self._buffer = ""
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        if not chunk:
            return

        text = self._buffer + chunk.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        self._buffer = ""
        for line in lines:
            if line.endswith("\n") or line.endswith("\r"):
                self._handle_line(line)
            else:
                self._buffer = line

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        self._append_payload(payload)

    def _append_payload(self, payload: dict[str, Any]) -> None:
        payload_type = payload.get("type")
        if payload_type in {"assistant", "user"}:
            for event in canonicalize_claude_sdk_message(
                payload,
                session_id=self.session_id,
                trace_id=self.trace_id,
            ):
                self.store.append_event(event)
            return

        if payload_type == "system":
            subtype = payload.get("subtype")
            content = {key: value for key, value in payload.items() if key != "type"}
            event_type = "claude_system_message"
            if subtype == "init":
                event_type = "claude_session_init"
            self.store.append_event(
                make_event(
                    self.session_id,
                    event_type,
                    trace_id=self.trace_id,
                    platform="claude_code",
                    actor=ActorRef(kind="runtime", id="claude_sdk"),
                    target=TargetRef(kind="agent", name="claude_code"),
                    content=content,
                    visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                )
            )
            return

        result_text = payload.get("result")
        if isinstance(result_text, str) and result_text.strip():
            self.store.append_event(
                make_event(
                    self.session_id,
                    "final_output",
                    trace_id=self.trace_id,
                    platform="claude_code",
                    actor=ActorRef(kind="agent", id="claude_code"),
                    target=TargetRef(kind="user", name="terminal"),
                    content={"text": result_text},
                    visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=True),
                )
            )


def run_claude_session(
    *,
    root: Path,
    cwd: Path,
    user_args: list[str],
    structured_output: str = "auto",
) -> LaunchResult:
    plan = build_claude_command_plan(user_args, structured_output=structured_output)
    monitor: ClaudeTranscriptMonitor | None = None

    def _on_session_ready(store: SessionStore, session_id: str, trace_id: str, _command: list[str]) -> None:
        nonlocal monitor
        monitor = ClaudeTranscriptMonitor(
            store=store,
            session_id=session_id,
            trace_id=trace_id,
            enabled=plan.structured_stream,
        )
        store.append_event(
            make_event(
                session_id,
                "claude_capture_mode",
                trace_id=trace_id,
                platform="claude_code",
                actor=ActorRef(kind="runtime", id="claude_adapter"),
                target=TargetRef(kind="agent", name="claude_code"),
                content={
                    "headless": plan.headless,
                    "structured_stream": plan.structured_stream,
                    "output_format": plan.output_format,
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )
        if monitor is not None:
            monitor.start()

    def _on_session_finished(store: SessionStore, session_id: str, trace_id: str, _exit_code: int) -> None:
        nonlocal monitor
        if monitor is not None:
            monitor.stop()
            monitor = None
        generate_session_report_artifact(store)

    return launch_session(
        LaunchConfig(
            agent="claude_code",
            command=plan.command,
            root=root,
            cwd=cwd,
            provider="anthropic",
            on_session_ready=_on_session_ready,
            on_session_finished=_on_session_finished,
        )
    )
