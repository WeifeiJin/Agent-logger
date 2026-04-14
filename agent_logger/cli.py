from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import time

from .authz_cases import build_authz_cases, generate_authz_case_artifacts, render_authz_review, serialize_authz_cases
from .codex_adapter import run_codex_session
from .ids import make_session_id, make_trace_id
from .launcher import LaunchConfig, launch_session
from .proxy import EmbeddedTraceProxy, ProxySettings
from .render import build_session_report, resolve_session_dir
from .schema import ActorRef, TargetRef, Visibility, make_event
from .store import SessionStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asg", description="Agent trace logger")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Wrap and record an agent session")
    run_parser.add_argument("--agent", required=True, help="Logical agent name")
    run_parser.add_argument("--root", default=".asg", help="Logger root directory")
    run_parser.add_argument("--cwd", default=".", help="Working directory for the child process")
    run_parser.add_argument("--provider", default=None, help="Provider name for proxy metadata")
    run_parser.add_argument("--upstream-url", default=None, help="Upstream provider base URL")
    run_parser.add_argument(
        "--base-url-env",
        default=None,
        help="Environment variable name to inject with the embedded proxy URL",
    )
    run_parser.add_argument("--proxy-host", default="127.0.0.1")
    run_parser.add_argument("--proxy-port", type=int, default=0)
    run_parser.add_argument("child_command", nargs=argparse.REMAINDER)

    codex_parser = subparsers.add_parser("codex", help="Wrap Codex with a Codex-aware adapter")
    codex_parser.add_argument("--root", default=".asg", help="Logger root directory")
    codex_parser.add_argument("--cwd", default=".", help="Working directory for Codex")
    codex_parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Do not inject the embedded model proxy; still import Codex local metadata",
    )
    codex_parser.add_argument(
        "--upstream-url",
        default=None,
        help="Override the upstream base URL instead of using the active Codex provider config",
    )
    codex_parser.add_argument("codex_args", nargs=argparse.REMAINDER)

    proxy_parser = subparsers.add_parser("proxy", help="Run a standalone trace proxy")
    proxy_parser.add_argument("--session-id", default=None, help="Existing or new session id")
    proxy_parser.add_argument("--root", default=".asg", help="Logger root directory")
    proxy_parser.add_argument("--platform", default="proxy")
    proxy_parser.add_argument("--provider", required=True)
    proxy_parser.add_argument("--upstream-url", required=True)
    proxy_parser.add_argument("--host", default="127.0.0.1")
    proxy_parser.add_argument("--port", type=int, default=8787)

    render_parser = subparsers.add_parser("render", help="Render a human-readable session report")
    render_parser.add_argument("--root", default=".asg", help="Logger root directory")
    render_parser.add_argument("--session-id", default=None, help="Session id under <root>/sessions")
    render_parser.add_argument("--session-dir", default=None, help="Absolute or relative path to a session directory")
    render_parser.add_argument("--latest", action="store_true", help="Render the most recently modified session")
    render_parser.add_argument("--include-noisy", action="store_true", help="Include noisy low-level events such as tty chunks and deltas")
    render_parser.add_argument("--output", default=None, help="Optional output path for the rendered report")

    extract_parser = subparsers.add_parser("extract-authz-cases", help="Extract benchmark-oriented authorization case seeds from a session")
    extract_parser.add_argument("--root", default=".asg", help="Logger root directory")
    extract_parser.add_argument("--session-id", default=None, help="Session id under <root>/sessions")
    extract_parser.add_argument("--session-dir", default=None, help="Absolute or relative path to a session directory")
    extract_parser.add_argument("--latest", action="store_true", help="Extract from the most recently modified session")
    extract_parser.add_argument("--output-jsonl", default=None, help="Optional output path for the extracted case JSONL")
    extract_parser.add_argument("--output-md", default=None, help="Optional output path for the human review markdown")
    extract_parser.add_argument("--print-review", action="store_true", help="Print the review markdown to stdout after extraction")
    return parser


