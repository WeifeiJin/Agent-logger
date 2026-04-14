from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
from pathlib import Path
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import tty
from typing import Callable

from .context import collect_session_context, context_ref
from .ids import make_session_id, make_trace_id
from .proxy import EmbeddedTraceProxy, ProxySettings
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


@dataclass(slots=True)
class LaunchConfig:
    agent: str
    command: list[str] | None
    root: Path
    cwd: Path
    provider: str | None = None
    upstream_url: str | None = None
    base_url_env: str | None = None
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 0
    env_overrides: dict[str, str] = field(default_factory=dict)
    command_builder: Callable[[str | None], list[str]] | None = None
    on_session_ready: Callable[[SessionStore, str, str, list[str]], None] | None = None
    on_session_finished: Callable[[SessionStore, str, str, int], None] | None = None


@dataclass(slots=True)
class LaunchResult:
    session_id: str
    trace_id: str
    exit_code: int
    session_dir: Path
    command: list[str]


def _prepare_transcript_paths(store: SessionStore) -> tuple[Path, str, Path, str]:
    stdin_path = store.artifact_path("tty.stdin.log")
    stdout_path = store.artifact_path("tty.stdout.log")
    return (
        stdin_path,
        store.session_relative(stdin_path),
        stdout_path,
        store.session_relative(stdout_path),
    )


_DEFAULT_WINSIZE = struct.pack("HHHH", 24, 80, 0, 0)


def _read_winsize(fd: int) -> bytes:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, _DEFAULT_WINSIZE)
    except OSError:
        return _DEFAULT_WINSIZE


def _sync_winsize(source_fd: int, target_fd: int) -> None:
    try:
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, _read_winsize(source_fd))
    except OSError:
        return


def _run_with_pty(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    store: SessionStore,
    session_id: str,
    platform: str,
    trace_id: str,
) -> tuple[int, list[str]]:
    stdin_path, _, stdout_path, _ = _prepare_transcript_paths(store)
    transcript_refs = [
        store.session_relative(stdin_path),
        store.session_relative(stdout_path),
    ]

    with stdin_path.open("ab") as stdin_file, stdout_path.open("ab") as stdout_file:
        pid, master_fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.execvpe(command[0], command, env)

        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        old_settings = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
        old_winch_handler = None
        if old_settings is not None:
            _sync_winsize(stdin_fd, master_fd)
            tty.setraw(stdin_fd)
            if hasattr(signal, "SIGWINCH"):
                old_winch_handler = signal.getsignal(signal.SIGWINCH)

                def _handle_winch(_signum: int, _frame: object | None) -> None:
                    _sync_winsize(stdin_fd, master_fd)

                signal.signal(signal.SIGWINCH, _handle_winch)

        try:
            while True:
                ready, _, _ = select.select([master_fd, stdin_fd], [], [])

                if master_fd in ready:
                    try:
                        output = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not output:
                        break
                    os.write(stdout_fd, output)
                    stdout_file.write(output)
                    stdout_file.flush()
                    store.append_event(
                        make_event(
                            session_id,
                            "tty_output_chunk",
                            trace_id=trace_id,
                            platform=platform,
                            actor=ActorRef(kind="agent", id=platform),
                            target=TargetRef(kind="user", name="terminal"),
                            content={"text": output.decode("utf-8", errors="replace")},
                            visibility=Visibility(
                                provider_exposed=False,
                                runtime_exposed=True,
                                user_visible=True,
                            ),
                        )
                    )

                if stdin_fd in ready:
                    incoming = os.read(stdin_fd, 4096)
                    if not incoming:
                        continue
                    os.write(master_fd, incoming)
                    stdin_file.write(incoming)
                    stdin_file.flush()
                    store.append_event(
                        make_event(
                            session_id,
                            "tty_input_chunk",
                            trace_id=trace_id,
                            platform=platform,
                            actor=ActorRef(kind="user", id="terminal_user"),
                            target=TargetRef(kind="agent", name=platform),
                            content={"text": incoming.decode("utf-8", errors="replace")},
                            visibility=Visibility(
                                provider_exposed=False,
                                runtime_exposed=True,
                                user_visible=True,
                            ),
                        )
                    )
        finally:
            if old_winch_handler is not None and hasattr(signal, "SIGWINCH"):
                signal.signal(signal.SIGWINCH, old_winch_handler)
            if old_settings is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)

        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), transcript_refs


