from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import threading
import time
from typing import Any

from .canonicalize import canonicalize_request
from .ids import make_trace_id
from .launcher import LaunchConfig, LaunchResult, launch_session
from .render import generate_session_report_artifact
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


@dataclass(slots=True)
class OpenClawProxyConfig:
    upstream_url: str | None = None
    provider_api: str | None = None
    model_id: str | None = None
    provider_id: str = "agent_logger_proxy"
    api_key_env: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.upstream_url)


def resolve_openclaw_config_path() -> Path:
    env_path = Path.home() / ".openclaw" / "openclaw.json"
    raw = str(Path(env_path))
    try:
        import os

        raw = os.environ.get("OPENCLAW_CONFIG_PATH", raw)
    except Exception:
        raw = str(env_path)
    return Path(raw).expanduser()


def _default_api_key_env(provider_api: str | None) -> str | None:
    if provider_api == "anthropic-messages":
        return "ANTHROPIC_API_KEY"
    if provider_api in {"openai-completions", "openai-responses"}:
        return "OPENAI_API_KEY"
    return None


def _write_openclaw_overlay_config(
    *,
    overlay_config_path: Path,
    base_config_path: Path | None,
    cache_trace_path: Path,
    proxy_url: str | None,
    proxy: OpenClawProxyConfig,
) -> None:
    overlay_config_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "diagnostics": {
            "cacheTrace": {
                "enabled": True,
                "filePath": str(cache_trace_path),
                "includeMessages": True,
                "includePrompt": True,
                "includeSystem": True,
            }
        }
    }

    if base_config_path is not None and base_config_path.exists():
        copied = overlay_config_path.parent / "base.openclaw.json"
        shutil.copyfile(base_config_path, copied)
        payload["$include"] = "./base.openclaw.json"

    if proxy.enabled:
        if not proxy.provider_api or not proxy.model_id:
            raise ValueError("OpenClaw proxy mode requires both provider_api and model_id")
        provider_payload: dict[str, Any] = {
            "api": proxy.provider_api,
            "baseUrl": proxy_url,
            "models": [
                {
                    "id": proxy.model_id,
                    "name": proxy.model_id,
                }
            ],
        }
        api_key_env = proxy.api_key_env or _default_api_key_env(proxy.provider_api)
        if api_key_env:
            provider_payload["apiKey"] = "${" + api_key_env + "}"
        payload["models"] = {
            "providers": {
                proxy.provider_id: provider_payload,
            }
        }
        payload["agents"] = {
            "defaults": {
                "model": {
                    "primary": f"{proxy.provider_id}/{proxy.model_id}",
                }
            }
        }

    overlay_config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _retarget_openclaw_events(
    events: list[Any],
    *,
    trace_id: str,
    parent_event_id: str | None,
    source: str,
) -> list[Any]:
    for event in events:
        event.platform = "openclaw"
        event.trace_id = trace_id
        event.parent_event_id = parent_event_id
        event.visibility = Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False)
        metadata = dict(event.platform_metadata)
        metadata["source"] = source
        event.platform_metadata = metadata
    return events


def _canonicalize_openclaw_cache_trace_row(
    row: dict[str, Any],
    *,
    session_id: str,
    trace_id: str,
    parent_event_id: str | None,
) -> list[Any]:
    events: list[Any] = []

    messages = row.get("messages")
    if isinstance(messages, list):
        payload: dict[str, Any] = {"messages": messages}
        if "system" in row:
            payload["system"] = row.get("system")
        if "model" in row:
            payload["model"] = row.get("model")
        events.extend(
            _retarget_openclaw_events(
                canonicalize_request(
                    payload,
                    session_id=session_id,
                    platform="openclaw",
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                ),
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                source="openclaw_cache_trace.messages",
            )
        )

    prompt_value = row.get("prompt")
    prompt_text = prompt_value if isinstance(prompt_value, str) else json.dumps(prompt_value, ensure_ascii=False) if prompt_value is not None else ""
    if prompt_text.strip():
        events.append(
            make_event(
                session_id,
                "request_system_message",
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                platform="openclaw",
                actor=ActorRef(kind="runtime", id="openclaw_cache_trace"),
                target=TargetRef(kind="agent", name="openclaw"),
                content={"text": prompt_text},
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                platform_metadata={"source": "openclaw_cache_trace.prompt"},
            )
        )

    return events


