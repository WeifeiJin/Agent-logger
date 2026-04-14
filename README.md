# Agent Logger

`agent-logger` is a local trace capture tool for coding agents.

It is designed for benchmark construction and post-hoc analysis, not for hidden instrumentation inside the model runtime. The tool records observable evidence only:

- user input
- provider requests and responses
- assistant output
- tool call requests and results
- local runtime artifacts such as Codex rollout events
- sub-agent orchestration events when the runtime exposes them

Current first-class support is for Codex. The generic launcher and proxy also make it usable as a wrapper around other local agent CLIs.

## Why This Exists

For agent evaluation work, the hard part is usually not generating a transcript. The hard part is preserving the evidence chain around each action:

- what the user actually asked
- what the model saw in prompt context
- what tools were invoked
- what external outputs may have influenced later behavior
- whether an action looked merely related to the task or actually authorized

`agent-logger` writes that evidence to disk in a stable session layout under `.asg/`.

## Principles

- Observable only: it does not claim access to hidden model reasoning.
- Local by default: captured data stays on disk unless you move it elsewhere.
- Evidence first: raw event logs remain the source of truth.
- Benchmark-oriented: derived artifacts are meant to help curation, not replace annotation.

## Install

Clone the repo, then install it in editable mode:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

This installs two equivalent CLI entry points:

- `agent-logger`
- `asg`

`asg` is kept as a short compatibility alias.

## Quick Start

Record a Codex session:

```bash
agent-logger codex -- -a never exec "Reply with exactly OK."
```

During the session, the tool incrementally refreshes:

```text
.asg/sessions/<session_id>/events.jsonl
.asg/sessions/<session_id>/snapshots/codex_monitor_state.json
.asg/sessions/<session_id>/artifacts/authz_cases.jsonl
.asg/sessions/<session_id>/artifacts/authz_review.md
```

When the session ends, it also writes:

```text
.asg/sessions/<session_id>/artifacts/session_report.md
```

Render the latest session as a readable report:

```bash
agent-logger render --latest
```

Extract benchmark-oriented authorization cases from the latest session:

```bash
agent-logger extract-authz-cases --latest
```

The more detailed Codex walkthrough is in [QUICKSTART_CODEX.md](QUICKSTART_CODEX.md).

## Session Layout

Each run creates a session directory:

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

Key files:

- `events.jsonl`: canonical machine-oriented log
- `manifest.json`: top-level metadata about the run
- `artifacts/session_report.md`: readable timeline summary
- `artifacts/authz_cases.jsonl`: action-centered benchmark seed cases
- `artifacts/authz_review.md`: reviewer-facing markdown summary of extracted cases

## Supported Flows

- `agent-logger codex`: Codex-aware adapter with provider proxying and local rollout import
- `agent-logger run`: generic command wrapper for local agent CLIs
- `agent-logger proxy`: standalone trace proxy
- `agent-logger render`: render a human-readable session report
- `agent-logger extract-authz-cases`: derive authorization benchmark seeds from raw events

## Status

Implemented now:

- event schema and JSONL storage
- context snapshot capture
- terminal I/O capture
- provider request/response recording with best-effort canonicalization
- realtime Codex rollout and history ingestion during a live session
- Codex tool and sub-agent event capture when exposed by runtime artifacts
- benchmark-oriented authorization case extraction

Not implemented yet:

- first-class Claude Code adapter
- stronger normalization of resources and actions across runtimes
- richer reviewer workflow beyond markdown and JSONL artifacts
- any claim of complete hidden reasoning capture

## Development

Run the unit tests:

```bash
python3 -m unittest discover -s tests -v
```

If you want a very small smoke run:

```bash
agent-logger codex -- -a never exec "Use exec_command to run pwd, then reply with exactly OK."
```
