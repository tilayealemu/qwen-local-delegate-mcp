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
Backend: Ollama /api/chat at http://localhost:11434 (configurable via OLLAMA_HOST).
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
from mcp.server.fastmcp import FastMCP


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("QWEN_MODEL", "qwen3.6:35b-a3b")
KEEP_ALIVE = os.environ.get("QWEN_KEEP_ALIVE", "30m")
REQUEST_TIMEOUT = float(os.environ.get("QWEN_TIMEOUT", "600"))

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


def _call_ollama(model: str, messages: list[dict]) -> dict[str, Any]:
    """POST to /api/chat. Returns parsed JSON. Raises on HTTP or network error."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        r = client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()


MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "11435"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")

mcp = FastMCP("qwen-delegate", host=MCP_HOST, port=MCP_PORT)


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
def qwen_send(session_id: str, message: str) -> dict[str, Any]:
    """
    Send a message to an existing Qwen session and get the reply.

    The full session history is sent to Qwen every call, so context is retained
    across turns. Long sessions accumulate tokens — call qwen_end_session when
    the task is done to free the state (and let Ollama unload the model).

    Args:
        session_id: The ID returned by qwen_start_session.
        message: The user message to send (Claude's instruction to Qwen).

    Returns: {reply, session_id, turn, eval_count, prompt_eval_count, total_duration_ms}
    """
    s = _get(session_id)
    s.messages.append({"role": "user", "content": message})
    started = time.time()
    resp = _call_ollama(s.model, s.messages)
    latency_ms = int((time.time() - started) * 1000)

    msg = resp.get("message", {})
    reply = msg.get("content", "")
    s.messages.append({"role": "assistant", "content": reply})
    s.last_used_at = time.time()
    s.save()

    return {
        "reply": reply,
        "session_id": session_id,
        "turn": s.message_count() // 2,
        "eval_count": resp.get("eval_count"),
        "prompt_eval_count": resp.get("prompt_eval_count"),
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