class OpenClawCacheTraceMonitor:
    def __init__(
        self,
        *,
        store: SessionStore,
        session_id: str,
        trace_id: str,
        cache_trace_path: Path,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.trace_id = trace_id
        self.cache_trace_path = cache_trace_path
        self._offset = 0
        self._buffer = ""
        self._row_index = 0
        self._seen_hashes: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
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
        if not self.cache_trace_path.exists():
            return
        with self.cache_trace_path.open("rb") as handle:
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
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        digest = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if digest in self._seen_hashes:
            return
        self._seen_hashes.add(digest)

        row_name = f"openclaw/cache_trace_{self._row_index:04d}.json"
        raw_ref = self.store.write_raw_json(row_name, payload)
        imported_event = make_event(
            self.session_id,
            "openclaw_cache_trace_imported",
            trace_id=self.trace_id,
            platform="openclaw",
            actor=ActorRef(kind="runtime", id="openclaw_cache_trace"),
            target=TargetRef(kind="file", name=str(self.cache_trace_path)),
            content={"row_index": self._row_index, "keys": sorted(payload.keys())},
            raw_ref=raw_ref,
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
        self.store.append_event(imported_event)
        self._row_index += 1

        for event in _canonicalize_openclaw_cache_trace_row(
            payload,
            session_id=self.session_id,
            trace_id=self.trace_id,
            parent_event_id=imported_event.event_id,
        ):
            self.store.append_event(event)


def run_openclaw_session(
    *,
    root: Path,
    cwd: Path,
    user_args: list[str],
    proxy: OpenClawProxyConfig | None = None,
) -> LaunchResult:
    proxy = proxy or OpenClawProxyConfig()
    runtime_id = make_trace_id("openclaw")
    runtime_root = root.resolve() / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    overlay_dir = runtime_root / runtime_id
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_config_path = overlay_dir / "openclaw.json"
    cache_trace_path = overlay_dir / "cache-trace.jsonl"
    original_config_path = resolve_openclaw_config_path()

    def _command_builder(proxy_url: str | None) -> list[str]:
        _write_openclaw_overlay_config(
            overlay_config_path=overlay_config_path,
            base_config_path=original_config_path if original_config_path.exists() else None,
            cache_trace_path=cache_trace_path,
            proxy_url=proxy_url,
            proxy=proxy,
        )
        return ["openclaw", *user_args]

    monitor: OpenClawCacheTraceMonitor | None = None

    def _on_session_ready(store: SessionStore, session_id: str, trace_id: str, _command: list[str]) -> None:
        nonlocal monitor
        store.append_event(
            make_event(
                session_id,
                "openclaw_overlay_configured",
                trace_id=trace_id,
                platform="openclaw",
                actor=ActorRef(kind="runtime", id="openclaw_adapter"),
                target=TargetRef(kind="file", name=str(overlay_config_path)),
                content={
                    "overlay_config_path": str(overlay_config_path),
                    "cache_trace_path": str(cache_trace_path),
                    "proxy_enabled": proxy.enabled,
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )
        monitor = OpenClawCacheTraceMonitor(
            store=store,
            session_id=session_id,
            trace_id=trace_id,
            cache_trace_path=cache_trace_path,
        )
        monitor.start()

    def _on_session_finished(store: SessionStore, session_id: str, trace_id: str, _exit_code: int) -> None:
        nonlocal monitor
        if monitor is not None:
            monitor.stop()
            monitor = None
        generate_session_report_artifact(store)

    return launch_session(
        LaunchConfig(
            agent="openclaw",
            command=None,
            command_builder=_command_builder,
            root=root,
            cwd=cwd,
            provider=proxy.provider_id if proxy.enabled else "openclaw",
            upstream_url=proxy.upstream_url,
            env_overrides={"OPENCLAW_CONFIG_PATH": str(overlay_config_path)},
            on_session_ready=_on_session_ready,
            on_session_finished=_on_session_finished,
        )
    )
