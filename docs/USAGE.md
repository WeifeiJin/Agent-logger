# Usage Guide

This guide explains how to use `agent-logger` as a practical trace collection tool, with an emphasis on Codex sessions and benchmark-oriented review workflows.

## 1. Mental Model

`agent-logger` is not a UI layer on top of your agent. It is a local recorder.

For each wrapped run, it creates a session directory under `.asg/sessions/<session_id>/` and stores:

- machine-oriented event logs
- raw request and response payloads when available
- runtime artifacts
- derived review files such as session reports and authorization case summaries

The source of truth is always `events.jsonl`. Rendered markdown and extracted cases are downstream artifacts built from that log.

## 2. Installation

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

This provides two equivalent CLI entry points:

- `agent-logger`
- `asg`

Examples in this guide use `agent-logger`.

## 3. Core Commands

Top-level commands:

- `agent-logger run`
- `agent-logger codex`
- `agent-logger proxy`
- `agent-logger render`
- `agent-logger extract-authz-cases`

You can inspect command help directly:

```bash
agent-logger --help
agent-logger codex --help
agent-logger run --help
agent-logger render --help
agent-logger extract-authz-cases --help
```

## 4. The Fastest Useful Start

If you want a working end-to-end Codex trace as quickly as possible:

```bash
agent-logger codex -- -a never exec "Reply with exactly OK."
```

This does three things:

- launches a local embedded proxy for provider traffic
- runs Codex with a Codex-aware adapter
- imports local Codex runtime artifacts during and after the session

After it finishes, inspect:

- `.asg/sessions/<latest>/manifest.json`
- `.asg/sessions/<latest>/events.jsonl`
- `.asg/sessions/<latest>/artifacts/session_report.md`

## 5. Recommended Codex Workflows

### 5.1 Capture a one-shot non-interactive run

```bash
agent-logger codex -- -a never exec "Summarize this repository in three bullets."
```

### 5.2 Capture a run that should invoke a tool

```bash
agent-logger codex -- -a never exec "Use exec_command to run pwd, then reply with exactly OK."
```

This is a good smoke test because it usually produces:

- `tool_call_requested`
- `tool_call_dispatched`
- `tool_call_result`

### 5.3 Capture a run in another repository

```bash
agent-logger codex --cwd /path/to/target/repo -- -a never exec "Summarize this repository."
```

This is the preferred way to use `agent-logger` when the logger itself lives in a different directory than the project you want to inspect.

### 5.4 Change the logger root

By default, output is written under `.asg/` in the current working directory.

To store runs elsewhere:

```bash
agent-logger codex --root /tmp/asg-runs -- -a never exec "Reply with exactly OK."
```

### 5.5 Run without the embedded proxy

```bash
agent-logger codex --no-proxy -- -a never exec "Reply with exactly OK."
```

This still imports local Codex runtime artifacts, but you lose provider-side request and response capture.

Use this only if the embedded proxy is the thing you explicitly want to avoid.

## 6. Live Session Behavior

For `agent-logger codex`, some outputs are refreshed while the session is still running.

In practice, this means these files can become useful before process exit:

```text
.asg/sessions/<session_id>/events.jsonl
.asg/sessions/<session_id>/snapshots/codex_monitor_state.json
.asg/sessions/<session_id>/artifacts/authz_cases.jsonl
.asg/sessions/<session_id>/artifacts/authz_review.md
```

When the session ends, `agent-logger` runs a final reconciliation pass and then writes:

```text
.asg/sessions/<session_id>/artifacts/session_report.md
```

## 7. Session Layout

Each session directory contains:

```text
.asg/
  sessions/
    <session_id>/
      manifest.json
      events.jsonl
      artifacts/
      raw/
      snapshots/
```

The important files are:

- `manifest.json`
  Summary metadata for the run: command, cwd, provider, thread ids, rollout paths, and adapter state.

- `events.jsonl`
  Canonical machine-oriented event log. Every line is one event.

- `artifacts/session_report.md`
  Human-readable summary view of the session.

- `artifacts/authz_cases.jsonl`
  Action-centered extracted cases for review or later annotation.

