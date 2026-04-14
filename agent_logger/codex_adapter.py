from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import sqlite3
import time
import tomllib
from typing import Any

from .codex_rollout import canonicalize_rollout_entries, extract_rollout_thread_id, find_rollout_paths, read_rollout_entries
from .codex_monitor import CodexRuntimeMonitor, reconcile_codex_runtime
from .ids import utc_timestamp_from_epoch
from .launcher import LaunchConfig, LaunchResult, launch_session
from .render import generate_session_report_artifact
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


@dataclass(slots=True)
class CodexPaths:
    home: Path
    config_path: Path
    history_path: Path
    state_db_path: Path
    logs_db_path: Path


@dataclass(slots=True)
class CodexSnapshot:
    history_size: int
    started_at_epoch: int


def resolve_codex_paths() -> CodexPaths:
    home = Path.home() / ".codex"
    return CodexPaths(
        home=home,
        config_path=home / "config.toml",
        history_path=home / "history.jsonl",
        state_db_path=home / "state_5.sqlite",
        logs_db_path=home / "logs_2.sqlite",
    )


def load_codex_config(paths: CodexPaths) -> dict[str, Any]:
    if not paths.config_path.exists():
        raise FileNotFoundError(f"Codex config not found: {paths.config_path}")
    with paths.config_path.open("rb") as handle:
        return tomllib.load(handle)


def get_active_provider_config(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    provider_name = config.get("model_provider")
    providers = config.get("model_providers", {})
    if not provider_name or provider_name not in providers:
        raise ValueError("Unable to locate active Codex model provider in ~/.codex/config.toml")
    provider = dict(providers[provider_name])
    return str(provider_name), provider


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"Unsupported config override type: {type(value)!r}")


def build_codex_provider_override_args(
    *,
    proxy_url: str,
    active_provider_name: str,
    active_provider_config: dict[str, Any],
    alias: str = "asg_proxy",
) -> list[str]:
    args = ["-c", f"model_provider={_toml_literal(alias)}"]
    merged = dict(active_provider_config)
    merged["name"] = active_provider_name
    merged["base_url"] = proxy_url
    for key, value in merged.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            args.extend(["-c", f"model_providers.{alias}.{key}={_toml_literal(value)}"])
    args.extend(["-c", f'asg.original_model_provider={_toml_literal(active_provider_name)}'])
    return args


def snapshot_codex_runtime(paths: CodexPaths) -> CodexSnapshot:
    history_size = paths.history_path.stat().st_size if paths.history_path.exists() else 0
    return CodexSnapshot(history_size=history_size, started_at_epoch=int(time.time()))


def read_appended_history(paths: CodexPaths, snapshot: CodexSnapshot) -> list[dict[str, Any]]:
    if not paths.history_path.exists():
        return []
    with paths.history_path.open("rb") as handle:
        handle.seek(snapshot.history_size)
        data = handle.read()
    if not data:
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def filter_history_entries(
    entries: list[dict[str, Any]],
    *,
    thread_ids: list[str] | None = None,
    started_at_epoch: int | None = None,
) -> list[dict[str, Any]]:
    filtered = list(entries)
    if started_at_epoch is not None:
        filtered = [
            entry
            for entry in filtered
            if not isinstance(entry.get("ts"), (int, float)) or int(entry["ts"]) >= started_at_epoch
        ]
    if thread_ids:
        allowed = set(thread_ids)
        filtered = [
            entry
            for entry in filtered
            if isinstance(entry.get("session_id"), str) and entry["session_id"] in allowed
        ]
    return filtered


