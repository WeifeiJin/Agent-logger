from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import threading
from typing import Any

from .schema import Event


class SessionStore:
    def __init__(self, root: Path | str, session_id: str) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.session_dir = self.root / "sessions" / session_id
        self.artifacts_dir = self.session_dir / "artifacts"
        self.raw_dir = self.session_dir / "raw"
        self.snapshots_dir = self.session_dir / "snapshots"
        self.events_path = self.session_dir / "events.jsonl"
        self.manifest_path = self.session_dir / "manifest.json"
        self._lock = threading.Lock()

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)

    def _write_text_atomic(self, path: Path, content: str, *, encoding: str = "utf-8") -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(content)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _write_bytes_atomic(self, path: Path, content: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        with self._lock:
            self._write_text_atomic(
                self.manifest_path,
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            )
        return self.manifest_path

    def append_event(self, event: Event | dict[str, Any]) -> None:
        payload = event.to_dict() if isinstance(event, Event) else event
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")

    def session_relative(self, path: Path) -> str:
        return str(path.relative_to(self.session_dir))

    def artifact_path(self, name: str) -> Path:
        path = self.artifacts_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def raw_path(self, name: str) -> Path:
        path = self.raw_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def snapshot_path(self, name: str) -> Path:
        path = self.snapshots_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_json_artifact(self, name: str, payload: Any) -> str:
        path = self.artifact_path(name)
        with self._lock:
            self._write_text_atomic(
                path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        return self.session_relative(path)

    def write_text_artifact(self, name: str, content: str) -> str:
        path = self.artifact_path(name)
        with self._lock:
            self._write_text_atomic(path, content)
        return self.session_relative(path)

    def write_bytes_artifact(self, name: str, content: bytes) -> str:
        path = self.artifact_path(name)
        with self._lock:
            self._write_bytes_atomic(path, content)
        return self.session_relative(path)

    def write_raw_json(self, name: str, payload: Any) -> str:
        path = self.raw_path(name)
        with self._lock:
            self._write_text_atomic(
                path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        return self.session_relative(path)

    def write_snapshot(self, name: str, payload: Any) -> str:
        path = self.snapshot_path(name)
        with self._lock:
            self._write_text_atomic(
                path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        return self.session_relative(path)
