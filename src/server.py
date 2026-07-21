# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.2.0",
#     "httpx>=0.27",
# ]
# ///
"""
Qwen Delegate MCP server.

Exposes stateful chat sessions with a local Ollama-hosted Qwen model as MCP tools.
Claude (or any MCP client) can open a session, send follow-up messages that share
context, list active sessions, and close them.

Transport: streamable HTTP on localhost. Set MCP_TRANSPORT=stdio to override.
State: in-memory + JSON files under data/sessions/.
Backend: Ollama /api/chat (streaming) at http://localhost:11434 (configurable via
OLLAMA_HOST). Long generations emit MCP progress notifications so the client stays
alive and the caller sees the reply growing instead of a silent multi-minute block.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("QWEN_MODEL", "qwen3.6:35b-a3b")
KEEP_ALIVE = os.environ.get("QWEN_KEEP_ALIVE", "30m")
REQUEST_TIMEOUT = float(os.environ.get("QWEN_TIMEOUT", "600"))
PROGRESS_INTERVAL = float(os.environ.get("QWEN_PROGRESS_INTERVAL", "10"))
MAX_ATTACH_BYTES = int(os.environ.get("QWEN_MAX_ATTACH_BYTES", "2000000"))

DATA_DIR = Path(os.environ.get(
    "QWEN_DATA_DIR",
    Path(__file__).resolve().parent.parent / "data" / "sessions",
))
DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Session:
    session_id: str
    model: str
    topic: str
    system_prompt: str
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def path(self) -> Path:
        return DATA_DIR / f"{self.session_id}.json"

    def save(self) -> None:
        self.path.write_text(json.dumps(asdict(self), indent=2))

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def message_count(self) -> int:
        return sum(1 for m in self.messages if m["role"] != "system")


SESSIONS: dict[str, Session] = {}


def _load_sessions() -> None:
    for f in DATA_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            s = Session(**data)
            SESSIONS[s.session_id] = s
        except Exception:
            pass


def _get(session_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if s is None:
        raise ValueError(
            f"Unknown session_id={session_id!r}. "
            f"Call qwen_start_session first, or qwen_list_sessions to see active ones."
        )
    return s


def _ollama_down_msg() -> str:
    return (
        f"Cannot reach Ollama at {OLLAMA_HOST}. Is it running? "
        f"Start it with `ollama serve`, or set OLLAMA_HOST to the right address."
    )


def _model_missing_msg(model: str) -> str:
    return (
        f"Ollama has no model named {model!r}. Pull it with `ollama pull {model}`, "
        f"or pass a model you already have (see `ollama list`)."
    )


async def _safe(awaitable: Any) -> None:
    """Await a notification, swallowing errors — a failed progress/log
    notification must never fail an otherwise-successful generation."""
    try:
        await awaitable
    except Exception:
        pass


def _read_files_block(paths: list[str]) -> str:
    """
    Read each path and render it as a labeled block to append to a message.

    Reading happens server-side so file bytes go straight from disk into
    Qwen's prompt without ever passing through the caller's own context.
    Raises ValueError (actionable, caller-facing) on a bad path — before any
    Ollama call, so a bad files[] entry never costs a wasted generation.

    Rejects binaries and anything over MAX_ATTACH_BYTES in total. Both would
    otherwise fail silently and expensively: a binary decodes to a screenful of
    replacement chars, and an oversized attachment blows the context window
    somewhere inside Ollama rather than here.
    """
    blocks = []
    total = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError(f"files[] entries must be absolute paths, got {raw!r}")
        if not path.is_file():
            raise ValueError(f"File not found: {raw}")

        size = path.stat().st_size
        total += size
        if total > MAX_ATTACH_BYTES:
            raise ValueError(
                f"files[] exceeds the {MAX_ATTACH_BYTES / 1e6:.1f} MB attachment "
                f"budget at {raw} ({size / 1e6:.1f} MB, {total / 1e6:.1f} MB total). "
                f"Attach a smaller slice, split it across turns, or raise "
                f"QWEN_MAX_ATTACH_BYTES."
            )

        data = path.read_bytes()
        if b"\x00" in data:
            raise ValueError(
                f"{raw} looks like a binary file (contains NUL bytes). files[] is "
                f"text only — attaching binary spends context on garbage."
            )
        blocks.append(f"--- {raw} ---\n{data.decode('utf-8', errors='replace')}")
    return "\n\n".join(blocks)


async def _stream_chat(model: str, messages: list[dict]):
    """
    Stream Ollama /api/chat, yielding events:
        ("token", str)  -- an incremental chunk of the assistant reply
        ("done", dict)  -- the terminal object carrying eval/prompt counts

    Raises ValueError/RuntimeError with actionable text on the failure modes
    that actually happen locally (Ollama down, model not pulled, timeout).
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            async with client.stream(
                "POST", f"{OLLAMA_HOST}/api/chat", json=payload
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace").strip()
                    if r.status_code == 404:
                        raise ValueError(_model_missing_msg(model))
                    raise RuntimeError(
                        f"Ollama returned HTTP {r.status_code}: {body[:500]}"
                    )
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    err = obj.get("error")
                    if err:
                        if "not found" in str(err).lower():
                            raise ValueError(_model_missing_msg(model))
                        raise RuntimeError(f"Ollama error: {err}")
                    chunk = obj.get("message", {}).get("content", "")
                    if chunk:
                        yield ("token", chunk)
                    if obj.get("done"):
                        yield ("done", obj)
    except httpx.ConnectError as e:
        raise RuntimeError(_ollama_down_msg()) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"Qwen call exceeded QWEN_TIMEOUT ({REQUEST_TIMEOUT:.0f}s). Raise it for "
            f"longer generations, e.g. QWEN_TIMEOUT=1200."
        ) from e


MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "11435"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")

mcp = FastMCP("qwen-local-delegate", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
def qwen_start_session(
    topic: str = "",
    system_prompt: str = "",
    model: str = "",
) -> dict[str, Any]:
    """
    Open a new stateful chat session with Qwen.

    Use this when you want to delegate a piece of work that will span multiple
    back-and-forth messages (context accumulates across qwen_send calls in the
    same session). For a single one-shot question, use qwen_send with a fresh
    session and close it afterward.

    Args:
        topic: Short human-readable label for what this session is for
            (e.g. "refactor auth module"). Purely for your bookkeeping — the
            model does not see it unless you put it in system_prompt.
        system_prompt: Optional system message prepended to the conversation.
            Use this to give Qwen a role, constraints, or style guidance.
        model: Ollama model tag. Defaults to qwen3.6:35b-a3b.

    Returns: {session_id, model, topic}
    """
    session_id = uuid.uuid4().hex[:12]
    chosen_model = model or DEFAULT_MODEL
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    s = Session(
        session_id=session_id,
        model=chosen_model,
        topic=topic,
        system_prompt=system_prompt,
        messages=messages,
    )
    SESSIONS[session_id] = s
    s.save()
    return {"session_id": session_id, "model": chosen_model, "topic": topic}


@mcp.tool()
async def qwen_send(
    session_id: str,
    message: str,
    ctx: Context,
    files: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send a message to an existing Qwen session and get the reply.

    The full session history is sent to Qwen every call, so context is retained
    across turns. Long sessions accumulate tokens — call qwen_end_session when
    the task is done to free the state (and let Ollama unload the model).

    The reply is streamed from Ollama: while a long generation runs, the server
    emits MCP progress notifications (roughly every QWEN_PROGRESS_INTERVAL
    seconds) so the client stays alive instead of blocking silently. If the call
    fails (Ollama down, model not pulled, timeout), the just-sent message is
    rolled back so the session stays consistent and the call is safe to retry.

    Args:
        session_id: The ID returned by qwen_start_session.
        message: The user message to send (Claude's instruction to Qwen).
        files: Absolute paths to read and attach to this message. Use this
            instead of pasting file contents into `message` — the server reads
            the bytes directly, so they never pass through your own context.
            Attach freely: this is prompt input, not generated output, so it
            does not count toward the per-turn output-size guidance for
            avoiding timeouts. Text files only, up to QWEN_MAX_ATTACH_BYTES
            (default 2 MB) across all entries; bad paths, binaries, and
            oversized attachments are rejected before any Ollama call.

    Returns: {reply, session_id, turn, eval_count, prompt_eval_count, total_duration_ms}
    """
    s = _get(session_id)
    content = message
    if files:
        content = f"{message}\n\n{_read_files_block(files)}" if message else _read_files_block(files)
    s.messages.append({"role": "user", "content": content})
    started = time.time()
    await _safe(ctx.info(f"Qwen ({s.model}) is generating a reply…"))

    parts: list[str] = []
    final: dict[str, Any] = {}
    last_report = started
    try:
        async for kind, data in _stream_chat(s.model, s.messages):
            if kind == "token":
                parts.append(data)
                now = time.time()
                if now - last_report >= PROGRESS_INTERVAL:
                    last_report = now
                    chars = sum(len(p) for p in parts)
                    await _safe(ctx.report_progress(
                        progress=chars,
                        message=f"Qwen is generating… {chars} chars, "
                                f"{now - started:.0f}s elapsed",
                    ))
            else:  # "done"
                final = data
    except Exception:
        # Keep the session resumable: drop the user turn we optimistically
        # appended so a retry reproduces a clean turn rather than stacking two
        # user messages in a row.
        if s.messages and s.messages[-1].get("role") == "user":
            s.messages.pop()
        raise

    reply = "".join(parts)
    latency_ms = int((time.time() - started) * 1000)
    s.messages.append({"role": "assistant", "content": reply})
    s.last_used_at = time.time()
    s.save()
    await _safe(ctx.report_progress(
        progress=len(reply), total=len(reply), message="done"
    ))

    return {
        "reply": reply,
        "session_id": session_id,
        "turn": s.message_count() // 2,
        "eval_count": final.get("eval_count"),
        "prompt_eval_count": final.get("prompt_eval_count"),
        "total_duration_ms": latency_ms,
    }


@mcp.tool()
def qwen_end_session(session_id: str) -> dict[str, Any]:
    """
    Close a session and delete its state.

    Call this when the delegated task is complete. Frees memory and removes the
    session file. Idempotent — closing an unknown session_id is a no-op.

    Returns: {closed: bool, session_id}
    """
    s = SESSIONS.pop(session_id, None)
    if s is None:
        return {"closed": False, "session_id": session_id}
    s.delete()
    return {"closed": True, "session_id": session_id}


@mcp.tool()
def qwen_list_sessions() -> dict[str, Any]:
    """
    List all currently open Qwen sessions.

    Returns {"sessions": [...]} with one entry per session (id, topic, model,
    message count, timestamps). Useful when you resume work and forgot which
    session_id was for what.
    """
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "topic": s.topic,
                "model": s.model,
                "message_count": s.message_count(),
                "created_at": s.created_at,
                "last_used_at": s.last_used_at,
            }
            for s in sorted(SESSIONS.values(), key=lambda x: x.last_used_at, reverse=True)
        ]
    }


@mcp.tool()
async def qwen_list_models() -> dict[str, Any]:
    """
    List Ollama models pulled locally, with the currently configured default.

    Call this before qwen_start_session to confirm a model is actually
    available instead of discovering it is missing only after qwen_send fails.

    Returns {"default_model", "models": [{name, size_gb, parameter_size,
    quantization, modified_at}, ...]}.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            r.raise_for_status()
    except httpx.ConnectError as e:
        raise RuntimeError(_ollama_down_msg()) from e
    except httpx.TimeoutException as e:
        # A connect/read timeout is not a ConnectError, but from the caller's
        # side it is the same problem with the same fix.
        raise RuntimeError(
            f"{_ollama_down_msg()} (Timed out after 10s — it may be up but wedged; "
            f"check `ollama ps`.)"
        ) from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Ollama returned HTTP {e.response.status_code} for /api/tags"
        ) from e

    models = []
    for m in r.json().get("models", []):
        details = m.get("details", {})
        models.append({
            "name": m.get("name") or m.get("model"),
            "size_gb": round(m.get("size", 0) / 1e9, 1),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "modified_at": m.get("modified_at"),
        })
    return {"default_model": DEFAULT_MODEL, "models": models}


@mcp.tool()
def qwen_get_history(session_id: str, tail: int = 0) -> dict[str, Any]:
    """
    Read a session's message history for observability/debugging.

    Args:
        session_id: The session to inspect.
        tail: If >0, only return the last N messages. 0 = all.

    Returns {"session_id", "messages": [...]}.
    """
    s = _get(session_id)
    msgs = s.messages if tail <= 0 else s.messages[-tail:]
    return {"session_id": session_id, "messages": msgs}


_load_sessions()

if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
