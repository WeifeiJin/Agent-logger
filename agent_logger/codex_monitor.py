from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import threading
import time
from typing import Any

from .authz_cases import generate_authz_case_artifacts
from .codex_rollout import (
    RolloutCursorState,
    canonicalize_rollout_delta,
    extract_rollout_thread_id,
    find_rollout_paths,
    read_rollout_entries,
)
from .ids import utc_timestamp_from_epoch
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


_AUTHZ_TRIGGER_EVENT_TYPES = {
    "tool_call_requested",
}


@dataclass(slots=True)
class CodexMonitorState:
    history_offset: int = 0
    events_offset: int = 0
    known_thread_ids: list[str] = field(default_factory=list)
    seen_llm_request: bool = False
    seen_llm_response: bool = False
    history_entries_imported: int = 0
    rollout_entry_counts: dict[str, int] = field(default_factory=dict)
    rollout_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    rollout_entries_imported: int = 0
    authz_case_count: int = 0
    last_authz_refresh_timestamp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_offset": self.history_offset,
            "events_offset": self.events_offset,
            "known_thread_ids": list(self.known_thread_ids),
            "seen_llm_request": self.seen_llm_request,
            "seen_llm_response": self.seen_llm_response,
            "history_entries_imported": self.history_entries_imported,
            "rollout_entry_counts": dict(self.rollout_entry_counts),
            "rollout_states": dict(self.rollout_states),
            "rollout_entries_imported": self.rollout_entries_imported,
            "authz_case_count": self.authz_case_count,
            "last_authz_refresh_timestamp": self.last_authz_refresh_timestamp,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "CodexMonitorState":
        if not isinstance(payload, dict):
            return cls()
        known_thread_ids = payload.get("known_thread_ids")
        rollout_entry_counts = payload.get("rollout_entry_counts")
        rollout_states = payload.get("rollout_states")
        if not isinstance(known_thread_ids, list):
            known_thread_ids = []
        if not isinstance(rollout_entry_counts, dict):
            rollout_entry_counts = {}
        if not isinstance(rollout_states, dict):
            rollout_states = {}
        return cls(
            history_offset=int(payload.get("history_offset", 0) or 0),
            events_offset=int(payload.get("events_offset", 0) or 0),
            known_thread_ids=[str(item) for item in known_thread_ids if isinstance(item, str) and item],
            seen_llm_request=bool(payload.get("seen_llm_request")),
            seen_llm_response=bool(payload.get("seen_llm_response")),
            history_entries_imported=int(payload.get("history_entries_imported", 0) or 0),
            rollout_entry_counts={
                str(key): int(value)
                for key, value in rollout_entry_counts.items()
                if isinstance(key, str)
            },
            rollout_states={
                str(key): value
                for key, value in rollout_states.items()
                if isinstance(key, str) and isinstance(value, dict)
            },
            rollout_entries_imported=int(payload.get("rollout_entries_imported", 0) or 0),
            authz_case_count=int(payload.get("authz_case_count", 0) or 0),
            last_authz_refresh_timestamp=(
                float(payload["last_authz_refresh_timestamp"])
                if isinstance(payload.get("last_authz_refresh_timestamp"), (int, float))
                else None
            ),
        )


def _monitor_state_path(store: SessionStore) -> Path:
    return store.snapshot_path("codex_monitor_state.json")


def _load_state(store: SessionStore, *, initial_history_offset: int) -> CodexMonitorState:
    path = _monitor_state_path(store)
    if not path.exists():
        return CodexMonitorState(history_offset=initial_history_offset)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CodexMonitorState(history_offset=initial_history_offset)
    state = CodexMonitorState.from_dict(payload)
    if state.history_offset < initial_history_offset:
        state.history_offset = initial_history_offset
    return state


def _save_state(store: SessionStore, state: CodexMonitorState) -> None:
    store.write_snapshot("codex_monitor_state.json", state.to_dict())


