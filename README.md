 # qwen-local-delegate-mcp

Cut Claude Code costs by delegating mechanical work to a local Qwen model.
Claude plans and reviews; Qwen does the grunt work (bulk renames, docstrings,
boilerplate, test scaffolding) locally, for free.

Every `qwen_send` runs on your machine at **zero API cost**. Claude still bills
for its own turns, but it only sees Qwen's final reply rather than the whole
iteration transcript, so its context stays small and its turns stay short. The
savings are biggest on work that iterates: refactors, docstring passes, test
scaffolding.

Sessions are stateful: Qwen keeps context across calls within a session, so
delegating feels like handing off to a coworker rather than querying a stateless
oracle.

## Architecture

```
   ┌─────────────────────┐
   │     Claude Code     │  plans the work, reviews every reply
   └──────────┬──────────┘
              │
              │  MCP over HTTP, localhost:11435
              │  qwen_start_session / qwen_send / qwen_end_session ...
              ▼
   ┌─────────────────────────────┐  write   ┌──────────────────────────┐
   │   qwen-local-delegate-mcp   │─────────►│  data/sessions/<id>.json │
   │                             │          │                          │
   │  owns session state in RAM  │◄─────────│  one file per session,   │
   │  one message list per id    │   load   │  reloaded on restart     │
   └──────────┬──────────────────┘          └──────────────────────────┘
              │
              │  HTTP, localhost:11434
              │  POST /api/chat, full history replayed each turn
              ▼
   ┌─────────────────────────────┐
   │           Ollama            │
   │  ┌───────────────────────┐  │
   │  │    Qwen3.6-35B-A3B    │  │  35B params total, 3B active (MoE)
   │  │  resident in RAM via  │  │  stays loaded between calls, so only
   │  │     keep_alive=30m    │  │  the first call pays load time
   │  └───────────────────────┘  │
   └─────────────────────────────┘
```

**Claude Code** sees six tools and calls them like it would `Read` or `Bash`.

**The MCP server** owns session state. Each session is a message list keyed by a
short `session_id`. Every `qwen_send` replays the full history to Ollama so Qwen
has full context, while Claude's own context stays small: it only ever sees the
latest reply, never the accumulated transcript. That gap is where the savings
come from.

**Ollama** hosts the model and keeps it resident, so repeated turns within a
session are fast.

It runs as an HTTP daemon rather than stdio because session state lives in
memory and there must be exactly one copy. Under stdio, every Claude Code
instance spawns its own server, so `qwen_list_sessions` could not find a session
you opened in another project. `MCP_TRANSPORT=stdio` is fine for a single
client, and the tests use it.

Replies stream: a long generation reports progress every few seconds so the
client stays alive instead of blocking silently for minutes. State is persisted
per turn, and a failed `qwen_send` rolls back the message it appended, so a
mid-task crash leaves the session consistent and safe to retry.

## Setup

Prerequisites: macOS, Homebrew, Claude Code, `uv` (`brew install uv`).

**1. Install.**

```bash
git clone https://github.com/tilayealemu/qwen-local-delegate-mcp.git
cd qwen-local-delegate-mcp
./install.sh          # idempotent, safe to re-run
```

This installs Ollama, pulls `qwen3.6:35b-a3b` (~24 GB, one time), loads a
LaunchAgent on port 11435, and registers the server with `claude mcp add`.
Verify with `claude mcp get qwen-local-delegate`, which should report
`Status: ✔ Connected`.

**Using a smaller model.** The default wants roughly 24 GB resident, which is
more than a 16 GB machine has to spare. `install.sh` honors `QWEN_MODEL`, so set
it once at install time and the installer pulls that tag *and* bakes it into the
LaunchAgent as the daemon's default:

```bash
QWEN_MODEL=qwen3:30b-a3b ./install.sh   # ~19 GB, same MoE shape as the default
QWEN_MODEL=qwen3:8b      ./install.sh   # ~5 GB, dense, comfortable on 16 GB
```

