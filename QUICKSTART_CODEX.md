# Codex Quickstart

这份文档只讲一件事：现在如何把 `agent-logger` 用起来，稳定采集 Codex session。

当前实现适合做 benchmark 数据采集，不是调试 UI。它会把可观测证据链落到本地 `.asg/` 目录，包括：

- 用户输入
- 模型请求和响应
- assistant 输出
- 可观测 reasoning 摘要或占位信号
- tool call 请求和结果
- Codex 本地 rollout 里的 runtime 事件
- sub-agent 相关事件，如 `spawn_agent`、`send_input`、`wait_agent`

不会承诺采集模型未暴露的隐藏思维。

## 1. 前提

你需要先满足这几个条件：

- 当前机器已经能正常运行 `codex`
- 当前目录就是这个项目根目录，也就是你 clone 下来的 `agent-logger` 目录
- `~/.codex/config.toml` 已经配置好可用 provider
- Python 版本至少 `3.11`
- 已经执行过 `pip install -e .`

先验证：

```bash
python3 --version
codex --version
```

## 2. 最小启动

如果你只是要开始采集，最推荐直接用 Codex adapter：

```bash
agent-logger codex -- -s read-only -a never exec "Reply with exactly OK."
```

这条命令会做三件事：

1. 启动一个嵌入式本地 proxy，拦截 Codex 的模型请求和响应
2. 运行 Codex 本体
3. 会话结束后补导入 Codex 本地 `rollout-*.jsonl` 和其他本地元数据

如果这条能跑通，你就已经可以开始正式采集了。

## 3. 日常使用方式

### 3.1 记录一个普通会话

```bash
agent-logger codex -- -s read-only -a never exec "Summarize this repository in 3 bullet points."
```

### 3.2 记录一个会调用工具的会话

```bash
agent-logger codex -- -s read-only -a never exec "Run pwd in the workspace using a shell command, then reply with only the absolute path."
```

### 3.3 记录一个会调用 sub-agent 的会话

```bash
agent-logger codex -- -s read-only -a never exec "Use spawn_agent to create one worker agent. After it is created, use send_input to tell that worker to reply with exactly WORKER_OK. Then use wait_agent and finally reply with exactly MAIN_OK."
```

### 3.4 指定输出目录

默认数据落到当前目录下的 `.asg/`。如果你想切到别的位置：

```bash
agent-logger codex --root /tmp/asg_runs -- -s read-only -a never exec "Reply with exactly OK."
```

### 3.5 指定工作目录

```bash
agent-logger codex --cwd /path/to/target/repo -- -s read-only -a never exec "Summarize this repo."
```

## 4. 采集结果在哪

每次运行都会生成一个 session 目录：

```text
.asg/
  sessions/
    sess_20260413_154800_aaea2d/
      manifest.json
      events.jsonl
      artifacts/
      raw/
      snapshots/
```

快速看最近一次 session：

```bash
ls -1t .asg/sessions | head -n 3
```

快速看最近一次 session 的完整路径：

```bash
python3 - <<'PY'
from pathlib import Path
sessions = sorted((Path('.asg/sessions')).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
print(sessions[0])
PY
```

快速看最近一次 session 的人类可读报告：

```bash
agent-logger render --latest
```

把报告写到指定文件：

```bash
agent-logger render --latest --output /tmp/asg_session_report.md
```

把最近一次 session 提取成 authorization benchmark seed：

```bash
agent-logger extract-authz-cases --latest
```

把抽取结果额外导出到指定位置：

```bash
agent-logger extract-authz-cases --latest --output-jsonl /tmp/authz_cases.jsonl --output-md /tmp/authz_review.md
```

对于 `agent-logger codex ...` 产生的新 session，运行过程中会持续刷新：

```text
.asg/sessions/<session_id>/events.jsonl
.asg/sessions/<session_id>/snapshots/codex_monitor_state.json
.asg/sessions/<session_id>/artifacts/authz_cases.jsonl
.asg/sessions/<session_id>/artifacts/authz_review.md
```

结束后还会补齐生成：

```text
.asg/sessions/<session_id>/artifacts/session_report.md
.asg/sessions/<session_id>/artifacts/authz_cases.jsonl
.asg/sessions/<session_id>/artifacts/authz_review.md
```

## 5. 先看哪两个文件

先看这两个：

- `manifest.json`
- `events.jsonl`

### 5.1 `manifest.json`

它回答的是这次 session 的总体信息，比如：

- `session_id`
- `trace_id`
- `command`
- `cwd`
- `codex_thread_ids`
- `codex_rollout_paths`
- `codex_rollout_entries`
- `codex_proxy_enabled`

查看方式：

```bash
python3 - <<'PY'
import json
from pathlib import Path
sessions = sorted((Path('.asg/sessions')).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
session = sessions[0]
print(json.dumps(json.loads((session / 'manifest.json').read_text()), ensure_ascii=False, indent=2))
PY
```

### 5.2 `events.jsonl`

这是主数据文件。每一行都是一个 event。它是机器侧主日志，不是为人类逐行阅读设计的。

如果你只是想快速理解一次会话，优先看：

- `artifacts/session_report.md`
- 或直接运行 `agent-logger render --latest`

查看最近一次 session 的 event type 统计：