def _strip_remainder_delimiter(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _cmd_run(args: argparse.Namespace) -> int:
    child_command = _strip_remainder_delimiter(args.child_command)
    if not child_command:
        raise SystemExit("missing child command after `asg run`")
    config = LaunchConfig(
        agent=args.agent,
        command=child_command,
        root=Path(args.root),
        cwd=Path(args.cwd).resolve(),
        provider=args.provider,
        upstream_url=args.upstream_url,
        base_url_env=args.base_url_env,
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
    )
    return launch_session(config).exit_code


def _cmd_codex(args: argparse.Namespace) -> int:
    codex_args = _strip_remainder_delimiter(args.codex_args)
    result = run_codex_session(
        root=Path(args.root),
        cwd=Path(args.cwd).resolve(),
        user_args=codex_args,
        enable_proxy=not args.no_proxy,
        upstream_url=args.upstream_url,
    )
    return result.exit_code


def _cmd_render(args: argparse.Namespace) -> int:
    session_dir = resolve_session_dir(
        root=Path(args.root),
        session_id=args.session_id,
        session_dir=Path(args.session_dir).resolve() if args.session_dir else None,
        latest=bool(args.latest),
    )
    report = build_session_report(
        session_dir=session_dir,
        include_noisy=bool(args.include_noisy),
    )
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(output_path)
    else:
        sys.stdout.write(report)
    return 0


def _cmd_extract_authz_cases(args: argparse.Namespace) -> int:
    session_dir = resolve_session_dir(
        root=Path(args.root),
        session_id=args.session_id,
        session_dir=Path(args.session_dir).resolve() if args.session_dir else None,
        latest=bool(args.latest),
    )
    cases = build_authz_cases(session_dir=session_dir)
    review = render_authz_review(session_dir=session_dir, cases=cases)

    extraction = None
    if session_dir.parent.name == "sessions":
        store = SessionStore(session_dir.parent.parent, session_dir.name)
        extraction = generate_authz_case_artifacts(store, cases=cases, review=review)

    if args.output_jsonl:
        output_jsonl = Path(args.output_jsonl).resolve()
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        output_jsonl.write_text(serialize_authz_cases(cases), encoding="utf-8")
        print(output_jsonl)
    if args.output_md:
        output_md = Path(args.output_md).resolve()
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(review, encoding="utf-8")
        print(output_md)

    if extraction is not None:
        print(session_dir / str(extraction["cases"]))
        print(session_dir / str(extraction["review"]))
        print(f"cases={extraction['case_count']}")
    elif not args.output_jsonl and not args.output_md:
        sys.stdout.write(review)

    if args.print_review and (args.output_jsonl or args.output_md or extraction is not None):
        sys.stdout.write(review)
    return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    session_id = args.session_id or make_session_id()
    trace_id = make_trace_id()
    root = Path(args.root)
    store = SessionStore(root, session_id)
    store.write_manifest(
        {
            "session_id": session_id,
            "trace_id": trace_id,
            "platform": args.platform,
            "provider": args.provider,
            "upstream_url": args.upstream_url,
            "mode": "standalone_proxy",
        }
    )

    settings = ProxySettings(
        session_id=session_id,
        root=root,
        upstream_url=args.upstream_url,
        provider=args.provider,
        platform=args.platform,
        trace_id=trace_id,
        listen_host=args.host,
        listen_port=args.port,
    )
    proxy = EmbeddedTraceProxy(settings, store=store)
    proxy.start()

    store.append_event(
        make_event(
            session_id,
            "proxy_started",
            trace_id=trace_id,
            platform=args.platform,
            actor=ActorRef(kind="runtime", id="cli"),
            target=TargetRef(kind="proxy", name="standalone_proxy"),
            content={"listen_url": proxy.url, "upstream_url": args.upstream_url},
            visibility=Visibility(provider_exposed=False, runtime_exposed=True, user_visible=False),
        )
    )

    print(proxy.url)

    should_stop = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not should_stop:
            time.sleep(0.2)
    finally:
        proxy.stop()
        store.append_event(
            make_event(
                session_id,
                "proxy_stopped",
                trace_id=trace_id,
                platform=args.platform,
                actor=ActorRef(kind="runtime", id="cli"),
                target=TargetRef(kind="proxy", name="standalone_proxy"),
                content={"listen_url": proxy.url},
                visibility=Visibility(
                    provider_exposed=False,
                    runtime_exposed=True,
                    user_visible=False,
                ),
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "codex":
        return _cmd_codex(args)
    if args.command == "proxy":
        return _cmd_proxy(args)
    if args.command == "render":
        return _cmd_render(args)
    if args.command == "extract-authz-cases":
        return _cmd_extract_authz_cases(args)
    raise SystemExit(f"unknown subcommand: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
