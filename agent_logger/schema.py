from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from typing import Any

from .ids import make_event_id, utc_timestamp


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


@dataclass(slots=True)
class ActorRef:
    kind: str
    id: str | None = None


@dataclass(slots=True)
class TargetRef:
    kind: str
    name: str | None = None


@dataclass(slots=True)
class Visibility:
    provider_exposed: bool = False
    runtime_exposed: bool = True
    user_visible: bool = False


@dataclass(slots=True)
class Event:
    session_id: str
    event_type: str
    timestamp: str = field(default_factory=utc_timestamp)
    event_id: str = field(default_factory=make_event_id)
    turn_id: str | None = None
    parent_event_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    platform: str | None = None
    actor: ActorRef | None = None
    target: TargetRef | None = None
    content: dict[str, Any] = field(default_factory=dict)
    context_ref: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    raw_ref: str | None = None
    visibility: Visibility = field(default_factory=Visibility)
    platform_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


def make_event(
    session_id: str,
    event_type: str,
    *,
    timestamp: str | None = None,
    turn_id: str | None = None,
    parent_event_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    platform: str | None = None,
    actor: ActorRef | None = None,
    target: TargetRef | None = None,
    content: dict[str, Any] | None = None,
    context_ref: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
    raw_ref: str | None = None,
    visibility: Visibility | None = None,
    platform_metadata: dict[str, Any] | None = None,
) -> Event:
    return Event(
        session_id=session_id,
        event_type=event_type,
        timestamp=timestamp or utc_timestamp(),
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        trace_id=trace_id,
        span_id=span_id,
        platform=platform,
        actor=actor,
        target=target,
        content=content or {},
        context_ref=context_ref or {},
        artifacts=artifacts or [],
        raw_ref=raw_ref,
        visibility=visibility or Visibility(),
        platform_metadata=platform_metadata or {},
    )
