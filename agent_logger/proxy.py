from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .canonicalize import canonicalize_request, canonicalize_response, canonicalize_response_stream
from .redaction import sanitize_headers
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

DEDUPE_TOOL_EVENT_TYPES = {
    "tool_call_requested",
    "tool_call_dispatched",
    "tool_call_result",
    "subagent_spawn_requested",
    "subagent_spawned",
    "subagent_result",
    "subagent_resumed",
    "subagent_closed",
    "subagent_message",
}


@dataclass(slots=True)
class ProxySettings:
    session_id: str
    root: Path
    upstream_url: str
    provider: str
    platform: str
    trace_id: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 0


def _join_upstream_url(base_url: str, incoming_path: str) -> tuple[str, str]:
    base = urlsplit(base_url)
    incoming = urlsplit(incoming_path)
    merged_path = base.path.rstrip("/") + incoming.path
    target = urlunsplit(
        (
            base.scheme,
            base.netloc,
            merged_path,
            incoming.query,
            incoming.fragment,
        )
    )
    target_path = urlunsplit(("", "", merged_path, incoming.query, incoming.fragment))
    return target, target_path


def _load_json_if_possible(body: bytes, headers: dict[str, str]) -> dict[str, Any] | None:
    content_type = headers.get("Content-Type", headers.get("content-type", ""))
    if "json" not in content_type.lower():
        stripped = body.lstrip()
        if not stripped.startswith((b"{", b"[")):
            return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _extract_codex_request_metadata(headers: dict[str, str]) -> tuple[str | None, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    turn_id: str | None = None
    raw = headers.get("x-codex-turn-metadata") or headers.get("X-Codex-Turn-Metadata")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        if isinstance(parsed, dict):
            metadata["codex_turn_metadata"] = parsed
            candidate_turn_id = parsed.get("turn_id")
            if isinstance(candidate_turn_id, str) and candidate_turn_id:
                turn_id = candidate_turn_id
            candidate_session_id = parsed.get("session_id")
            if isinstance(candidate_session_id, str) and candidate_session_id:
                metadata["codex_thread_id"] = candidate_session_id
    session_header = headers.get("session_id") or headers.get("Session-Id")
    if session_header:
        metadata["session_id_header"] = session_header
        metadata.setdefault("codex_thread_id", session_header)
    window_id = headers.get("x-codex-window-id") or headers.get("X-Codex-Window-Id")
    if window_id:
        metadata["codex_window_id"] = window_id
    return turn_id, metadata


def _canonical_event_dedupe_key(event: Any) -> tuple[str, str, str] | None:
    event_type = getattr(event, "event_type", None)
    if event_type not in DEDUPE_TOOL_EVENT_TYPES:
        return None
    content = getattr(event, "content", {})
    if not isinstance(content, dict):
        return None
    tool_call_id = content.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    qualifier = ""
    if event_type == "subagent_message":
        delivery_state = content.get("delivery_state")
        qualifier = str(delivery_state or "")
    return (str(event_type), tool_call_id, qualifier)


def _append_canonical_events(store: SessionStore, dedupe_keys: set[tuple[str, str, str]], events: list[Any]) -> None:
    for event in events:
        dedupe_key = _canonical_event_dedupe_key(event)
        if dedupe_key is not None:
            if dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(dedupe_key)
        store.append_event(event)


class _TraceProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    server: "_TraceHTTPServer"

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _proxy(self) -> None:
        settings = self.server.settings
        store = self.server.store

        request_length = int(self.headers.get("Content-Length", "0") or "0")
        request_body = self.rfile.read(request_length) if request_length else b""
        request_headers = {key: value for key, value in self.headers.items()}
        sanitized_request_headers = sanitize_headers(request_headers)
        turn_id, codex_metadata = _extract_codex_request_metadata(request_headers)

        request_event = make_event(
            settings.session_id,
            "llm_request",
            turn_id=turn_id,
            trace_id=settings.trace_id,
            platform=settings.platform,
            actor=ActorRef(kind="runtime", id="proxy"),
            target=TargetRef(kind="provider", name=settings.provider),
            content={
                "method": self.command,
                "path": self.path,
                "headers": sanitized_request_headers,
                "body_bytes": len(request_body),
            },
            visibility=Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False),
            platform_metadata=codex_metadata,
        )

        request_artifacts: list[str] = []
        request_payload = _load_json_if_possible(request_body, request_headers)
        if request_payload is not None:
            raw_ref = store.write_raw_json(f"{request_event.event_id}_request.json", request_payload)
            request_event.raw_ref = raw_ref
            request_artifacts.append(
                store.write_json_artifact(f"{request_event.event_id}_request.pretty.json", request_payload)
            )
        elif request_body:
            request_artifacts.append(
                store.write_bytes_artifact(f"{request_event.event_id}_request.bin", request_body)
            )
        request_event.artifacts = request_artifacts
        store.append_event(request_event)

        if request_payload is not None:
            _append_canonical_events(
                store,
                self.server.event_dedupe_keys,
                canonicalize_request(
                    request_payload,
                    session_id=settings.session_id,
                    platform=settings.platform,
                    trace_id=settings.trace_id,
                    parent_event_id=request_event.event_id,
                ),
            )

        upstream_url, _upstream_path = _join_upstream_url(settings.upstream_url, self.path)

        try:
            response, response_body, response_headers = self._forward_request(
                upstream_url=upstream_url,
                request_headers=request_headers,
                request_body=request_body,
            )
            sanitized_response_headers = sanitize_headers(response_headers)

            self.send_response(response.status, response.reason)
            for key, value in response_headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(response_body)
                self.wfile.flush()
            except BrokenPipeError:
                pass
            response_event = make_event(
                settings.session_id,
                "llm_response",
                trace_id=settings.trace_id,
                parent_event_id=request_event.event_id,
                platform=settings.platform,
                actor=ActorRef(kind="provider", id=settings.provider),
                target=TargetRef(kind="runtime", name="proxy"),
                content={
                    "status": response.status,
                    "reason": response.reason,
                    "headers": sanitized_response_headers,
                    "body_bytes": len(response_body),
                },
                visibility=Visibility(provider_exposed=True, runtime_exposed=True, user_visible=False),
            )

            response_payload = _load_json_if_possible(response_body, response_headers)
            if response_payload is not None:
                response_event.raw_ref = store.write_raw_json(
                    f"{response_event.event_id}_response.json",
                    response_payload,
                )
                response_event.artifacts.append(
                    store.write_json_artifact(
                        f"{response_event.event_id}_response.pretty.json",
                        response_payload,
                    )
                )
            else:
                response_event.artifacts.append(
                    store.write_bytes_artifact(
                        f"{response_event.event_id}_response.bin",
                        response_body,
                    )
                )
            store.append_event(response_event)

            if response_payload is not None:
                _append_canonical_events(
                    store,
                    self.server.event_dedupe_keys,
                    canonicalize_response(
                        response_payload,
                        session_id=settings.session_id,
                        platform=settings.platform,
                        trace_id=settings.trace_id,
                        parent_event_id=response_event.event_id,
                    ),
                )
            elif "text/event-stream" in response_headers.get("Content-Type", "").lower():
                _append_canonical_events(
                    store,
                    self.server.event_dedupe_keys,
                    canonicalize_response_stream(
                        response_body,
                        session_id=settings.session_id,
                        platform=settings.platform,
                        trace_id=settings.trace_id,
                        parent_event_id=response_event.event_id,
                    ),
                )
        except Exception as exc:
            error_event = make_event(
                settings.session_id,
                "proxy_error",
                trace_id=settings.trace_id,
                parent_event_id=request_event.event_id,
                platform=settings.platform,
                actor=ActorRef(kind="runtime", id="proxy"),
                target=TargetRef(kind="provider", name=settings.provider),
                content={"error": repr(exc), "path": self.path},
                visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
            )
            store.append_event(error_event)
            self.send_error(502, explain=str(exc))

    def _forward_request(
        self,
        *,
        upstream_url: str,
        request_headers: dict[str, str],
        request_body: bytes,
    ) -> tuple[Any, bytes, dict[str, str]]:
        forward_headers: dict[str, str] = {}
        for key, value in request_headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
                continue
            forward_headers[key] = value

        request = urllib.request.Request(
            upstream_url,
            data=request_body if request_body else None,
            headers=forward_headers,
            method=self.command,
        )
        opener = urllib.request.build_opener()
        try:
            response = opener.open(request, timeout=300)
        except urllib.error.HTTPError as exc:
            response = exc

        try:
            body = response.read()
            headers = dict(response.headers.items())
        finally:
            response.close()
        return response, body, headers


class _TraceHTTPServer(ThreadingHTTPServer):
    def __init__(self, settings: ProxySettings, store: SessionStore) -> None:
        super().__init__((settings.listen_host, settings.listen_port), _TraceProxyHandler)
        self.settings = settings
        self.store = store
        self.event_dedupe_keys: set[tuple[str, str, str]] = set()


class EmbeddedTraceProxy:
    def __init__(self, settings: ProxySettings, store: SessionStore | None = None) -> None:
        self.settings = settings
        self.store = store or SessionStore(settings.root, settings.session_id)
        self.httpd = _TraceHTTPServer(settings, self.store)
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.settings.listen_host}:{self.port}"

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
