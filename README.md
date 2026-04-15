# agent-logger

`agent-logger` is a local trace capture tool for coding agents.

It records observable runtime evidence for agent sessions and stores it on disk in a stable session layout. The primary use case is evaluation and benchmark construction, especially when you need more than a plain transcript:

- user messages
- provider requests and responses
- assistant output
- tool call requests and results
- runtime-level events exposed by the agent
- derived review artifacts for human inspection

Current runtime coverage:

- Codex: first-class support with realtime local artifact ingestion
- Claude Code: dedicated adapter, with structured capture for headless `claude -p` runs
- OpenClaw: dedicated adapter, with cache-trace capture and optional proxy-backed provider interception
- Other CLIs: generic wrapper mode through `agent-logger run`

## What It Is For

`agent-logger` is useful when you need to answer questions like:

- What exactly did the user ask before a tool call happened?
- What prompt-side instructions were visible to the model?
- Which tool call was requested, dispatched, and completed?
- Which later actions may have been influenced by earlier tool output?
- Which action candidates should be reviewed for authorization or scope drift?

## What It Does Not Claim

- It does not claim access to hidden model reasoning.
- It only records evidence that is observable through the provider API, local runtime artifacts, terminal I/O, or explicit adapter hooks.
- Derived artifacts are review aids, not ground-truth labels.

## Features

- Session-oriented JSONL event store under `.asg/`
- Human-readable session report rendering
- Realtime Codex trace ingestion during live sessions
- Claude Code headless stream-json ingestion
- OpenClaw cache-trace ingestion with optional proxy routing
- Tool and sub-agent event capture when exposed by the runtime
- Benchmark-oriented authorization case extraction
- Generic command wrapper and standalone trace proxy modes

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

This installs two equivalent CLI entry points:

- `agent-logger`
- `asg`

`asg` is retained as a short compatibility alias.

## Quick Start

Record a Codex session:

```bash
agent-logger codex -- -a never exec "Reply with exactly OK."
```

Record a Claude Code headless session:

```bash
agent-logger claude -- -p "Reply with exactly OK."
```

Record an OpenClaw session with cache-trace capture:

```bash
agent-logger openclaw -- <your-openclaw-args>
```

Render the latest session into a readable report:

```bash
agent-logger render --latest
```

Extract authorization-oriented review cases from the latest session:

```bash
agent-logger extract-authz-cases --latest
```

## Session Output

Each run creates a directory like:

```text
.asg/
  sessions/
    <session_id>/
      manifest.json
      events.jsonl
      artifacts/
        session_report.md
        authz_cases.jsonl
        authz_review.md
      raw/
      snapshots/
```

During a live `agent-logger codex ...` session, the logger incrementally refreshes:

```text
.asg/sessions/<session_id>/events.jsonl
.asg/sessions/<session_id>/snapshots/codex_monitor_state.json
.asg/sessions/<session_id>/artifacts/authz_cases.jsonl
.asg/sessions/<session_id>/artifacts/authz_review.md
```

## Commands

- `agent-logger codex`: Codex-aware adapter with provider proxying and local rollout import
- `agent-logger claude`: Claude Code adapter with headless structured capture when using `-p`
- `agent-logger openclaw`: OpenClaw adapter with generated overlay config, cache-trace capture, and optional proxy mode
- `agent-logger run`: generic command wrapper for local agent CLIs
- `agent-logger proxy`: standalone trace proxy
- `agent-logger render`: render a human-readable session report
- `agent-logger extract-authz-cases`: extract benchmark-oriented authorization review cases

## Documentation

- [Detailed usage guide](docs/USAGE.md)
- [Codex quickstart](QUICKSTART_CODEX.md)

## Current Status

Implemented now:

- event schema and JSONL storage
- context snapshot capture
- terminal I/O capture
- provider request/response recording with best-effort canonicalization
- realtime Codex rollout and history ingestion during a live session
- Codex tool and sub-agent event capture when exposed by local runtime artifacts
- Claude Code structured headless capture through `--output-format stream-json`
- Anthropic request/response/stream canonicalization
- OpenClaw cache-trace ingestion and optional proxy-backed provider routing
- authorization case extraction for benchmark curation

Important limitations:

- Claude Code interactive TUI sessions are currently recorded at the terminal level; structured event capture is best on headless `claude -p` runs.
- OpenClaw structured capture relies on generated overlay config and cache-trace output. Proxy mode is optional and requires explicit `--upstream-url`, `--provider-api`, and `--model-id`.
- Different runtimes expose different amounts of internal state; `agent-logger` only captures what is locally observable.

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```