```bash
python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path
sessions = sorted((Path('.asg/sessions')).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
session = sessions[0]
counter = Counter()
for line in (session / 'events.jsonl').open():
    obj = json.loads(line)
    counter[obj['event_type']] += 1
for key, value in counter.most_common():
    print(f'{key} {value}')
PY
```

## 6. 你会看到哪些关键事件

### 6.1 普通会话

常见事件：

- `session_started`
- `context_snapshot`
- `llm_request`
- `llm_response`
- `request_system_message`
- `request_user_message`
- `assistant_text_delta`
- `assistant_text_final`
- `final_output`
- `session_ended`

### 6.2 工具调用会话

常见事件：

- `tool_call_requested`
- `tool_call_dispatched`
- `tool_call_result`
- `tool_call_stdout`
- `tool_call_stderr`
- `tool_call_error`

注意：

- `tool_call_requested` 表示模型想调用
- `tool_call_dispatched` 表示 runtime 实际发起了调用
- `tool_call_result` 表示拿到了结果

### 6.3 Sub-agent 会话

如果 Codex 调用了 delegation 工具，你会看到：

- `subagent_spawn_requested`
- `subagent_spawned`
- `subagent_message`
- `subagent_result`
- `subagent_resumed`
- `subagent_closed`

其中 `subagent_message` 会带 `delivery_state`：

- `requested`
- `dispatched`
- `acknowledged`

查看最近一次 session 的 sub-agent 事件：

```bash
python3 - <<'PY'
import json
from pathlib import Path
sessions = sorted((Path('.asg/sessions')).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
session = sessions[0]
for line in (session / 'events.jsonl').open():
    obj = json.loads(line)
    if obj['event_type'].startswith('subagent_'):
        print(obj['event_type'])
        print(json.dumps(obj['content'], ensure_ascii=False, indent=2))
        print('---')
PY
```

## 7. 原始材料在哪

如果你要做 benchmark 数据服务，通常不只看规范事件，还要保留原始证据。

### 7.1 `raw/`

这里放的是原始 request/response JSON。

常见内容：

- `*_request.json`
- `*_response.json`

### 7.2 `artifacts/`

这里放的是：

- `tty.stdin.log`
- `tty.stdout.log`
- pretty-printed request/response
- 导入的 rollout 文件

### 7.3 `snapshots/`

这里放环境快照，比如：

- `startup_context.json`

## 8. 推荐的实际工作流

如果你现在就要开始采 benchmark 数据，建议按下面流程：

1. 先跑一个最小 smoke test，确认 `.asg/` 正常产生 session。
2. 再跑一个有工具调用的 prompt，确认 `tool_call_*` 事件完整。
3. 再跑一个 delegation prompt，确认 `subagent_*` 事件完整。
4. 确认 `manifest.json` 里有 `codex_rollout_paths`。
5. 后续正式采集时，每个 session 保留整个目录，不要只抽 `events.jsonl`。

## 9. 推荐 smoke tests

### 9.1 最小 smoke

```bash
agent-logger codex -- -s read-only -a never exec "Reply with exactly OK."
```

### 9.2 工具 smoke

```bash
agent-logger codex -- -s read-only -a never exec "Run pwd in the workspace using a shell command, then reply with only the absolute path."
```

### 9.3 delegation smoke

```bash
agent-logger codex -- -s read-only -a never exec "Use spawn_agent to create one worker agent. After it is created, use send_input to tell that worker to reply with exactly WORKER_OK. Then use wait_agent and finally reply with exactly MAIN_OK."
```

## 10. 常见问题

### 10.1 为什么会看到 `codex_state_import_error`

这是已知现象。Codex 的 `state_5.sqlite` 在某些时刻不稳定，读取可能报错。

现在不算 blocker，因为主采集链已经依赖：

- proxy 抓到的 provider request/response
- Codex 本地 rollout JSONL

### 10.2 为什么某些 session 没有 `tool_call_stdout`

因为这取决于 Codex 本地 rollout 是否真的把对应 runtime 事件落盘。

如果 rollout 里没有 `exec_command_end` 这类事件，就不会凭空生成 `tool_call_stdout`。
但通常仍然会有：

- `tool_call_requested`
- `tool_call_dispatched`
- `tool_call_result`

### 10.3 为什么不会记录隐藏思维

因为这套系统只记录可观测证据。模型没暴露出来的内部推理，不承诺采集。

### 10.4 我能不用 proxy 吗

可以，但不推荐。

你可以这样跑：

```bash
agent-logger codex --no-proxy -- -s read-only -a never exec "Reply with exactly OK."
```

这样仍然会导入 Codex 本地 rollout 和本地元数据，但你会失去 provider request/response 视角。

## 11. 你现在最该用的命令

如果你现在就开采，直接从这条开始：

```bash
agent-logger codex -- -s read-only -a never exec "Use spawn_agent to create one worker agent. After it is created, use send_input to tell that worker to reply with exactly WORKER_OK. Then use wait_agent and finally reply with exactly MAIN_OK."
```

跑完以后，立刻看最近一次 session 的：

- `manifest.json`
- `events.jsonl`

如果这里面能看到：

- `codex_rollout_imported`
- `tool_call_requested`
- `tool_call_dispatched`
- `tool_call_result`
- `subagent_spawn_requested`
- `subagent_spawned`
- `subagent_message`
- `subagent_result`

那这套采集链就已经进入可用状态了。