def _read_jsonl_delta(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return offset, []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        effective_offset = offset if 0 <= offset <= file_size else 0
        handle.seek(effective_offset)
        data = handle.read()
    if not data:
        return file_size, []

    rows: list[dict[str, Any]] = []
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return file_size, rows


def _append_unique(items: list[str], value: str | None) -> None:
    if isinstance(value, str) and value and value not in items:
        items.append(value)


def _update_state_from_session_rows(state: CodexMonitorState, rows: list[dict[str, Any]]) -> bool:
    should_refresh_authz = False
    for event in rows:
        event_type = event.get("event_type")
        if event_type == "llm_request":
            state.seen_llm_request = True
        elif event_type == "llm_response":
            state.seen_llm_response = True
        if event_type in _AUTHZ_TRIGGER_EVENT_TYPES:
            should_refresh_authz = True

        metadata = event.get("platform_metadata", {})
        if isinstance(metadata, dict):
            _append_unique(state.known_thread_ids, metadata.get("codex_thread_id"))
        content = event.get("content", {})
        if isinstance(content, dict):
            _append_unique(state.known_thread_ids, content.get("codex_thread_id"))
    return should_refresh_authz


def _filter_history_entries(
    entries: list[dict[str, Any]],
    *,
    thread_ids: list[str],
    started_at_epoch: int,
) -> list[dict[str, Any]]:
    if not thread_ids:
        return []
    allowed = set(thread_ids)
    return [
        entry
        for entry in entries
        if isinstance(entry.get("session_id"), str)
        and entry["session_id"] in allowed
        and (not isinstance(entry.get("ts"), (int, float)) or int(entry["ts"]) >= started_at_epoch)
    ]


def _append_history_entries(
    store: SessionStore,
    *,
    session_id: str,
    trace_id: str,
    entries: list[dict[str, Any]],
) -> int:
    imported = 0
    for index, entry in enumerate(entries):
        thread_id = entry.get("session_id")
        timestamp = utc_timestamp_from_epoch(entry.get("ts", time.time()))
        store.append_event(
            make_event(
                session_id,
                "user_input",
                timestamp=timestamp,
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="user", id="codex_user"),
                target=TargetRef(kind="agent", name="codex"),
                content={
                    "text": entry.get("text", ""),
                    "codex_thread_id": thread_id,
                    "history_index": index,
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=True),
                platform_metadata={"source": "codex_history", "codex_thread_id": thread_id},
            )
        )
        imported += 1
    return imported


def _candidate_rollout_paths(
    state: CodexMonitorState,
    *,
    codex_home: Path,
    cwd: Path,
    started_at_epoch: int,
) -> list[Path]:
    sessions_root = codex_home / "sessions"
    discovered = find_rollout_paths(
        sessions_root,
        thread_ids=list(state.known_thread_ids),
        cwd=str(cwd),
        started_at_epoch=started_at_epoch,
        fallback_limit=4,
    )
    candidates: list[Path] = []
    seen: set[str] = set()
    for path_str in state.rollout_entry_counts:
        path = Path(path_str)
        if path.exists() and path_str not in seen:
            candidates.append(path)
            seen.add(path_str)
    for path in discovered:
        path_str = str(path)
        if path.exists() and path_str not in seen:
            candidates.append(path)
            seen.add(path_str)
    return candidates


def prime_codex_monitor_state(
    store: SessionStore,
    *,
    initial_history_offset: int,
) -> CodexMonitorState:
    path = _monitor_state_path(store)
    if path.exists():
        return _load_state(store, initial_history_offset=initial_history_offset)
    state = CodexMonitorState(
        history_offset=initial_history_offset,
        events_offset=store.events_path.stat().st_size if store.events_path.exists() else 0,
    )
    _save_state(store, state)
    return state