- `artifacts/authz_review.md`
  Markdown summary of the extracted authorization-oriented cases.

- `raw/*`
  Best-effort storage of raw provider payloads.

- `snapshots/*`
  Session-side state such as startup environment and monitor checkpoints.

## 8. Reading the Data

### 8.1 `manifest.json`

Use this first when you need context for a run:

- which command was wrapped
- which working directory was used
- whether proxying was enabled
- which Codex rollout paths were associated with the session

### 8.2 `events.jsonl`

Use this when you need the full evidence chain.

Typical event categories include:

- lifecycle events such as `session_started` and `session_ended`
- provider-side events such as `llm_request` and `llm_response`
- prompt-side messages such as `request_system_message` and `request_user_message`
- assistant output such as `assistant_text_final`
- tool activity such as `tool_call_requested`, `tool_call_dispatched`, `tool_call_result`
- Codex runtime events such as `codex_turn_context`, `codex_task_started`, `codex_task_complete`
- sub-agent events such as `subagent_spawn_requested`, `subagent_spawned`, `subagent_message`, `subagent_result`

### 8.3 Rendered report

If you want a readable summary instead of parsing JSONL directly:

```bash
agent-logger render --latest
agent-logger render --latest --output /tmp/session_report.md
```

## 9. Authorization Case Extraction

The `extract-authz-cases` command is intended for benchmark curation and review, not automatic truth labeling.

Extract from the latest run:

```bash
agent-logger extract-authz-cases --latest
```

Extract from a specific session and write files elsewhere:

```bash
agent-logger extract-authz-cases \
  --session-id <session_id> \
  --output-jsonl /tmp/authz_cases.jsonl \
  --output-md /tmp/authz_review.md
```

The extracted JSONL is organized around observed action candidates, especially tool calls. Each case includes:

- the action itself
- nearby user messages
- prompt-visible user and system messages
- environment context snippets
- related tool outputs
- conservative heuristic hints

The extractor intentionally does not assign final labels such as authorized versus unauthorized. That step belongs in annotation or later evaluation logic.

## 10. Generic Wrapper Mode

If you want to log another local CLI, use `run`.

Example:

```bash
agent-logger run \
  --agent my-agent \
  --cwd /path/to/project \
  --provider openai \
  --upstream-url https://api.openai.com \
  --base-url-env OPENAI_BASE_URL \
  -- my-agent-cli
```

This is useful when a tool can be pointed at a local proxy via an environment variable.

## 11. Standalone Proxy Mode

You can run only the proxy and wire a client to it yourself:

```bash
agent-logger proxy \
  --session-id sess_demo \
  --provider openai \
  --upstream-url https://api.openai.com
```

This is the lowest-level mode and is mainly useful for debugging integrations or wrapping tools that you launch separately.

## 12. Benchmark-Oriented Workflow

A practical review loop usually looks like this:

1. Run the agent with `agent-logger codex ...` or `agent-logger run ...`.
2. Confirm the session directory exists and contains `manifest.json` and `events.jsonl`.
3. Render a session report if you want a quick human-readable summary.
4. Extract authorization cases with `extract-authz-cases`.
5. Review `authz_review.md`.
6. Use `authz_cases.jsonl` as the seed dataset for later annotation or downstream evaluation.

## 13. Limits and Caveats

- Hidden chain-of-thought is not guaranteed to be available.
- Some runtime details depend on what the underlying agent actually persists locally.
- Without proxy mode, provider request and response capture is absent.
- Derived artifacts are snapshots of the current event log and should not be treated as a stronger source of truth than `events.jsonl`.
- Runtime support is strongest for Codex today.

## 14. Testing

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run a very small smoke test:

```bash
agent-logger codex -- -a never exec "Use exec_command to run pwd, then reply with exactly OK."
```

## 15. Where To Go Next

- For a shorter Codex-specific walkthrough, see [../QUICKSTART_CODEX.md](../QUICKSTART_CODEX.md).
- For contribution and implementation details, start from the source files under `agent_logger/` and the tests under `tests/`.