def _query_all(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def retry_sqlite_read(fn: Any, *, attempts: int = 5, initial_delay: float = 0.1) -> Any:
    last_error: Exception | None = None
    delay = initial_delay
    for _ in range(attempts):
        try:
            return fn()
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(delay)
            delay *= 2
    if last_error is not None:
        raise last_error
    return fn()


def fetch_threads(paths: CodexPaths, thread_ids: list[str]) -> list[dict[str, Any]]:
    if not thread_ids or not paths.state_db_path.exists():
        return []
    placeholders = ",".join("?" for _ in thread_ids)
    query = f"select * from threads where id in ({placeholders}) order by updated_at asc"
    with sqlite3.connect(f"file:{paths.state_db_path}?mode=ro", uri=True) as conn:
        return _query_all(conn, query, tuple(thread_ids))


def fetch_spawn_edges(paths: CodexPaths, parent_thread_ids: list[str]) -> list[dict[str, Any]]:
    if not parent_thread_ids or not paths.state_db_path.exists():
        return []
    placeholders = ",".join("?" for _ in parent_thread_ids)
    query = (
        f"select * from thread_spawn_edges where parent_thread_id in ({placeholders}) "
        "order by parent_thread_id asc, child_thread_id asc"
    )
    with sqlite3.connect(f"file:{paths.state_db_path}?mode=ro", uri=True) as conn:
        return _query_all(conn, query, tuple(parent_thread_ids))


def fetch_thread_logs(
    paths: CodexPaths,
    thread_id: str,
    *,
    started_at_epoch: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not paths.logs_db_path.exists():
        return []
    query = (
        "select ts, ts_nanos, level, target, feedback_log_body, module_path, file, line, thread_id, process_uuid "
        "from logs where thread_id = ? and ts >= ? "
        "order by ts asc, ts_nanos asc, id asc limit ?"
    )
    with sqlite3.connect(f"file:{paths.logs_db_path}?mode=ro", uri=True) as conn:
        return _query_all(conn, query, (thread_id, started_at_epoch, limit))


def _append_codex_history_events(
    store: SessionStore,
    *,
    asg_session_id: str,
    trace_id: str,
    history_entries: list[dict[str, Any]],
    parent_event_id: str | None = None,
) -> list[str]:
    discovered_thread_ids: list[str] = []
    artifact_ref = store.write_json_artifact("codex/history_appended.json", history_entries)
    store.append_event(
        make_event(
            asg_session_id,
            "codex_history_imported",
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            platform="codex",
            actor=ActorRef(kind="runtime", id="codex_adapter"),
            target=TargetRef(kind="agent", name="codex"),
            content={"count": len(history_entries)},
            artifacts=[artifact_ref],
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )

    for index, entry in enumerate(history_entries):
        thread_id = entry.get("session_id")
        if isinstance(thread_id, str) and thread_id and thread_id not in discovered_thread_ids:
            discovered_thread_ids.append(thread_id)
        timestamp = utc_timestamp_from_epoch(entry.get("ts", time.time()))
        store.append_event(
            make_event(
                asg_session_id,
                "user_input",
                timestamp=timestamp,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                platform="codex",
                actor=ActorRef(kind="user", id="codex_user"),
                target=TargetRef(kind="agent", name="codex"),
                content={
                    "text": entry.get("text", ""),
                    "codex_thread_id": thread_id,
                    "history_index": index,
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=True),
                platform_metadata={"source": "codex_history"},
            )
        )
    return discovered_thread_ids


def _append_codex_thread_metadata(
    store: SessionStore,
    *,
    asg_session_id: str,
    trace_id: str,
    paths: CodexPaths,
    threads: list[dict[str, Any]],
    spawn_edges: list[dict[str, Any]],
    started_at_epoch: int,
) -> None:
    if threads:
        artifact_ref = store.write_json_artifact("codex/threads.json", threads)
        store.append_event(
            make_event(
                asg_session_id,
                "codex_threads_imported",
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_adapter"),
                target=TargetRef(kind="agent", name="codex"),
                content={"count": len(threads)},
                artifacts=[artifact_ref],
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )

    for thread in threads:
        store.append_event(
            make_event(
                asg_session_id,
                "codex_thread_detected",
                timestamp=utc_timestamp_from_epoch(thread.get("updated_at", started_at_epoch)),
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_adapter"),
                target=TargetRef(kind="thread", name=thread.get("id")),
                content=thread,
                context_ref={
                    "cwd": thread.get("cwd"),
                    "git_branch": thread.get("git_branch"),
                    "git_head": thread.get("git_sha"),
                },
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )

        thread_id = thread.get("id")
        if isinstance(thread_id, str):
            try:
                logs = retry_sqlite_read(
                    lambda: fetch_thread_logs(
                        paths=paths,
                        thread_id=thread_id,
                        started_at_epoch=started_at_epoch,
                    )
                )
            except sqlite3.Error as exc:
                store.append_event(
                    make_event(
                        asg_session_id,
                        "codex_state_import_error",
                        trace_id=trace_id,
                        platform="codex",
                        actor=ActorRef(kind="runtime", id="codex_adapter"),
                        target=TargetRef(kind="thread", name=thread_id),
                        content={"error": repr(exc), "source": "logs_2.sqlite"},
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                    )
                )
                logs = []
            if logs:
                logs_ref = store.write_json_artifact(f"codex/logs_{thread_id}.json", logs)
                store.append_event(
                    make_event(
                        asg_session_id,
                        "codex_thread_logs_imported",
                        trace_id=trace_id,
                        platform="codex",
                        actor=ActorRef(kind="runtime", id="codex_adapter"),
                        target=TargetRef(kind="thread", name=thread_id),
                        content={"codex_thread_id": thread_id, "count": len(logs)},
                        artifacts=[logs_ref],
                        visibility=Visibility(
                            provider_exposed=False,
                            runtime_exposed=True,
                            user_visible=False,
                        ),
                    )
                )

    if spawn_edges:
        artifact_ref = store.write_json_artifact("codex/thread_spawn_edges.json", spawn_edges)
        store.append_event(
            make_event(
                asg_session_id,
                "codex_spawn_edges_imported",
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_adapter"),
                target=TargetRef(kind="agent", name="codex"),
                content={"count": len(spawn_edges)},
                artifacts=[artifact_ref],
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
        )
        for edge in spawn_edges:
            store.append_event(
                make_event(
                    asg_session_id,
                    "subagent_spawned",
                    trace_id=trace_id,
                    platform="codex",
                    actor=ActorRef(kind="agent", id=edge["parent_thread_id"]),
                    target=TargetRef(kind="agent", name=edge["child_thread_id"]),
                    content={
                        "parent_thread_id": edge["parent_thread_id"],
                        "child_thread_id": edge["child_thread_id"],
                        "status": edge["status"],
                    },
                    visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                    platform_metadata={"source": "codex_thread_spawn_edges"},
                )
            )


def _session_has_event_type(store: SessionStore, event_type: str) -> bool:
    if not store.events_path.exists():
        return False
    with store.events_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") == event_type:
                return True
    return False


def _append_codex_rollout_events(
    store: SessionStore,
    *,
    asg_session_id: str,
    trace_id: str,
    paths: CodexPaths,
    thread_ids: list[str],
    cwd: Path,
    started_at_epoch: int,
) -> dict[str, Any]:
    sessions_root = paths.home / "sessions"
    rollout_paths = find_rollout_paths(
        sessions_root,
        thread_ids=thread_ids,
        cwd=str(cwd),
        started_at_epoch=started_at_epoch,
    )
    imported_paths: list[str] = []
    imported_thread_ids: list[str] = []
    imported_entries = 0
    include_request_backfill = not _session_has_event_type(store, "llm_request")
    include_response_backfill = not _session_has_event_type(store, "llm_response")

    for path in rollout_paths:
        try:
            entries = read_rollout_entries(path)
            artifact_ref = store.write_bytes_artifact(f"codex/{path.name}", path.read_bytes())
        except OSError as exc:
            store.append_event(
                make_event(
                    asg_session_id,
                    "codex_rollout_import_error",
                    trace_id=trace_id,
                    platform="codex",
                    actor=ActorRef(kind="runtime", id="codex_adapter"),
                    target=TargetRef(kind="file", name=str(path)),
                    content={"error": repr(exc)},
                    visibility=Visibility(
                        provider_exposed=False,
                        runtime_exposed=True,
                        user_visible=False,
                    ),
                )
            )
            continue

        if not entries:
            continue

        rollout_thread_id = extract_rollout_thread_id(entries)
        imported_paths.append(str(path))
        imported_entries += len(entries)
        if (
            isinstance(rollout_thread_id, str)
            and rollout_thread_id
            and rollout_thread_id not in imported_thread_ids
        ):
            imported_thread_ids.append(rollout_thread_id)
        store.append_event(
            make_event(
                asg_session_id,
                "codex_rollout_imported",
                trace_id=trace_id,
                platform="codex",
                actor=ActorRef(kind="runtime", id="codex_adapter"),
                target=TargetRef(kind="agent", name="codex"),
                content={
                    "path": str(path),
                    "count": len(entries),
                    "codex_thread_id": rollout_thread_id,
                },
                artifacts=[artifact_ref],
                visibility=Visibility(
                    provider_exposed=False,
                    runtime_exposed=True,
                    user_visible=False,
                ),
                platform_metadata={
                    "source": "codex_rollout",
                    "codex_thread_id": rollout_thread_id,
                    "rollout_path": str(path),
                },
            )
        )

        for event in canonicalize_rollout_entries(
            entries,
            session_id=asg_session_id,
            platform="codex",
            trace_id=trace_id,
            thread_id=rollout_thread_id,
            rollout_path=str(path),
            include_request_backfill=include_request_backfill,
            include_response_backfill=include_response_backfill,
        ):
            store.append_event(event)

    return {
        "paths": imported_paths,
        "thread_ids": imported_thread_ids,
        "entry_count": imported_entries,
    }


def update_manifest_with_codex_metadata(store: SessionStore, update: dict[str, Any]) -> None:
    manifest = {}
    if store.manifest_path.exists():
        manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest.update(update)
    store.write_manifest(manifest)


def extract_codex_thread_ids_from_session_events(store: SessionStore) -> list[str]:
    if not store.events_path.exists():
        return []
    thread_ids: list[str] = []
    for line in store.events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        metadata = event.get("platform_metadata", {})
        if not isinstance(metadata, dict):
            continue
        candidate = metadata.get("codex_thread_id")
        if isinstance(candidate, str) and candidate and candidate not in thread_ids:
            thread_ids.append(candidate)
    return thread_ids


def build_codex_command_builder(
    *,
    user_args: list[str],
    active_provider_name: str,
    active_provider_config: dict[str, Any],
    enable_proxy: bool,
) -> Any:
    def _builder(proxy_url: str | None) -> list[str]:
        args = ["codex"]
        if enable_proxy:
            if not proxy_url:
                raise ValueError("proxy_url is required when Codex proxy mode is enabled")
            args.extend(
                build_codex_provider_override_args(
                    proxy_url=proxy_url,
                    active_provider_name=active_provider_name,
                    active_provider_config=active_provider_config,
                )
            )
        args.extend(user_args)
        return args

    return _builder


def build_local_proxy_bypass_env(existing_env: dict[str, str] | None = None) -> dict[str, str]:
    source = existing_env or {}
    values = []
    for key in ("NO_PROXY", "no_proxy"):
        raw = source.get(key, "")
        values.extend(item.strip() for item in raw.split(",") if item.strip())
    for item in ("127.0.0.1", "localhost"):
        if item not in values:
            values.append(item)
    merged = ",".join(values)
    return {"NO_PROXY": merged, "no_proxy": merged}


def run_codex_session(
    *,
    root: Path,
    cwd: Path,
    user_args: list[str],
    enable_proxy: bool = True,
    upstream_url: str | None = None,
) -> LaunchResult:
    paths = resolve_codex_paths()
    config = load_codex_config(paths)
    active_provider_name, active_provider_config = get_active_provider_config(config)
    snapshot = snapshot_codex_runtime(paths)

    effective_upstream_url = upstream_url or str(active_provider_config.get("base_url") or "")
    if enable_proxy and not effective_upstream_url:
        raise ValueError("Codex proxy mode requires an upstream base_url from config or --upstream-url")

    monitor: CodexRuntimeMonitor | None = None

    def _on_session_ready(store: SessionStore, session_id: str, trace_id: str, _command: list[str]) -> None:
        nonlocal monitor
        try:
            monitor = CodexRuntimeMonitor(
                store=store,
                session_id=session_id,
                trace_id=trace_id,
                codex_home=paths.home,
                history_path=paths.history_path,
                cwd=cwd,
                started_at_epoch=snapshot.started_at_epoch,
                initial_history_offset=snapshot.history_size,
            )
            monitor.start()
        except Exception as exc:
            store.append_event(
                make_event(
                    session_id,
                    "codex_monitor_error",
                    trace_id=trace_id,
                    platform="codex",
                    actor=ActorRef(kind="runtime", id="codex_adapter"),
                    target=TargetRef(kind="agent", name="codex"),
                    content={"error": repr(exc), "phase": "start"},
                    visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                )
            )
            monitor = None

    def _on_session_finished(store: SessionStore, session_id: str, trace_id: str, _exit_code: int) -> None:
        nonlocal monitor
        if monitor is None:
            return
        try:
            monitor.stop()
        except Exception as exc:
            store.append_event(
                make_event(
                    session_id,
                    "codex_monitor_error",
                    trace_id=trace_id,
                    platform="codex",
                    actor=ActorRef(kind="runtime", id="codex_adapter"),
                    target=TargetRef(kind="agent", name="codex"),
                    content={"error": repr(exc), "phase": "stop"},
                    visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
                )
            )
        finally:
            monitor = None

    launch_config = LaunchConfig(
        agent="codex",
        command=None,
        root=root,
        cwd=cwd,
        provider=active_provider_name,
        upstream_url=effective_upstream_url if enable_proxy else None,
        env_overrides=build_local_proxy_bypass_env(dict(os.environ)) if enable_proxy else {},
        command_builder=build_codex_command_builder(
            user_args=user_args,
            active_provider_name=active_provider_name,
            active_provider_config=active_provider_config,
            enable_proxy=enable_proxy,
        ),
        on_session_ready=_on_session_ready,
        on_session_finished=_on_session_finished,
    )
    result = launch_session(launch_config)

    store = SessionStore(root, result.session_id)
    runtime_summary = reconcile_codex_runtime(
        store,
        session_id=result.session_id,
        trace_id=result.trace_id,
        codex_home=paths.home,
        history_path=paths.history_path,
        cwd=cwd,
        started_at_epoch=snapshot.started_at_epoch,
        initial_history_offset=snapshot.history_size,
        final=True,
    )
    update_manifest_with_codex_metadata(
        store,
        {
            "codex_home": str(paths.home),
            "codex_active_provider": active_provider_name,
            "codex_thread_ids": runtime_summary["thread_ids"],
            "codex_history_entries": runtime_summary["history_entries_total"],
            "codex_rollout_paths": runtime_summary["rollout_paths"],
            "codex_rollout_entries": runtime_summary["rollout_entries_total"],
            "codex_proxy_enabled": enable_proxy,
            "codex_realtime_monitor": True,
        },
    )
    generate_session_report_artifact(store)
    return result