def _run_without_tty(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    store: SessionStore,
    session_id: str,
    platform: str,
    trace_id: str,
) -> tuple[int, list[str]]:
    stdin_path, _, stdout_path, _ = _prepare_transcript_paths(store)
    transcript_refs = [
        store.session_relative(stdin_path),
        store.session_relative(stdout_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None

    with stdin_path.open("ab") as _stdin_file, stdout_path.open("ab") as stdout_file:
        for chunk in iter(lambda: process.stdout.read(4096), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            stdout_file.write(chunk)
            stdout_file.flush()
            store.append_event(
                make_event(
                    session_id,
                    "tty_output_chunk",
                    trace_id=trace_id,
                    platform=platform,
                    actor=ActorRef(kind="agent", id=platform),
                    target=TargetRef(kind="user", name="terminal"),
                    content={"text": chunk.decode("utf-8", errors="replace")},
                    visibility=Visibility(
                        provider_exposed=False,
                        runtime_exposed=True,
                        user_visible=True,
                    ),
                )
            )
    return process.wait(), transcript_refs


def launch_session(config: LaunchConfig) -> LaunchResult:
    session_id = make_session_id()
    trace_id = make_trace_id()
    store = SessionStore(config.root, session_id)
    proxy: EmbeddedTraceProxy | None = None

    snapshot = collect_session_context(config.cwd)
    snapshot_ref = store.write_snapshot("startup_context.json", snapshot)

    env = os.environ.copy()
    env["ASG_SESSION_ID"] = session_id
    env["ASG_TRACE_ID"] = trace_id
    env["ASG_DATA_ROOT"] = str(config.root.resolve())
    env.update(config.env_overrides)

    if config.upstream_url:
        proxy_settings = ProxySettings(
            session_id=session_id,
            root=config.root,
            upstream_url=config.upstream_url,
            provider=config.provider or "generic",
            platform=config.agent,
            trace_id=trace_id,
            listen_host=config.proxy_host,
            listen_port=config.proxy_port,
        )
        proxy = EmbeddedTraceProxy(proxy_settings, store=store)
        proxy.start()
        if config.base_url_env:
            env[config.base_url_env] = proxy.url

    command = (
        config.command_builder(proxy.url if proxy is not None else None)
        if config.command_builder is not None
        else list(config.command or [])
    )
    if not command:
        raise ValueError("launch requires either command or command_builder")

    manifest = {
        "session_id": session_id,
        "trace_id": trace_id,
        "agent": config.agent,
        "command": command,
        "cwd": str(config.cwd),
        "provider": config.provider,
        "upstream_url": config.upstream_url,
        "base_url_env": config.base_url_env,
        "env_override_keys": sorted(config.env_overrides.keys()),
    }
    store.write_manifest(manifest)

    store.append_event(
        make_event(
            session_id,
            "session_started",
            trace_id=trace_id,
            platform=config.agent,
            actor=ActorRef(kind="runtime", id="launcher"),
            target=TargetRef(kind="agent", name=config.agent),
            content={"command": command, "cwd": str(config.cwd)},
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )
    store.append_event(
        make_event(
            session_id,
            "context_snapshot",
            trace_id=trace_id,
            platform=config.agent,
            actor=ActorRef(kind="runtime", id="context_probe"),
            target=TargetRef(kind="environment", name="workspace"),
            content=snapshot,
            context_ref=context_ref(snapshot),
            artifacts=[snapshot_ref],
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )
    if proxy is not None:
        store.append_event(
            make_event(
                session_id,
                "proxy_started",
                trace_id=trace_id,
                platform=config.agent,
                actor=ActorRef(kind="runtime", id="launcher"),
                target=TargetRef(kind="proxy", name="embedded_proxy"),
                content={
                    "listen_url": proxy.url,
                    "upstream_url": config.upstream_url,
                    "provider": config.provider or "generic",
                    "base_url_env": config.base_url_env,
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )

    if config.on_session_ready is not None:
        config.on_session_ready(store, session_id, trace_id, command)

    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            exit_code, transcript_refs = _run_with_pty(
                command=command,
                cwd=config.cwd,
                env=env,
                store=store,
                session_id=session_id,
                platform=config.agent,
                trace_id=trace_id,
            )
        else:
            exit_code, transcript_refs = _run_without_tty(
                command=command,
                cwd=config.cwd,
                env=env,
                store=store,
                session_id=session_id,
                platform=config.agent,
                trace_id=trace_id,
            )
    finally:
        if proxy is not None:
            proxy.stop()
            store.append_event(
                make_event(
                    session_id,
                    "proxy_stopped",
                    trace_id=trace_id,
                    platform=config.agent,
                    actor=ActorRef(kind="runtime", id="launcher"),
                    target=TargetRef(kind="proxy", name="embedded_proxy"),
                    content={"listen_host": config.proxy_host},
                    visibility=Visibility(
                        provider_exposed=False,
                        runtime_exposed=True,
                        user_visible=False,
                    ),
                )
            )

    store.append_event(
        make_event(
            session_id,
            "session_ended",
            trace_id=trace_id,
            platform=config.agent,
            actor=ActorRef(kind="runtime", id="launcher"),
            target=TargetRef(kind="agent", name=config.agent),
            content={"exit_code": exit_code},
            artifacts=transcript_refs,
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )
    if config.on_session_finished is not None:
        config.on_session_finished(store, session_id, trace_id, exit_code)
    return LaunchResult(
        session_id=session_id,
        trace_id=trace_id,
        exit_code=exit_code,
        session_dir=store.session_dir,
        command=command,
    )