Any tag from [ollama.com/library](https://ollama.com/library) works. Smaller
models make worse deviations from a brief, so lean harder on the verification
rules in [When to delegate](#when-to-delegate).

To change models after installing, re-run `install.sh` with a new `QWEN_MODEL`,
or `ollama pull <tag>` and pass `model` to `qwen_start_session` for a single
session without disturbing the default.

**2. Update your CLAUDE.md. This is required.** Installing the server is not
enough. Without guidance Claude will not reliably use these tools, and a bare
*"delegate to Qwen"* can send it looking for a nonexistent `qwen` CLI. Copy the
block from [`CLAUDE.md`](./CLAUDE.md) into `~/.claude/CLAUDE.md` for every
project, or into a single project's own `CLAUDE.md`.

**3. Restart Claude Code.** Already-open sessions will not see the tools until
they restart. In a new session, `/mcp` should list `qwen-local-delegate` with six
tools.

**4. Sanity-check anytime.** `./verify.sh` verifies the whole pipeline —
Ollama, the model, the LaunchAgent, the MCP server, and the CLAUDE.md
guidance — in one read-only pass.

## Use

1. Ask for mechanical work as you normally would. With CLAUDE.md in place,
   Claude delegates on its own. To force it, be explicit:
   > *"Open a qwen session for this refactor and use the same one across all
   > files. Verify each file after Qwen edits it."*
2. Claude opens one session, iterates with Qwen, reviews the output, and closes
   the session when the task is done.
3. Check in anytime. `/mcp` lists the tools, and asking Claude to run
   `qwen_list_sessions` shows what is still open.

## When to delegate

Notes from real use. We keep updating this as we learn.

**Choose by verification cost, not task size.** If checking Qwen's output costs
about what writing it yourself would, delegating adds work instead of saving it.
Delegation optimizes the abundant resource (typing code) and taxes the scarce
one (verifying it).

**What pays:** many near-identical mechanical units with a cheap verifier
already in place. One good brief, N outputs, `npm test` as the oracle. Bulk
docstrings, renames across 40 files, format conversions, test scaffolding from a
fixed template, first drafts.

**What does not:** anything whose failure mode is silent. A textbook-looking
formula that passes toy unit tests and is quietly wrong on real data is exactly
what a cheap model writes and a cheap verifier green-lights. Also skip anything
needing images (this path is text only), anything where writing the brief costs
more than writing the code, and anything needing codebase context you would have
to paste in wholesale.

**In practice**

- State the codebase's style conventions in the brief (e.g. semicolon-free,
  prefer `const`). Qwen's logic is usually sound; its deviations tend to be
  cosmetic, and a one-line note heads them off before you fix them by hand.
- Read `qwen_get_history` before `qwen_end_session`. Closing deletes the
  transcript, and that transcript is your only evidence of whether delegation
  worked.
- Qwen can crash mid-task. Anything routed through it should be resumable.
- A `qwen_send` timeout is not a failure. The MCP call can idle out while Ollama
  keeps generating, and the finished reply still lands in the transcript. Call
  `qwen_get_history` before re-asking — re-asking blind double-generates and
  desyncs the session. Keep each turn's output to ≲150 lines to avoid it.
- If a subagent drives Qwen, that hop only pays when Qwen's output is bulky
  enough to be worth keeping out of the orchestrator's context. For a 20-line
  fix the round trip is pure overhead.
- Delegation is not a goal in itself. Plenty of sessions contain almost no bulk
  code-writing, and forcing the rule there makes the work worse, not cheaper.

## Tools

| tool | purpose |
|---|---|
| `qwen_start_session(topic, system_prompt, model)` | Open a stateful chat; returns `session_id`. |
| `qwen_send(session_id, message, files=None)` | Send a turn. Full history is replayed to Ollama; the reply streams, with progress notifications on long runs. |
| `qwen_get_history(session_id, tail=0)` | Read the transcript; `tail=N` for the last N. |
| `qwen_list_sessions()` | List active sessions. |
| `qwen_list_models()` | List Ollama models pulled locally, with the configured default. |
| `qwen_end_session(session_id)` | Close and delete state. Idempotent. |

One session per coherent task; reuse the `session_id` across follow-ups.

**Passing files without spending your own context.** `qwen_send`'s optional
`files` argument takes absolute paths; the server reads them off disk and
attaches their contents to the message itself, so file bytes go straight into
Qwen's prompt without ever landing in Claude's context:

```
qwen_send(session_id, "Summarize what this file does.", files=["/abs/path/large_module.py"])
```

Prefer this over `Read`-then-paste whenever the point of delegating is to keep
the file's bulk out of your own context — that's most of the time. Attachment
size is not a timeout risk: the ~150-line-per-turn guidance is about how much
Qwen *generates*, and `files` is prompt input, not output.

Text files only, up to `QWEN_MAX_ATTACH_BYTES` (2 MB) across all entries.
Relative paths, missing paths, binaries, and oversized attachments are all
rejected before Ollama is called, so a bad entry costs a fast error rather than
a wasted generation or a silently truncated prompt.

## Configuration

Set in the LaunchAgent plist or your shell.

| var | default | purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama listens. |
| `QWEN_MODEL` | `qwen3.6:35b-a3b` | Default model tag. Written into the plist by `install.sh`; see [Setup](#setup). |
| `QWEN_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded. |
| `QWEN_TIMEOUT` | `600` | HTTP timeout per `/api/chat` call, seconds. |
| `QWEN_PROGRESS_INTERVAL` | `10` | Seconds between progress notifications during a stream. |
| `QWEN_MAX_ATTACH_BYTES` | `2000000` | Total bytes `qwen_send`'s `files` may attach per turn. |
| `QWEN_DATA_DIR` | `<repo>/data/sessions` | Where session JSON lives. |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `11435` | MCP server bind address. |
| `MCP_TRANSPORT` | `streamable-http` | `stdio` for local testing. |

## Troubleshooting

**Start here:** `./verify.sh` checks Ollama, the model, the LaunchAgent, the
MCP server, and the CLAUDE.md guidance in one pass and tells you which one is
broken. It's read-only — safe to run anytime.

**Claude looks for a `qwen` CLI.** You skipped the CLAUDE.md step, or Claude
Code was already running at install. Update it and restart.

**`✘ Failed to connect`.** Something else holds the port. Check with
`lsof -iTCP:11435 -sTCP:LISTEN`, then change `MCP_PORT` in the plist, reload it,
and re-register:

```bash
claude mcp remove qwen-local-delegate --scope user
claude mcp add --transport http --scope user qwen-local-delegate http://localhost:<port>/mcp
```

**`qwen_send` fails.** The error says which case it is: can't reach Ollama
(`ollama serve`), no such model (`ollama pull`), or timeout (raise
`QWEN_TIMEOUT`). A failed send rolls back its message, so the session stays
consistent and safe to retry.

**`qwen_send` times out, but the work isn't lost.** The MCP client can idle out
(~300s) while Ollama keeps generating; the finished reply still lands in the
session transcript. A timeout is not a Qwen failure. Call `qwen_get_history`
*before* re-asking — re-asking blind makes the model answer twice and desyncs
the session. Avoid it by keeping each turn's output to ≲150 lines, or by raising
the per-server MCP `timeout` (and `QWEN_TIMEOUT` for the Ollama call itself).

**First call is slow (30-60 s).** Ollama is loading 24 GB into memory. Later
calls are warm; raise `QWEN_KEEP_ALIVE` to extend that.

**Session runs out of context.** Qwen3.6 has 262K native. End the session, or
have Qwen summarize and seed the next session's `system_prompt` with it.

**Want a clean slate.** `rm data/sessions/*.json` while the server is idle.

Logs are at `data/server.log`. LaunchAgent control: `launchctl list | grep qwen`,
`launchctl unload|load ~/Library/LaunchAgents/local.qwen-local-delegate-mcp.plist`.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/local.qwen-local-delegate-mcp.plist
rm ~/Library/LaunchAgents/local.qwen-local-delegate-mcp.plist
claude mcp remove qwen-local-delegate --scope user
# Then remove the guidance block from your CLAUDE.md (manual).

brew uninstall ollama && rm -rf ~/.ollama   # optional: nuke Ollama and the model
```
