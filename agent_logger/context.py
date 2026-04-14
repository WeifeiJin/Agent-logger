from __future__ import annotations

from pathlib import Path
import hashlib
import os
import platform as py_platform
import socket
import subprocess
from typing import Any


def _run_command(args: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "command not found"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _hostname_digest() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]


def collect_session_context(cwd: Path | str | None = None) -> dict[str, Any]:
    current_dir = Path(cwd or os.getcwd()).resolve()
    snapshot: dict[str, Any] = {
        "cwd": str(current_dir),
        "shell": os.environ.get("SHELL"),
        "os": py_platform.system(),
        "os_release": py_platform.release(),
        "python_version": py_platform.python_version(),
        "hostname_hash": _hostname_digest(),
    }

    code, repo_root, _ = _run_command(["git", "rev-parse", "--show-toplevel"], current_dir)
    if code != 0 or not repo_root:
        snapshot["repo_root"] = None
        snapshot["git_branch"] = None
        snapshot["git_head"] = None
        snapshot["git_status"] = []
        return snapshot

    snapshot["repo_root"] = repo_root

    _, branch, _ = _run_command(["git", "branch", "--show-current"], current_dir)
    _, head, _ = _run_command(["git", "rev-parse", "HEAD"], current_dir)
    _, status, _ = _run_command(["git", "status", "--short"], current_dir)

    snapshot["git_branch"] = branch or None
    snapshot["git_head"] = head or None
    snapshot["git_status"] = status.splitlines() if status else []
    return snapshot


def context_ref(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "cwd": snapshot.get("cwd"),
        "repo_root": snapshot.get("repo_root"),
        "git_branch": snapshot.get("git_branch"),
        "git_head": snapshot.get("git_head"),
    }