def reconcile_codex_runtime(
    store: SessionStore,
    *,
    session_id: str,
    trace_id: str,
    codex_home: Path,
    history_path: Path,
    cwd: Path,
    started_at_epoch: int,
    initial_history_offset: int,
    final: bool = False,
) -> dict[str, Any]:
    state = _load_state(store, initial_history_offset=initial_history_offset)

    next_events_offset, session_rows = _read_jsonl_delta(store.events_path, state.events_offset)
    state.events_offset = next_events_offset
    should_refresh_authz = _update_state_from_session_rows(state, session_rows)

    rollout_events_imported = 0
    rollout_entries_imported = 0
    include_request_backfill = not state.seen_llm_request
    include_response_backfill = not state.seen_llm_response

    for path in _candidate_rollout_paths(
        state,
        codex_home=codex_home,
        cwd=cwd,
        started_at_epoch=started_at_epoch,
    ):
        entries = read_rollout_entries(path)
        if not entries:
            continue

        thread_id = extract_rollout_thread_id(entries)
        _append_unique(state.known_thread_ids, thread_id)

        path_key = str(path)
        processed_count = state.rollout_entry_counts.get(path_key, 0)
        cursor = RolloutCursorState.from_dict(state.rollout_states.get(path_key))
        if len(entries) < processed_count:
            processed_count = 0
            cursor = RolloutCursorState()

        delta_entries = entries[processed_count:]
        if not delta_entries:
            state.rollout_entry_counts[path_key] = len(entries)
            state.rollout_states[path_key] = cursor.to_dict()
            continue

        delta_events, cursor = canonicalize_rollout_delta(
            delta_entries,
            session_id=session_id,
            platform="codex",
            trace_id=trace_id,
            thread_id=thread_id,
            rollout_path=path_key,
            entry_index_offset=processed_count,
            include_request_backfill=include_request_backfill,
            include_response_backfill=include_response_backfill,
            state=cursor,
        )
        for event in delta_events:
            store.append_event(event)

        if any(event.event_type in _AUTHZ_TRIGGER_EVENT_TYPES for event in delta_events):
            should_refresh_authz = True

        rollout_entries_imported += len(delta_entries)
        rollout_events_imported += len(delta_events)
        state.rollout_entry_counts[path_key] = len(entries)
        state.rollout_states[path_key] = cursor.to_dict()

    history_entries_imported = 0
    if state.known_thread_ids:
        next_history_offset, history_rows = _read_jsonl_delta(history_path, state.history_offset)
        filtered_history = _filter_history_entries(
            history_rows,
            thread_ids=state.known_thread_ids,
            started_at_epoch=started_at_epoch,
        )
        history_entries_imported = _append_history_entries(
            store,
            session_id=session_id,
            trace_id=trace_id,
            entries=filtered_history,
        )
        state.history_offset = next_history_offset
        state.history_entries_imported += history_entries_imported

    authz_summary: dict[str, Any] | None = None
    if final or should_refresh_authz:
        authz_summary = generate_authz_case_artifacts(store, append_event=final)
        state.authz_case_count = int(authz_summary["case_count"])
        state.last_authz_refresh_timestamp = time.time()

    state.rollout_entries_imported += rollout_entries_imported
    _save_state(store, state)

    summary = {
        "thread_ids": list(state.known_thread_ids),
        "history_entries_imported": history_entries_imported,
        "history_entries_total": state.history_entries_imported,
        "rollout_entries_imported": rollout_entries_imported,
        "rollout_entries_total": state.rollout_entries_imported,
        "rollout_events_imported": rollout_events_imported,
        "rollout_paths": sorted(state.rollout_entry_counts.keys()),
        "authz_case_count": state.authz_case_count,
        "authz_refreshed": bool(authz_summary is not None),
    }
    if final:
        store.append_event(
            make_event(
                session_id,
                "codex_runtime_reconciled",
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_monitor"),
                target=TargetRef(kind="agent", name="codex"),
                content=summary,
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )
    return summary


class CodexRuntimeMonitor:
    def __init__(
        self,
        *,
        store: SessionStore,
        session_id: str,
        trace_id: str,
        codex_home: Path,
        history_path: Path,
        cwd: Path,
        started_at_epoch: int,
        initial_history_offset: int,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.trace_id = trace_id
        self.codex_home = codex_home
        self.history_path = history_path
        self.cwd = cwd
        self.started_at_epoch = started_at_epoch
        self.initial_history_offset = initial_history_offset
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        prime_codex_monitor_state(
            self.store,
            initial_history_offset=self.initial_history_offset,
        )
        self.store.append_event(
            make_event(
                self.session_id,
                "codex_monitor_started",
                trace_id=self.trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_monitor"),
                target=TargetRef(kind="agent", name="codex"),
                content={"poll_interval_seconds": self.poll_interval_seconds},
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )
        self._thread = threading.Thread(
            target=self._run,
            name=f"codex-monitor-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_seconds * 4))
            self._thread = None
        self.store.append_event(
            make_event(
                self.session_id,
                "codex_monitor_stopped",
                trace_id=self.trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_monitor"),
                target=TargetRef(kind="agent", name="codex"),
                content={},
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval_seconds):
            try:
                reconcile_codex_runtime(
                    self.store,
                    session_id=self.session_id,
                    trace_id=self.trace_id,
                    codex_home=self.codex_home,
                    history_path=self.history_path,
                    cwd=self.cwd,
                    started_at_epoch=self.started_at_epoch,
                    initial_history_offset=self.initial_history_offset,
                    final=False,
                )
            except Exception as exc:
                self.store.append_event(
                    make_event(
                        self.session_id,
                        "codex_monitor_error",
                        trace_id=self.trace_id,
                        platform="codex",
                        actor=ActorRef(kind="runtime", id="codex_monitor"),
                        target=TargetRef(kind="agent", name="codex"),
                        content={"error": repr(exc)},
                        visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                    )
                )
                return
