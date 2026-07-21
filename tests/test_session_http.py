# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2.0"]
# ///
"""
End-to-end test against the live LaunchAgent-hosted MCP server (HTTP transport).

This exercises the exact same server instance Claude Code will connect to.
Verifies (1) HTTP MCP handshake works, (2) all six tools are advertised,
(3) a multi-turn session actually retains context.

Requires:
  - LaunchAgent running (http://localhost:11435/mcp responds)
  - Ollama running with qwen3.6:35b-a3b pulled
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:11435/mcp"
SECRET = "aubergine-92-orbital"

EXPECTED_TOOLS = {
    "qwen_start_session",
    "qwen_send",
    "qwen_end_session",
    "qwen_list_sessions",
    "qwen_get_history",
    "qwen_list_models",
}


def _text(resp) -> dict:
    return json.loads(resp.content[0].text)


async def main() -> int:
    async with streamablehttp_client(URL) as (r, w, _get_sid):
        async with ClientSession(r, w) as session:
            await session.initialize()

            tools_resp = await session.list_tools()
            got = {t.name for t in tools_resp.tools}
            missing = EXPECTED_TOOLS - got
            if missing:
                print(f"FAIL: missing tools: {missing}")
                return 1
            print(f"tools: {sorted(got)}")

            start = _text(await session.call_tool(
                "qwen_start_session",
                {
                    "topic": "http memory test",
                    "system_prompt": (
                        "You are a concise assistant used in an automated test. "
                        "Answer in one short sentence."
                    ),
                },
            ))
            sid = start["session_id"]
            print(f"session: {sid} model={start['model']}")

            turn1 = _text(await session.call_tool(
                "qwen_send",
                {
                    "session_id": sid,
                    "message": (
                        f"Please remember this secret code for our conversation: "
                        f"{SECRET}. Acknowledge briefly."
                    ),
                },
            ))
            print(f"turn 1 reply: {turn1['reply'][:200]}")
            print(f"  timing: {turn1.get('total_duration_ms')}ms, "
                  f"eval={turn1.get('eval_count')} prompt={turn1.get('prompt_eval_count')}")

            turn2 = _text(await session.call_tool(
                "qwen_send",
                {
                    "session_id": sid,
                    "message": "What was the secret code I just told you? Only output the code.",
                },
            ))
            print(f"turn 2 reply: {turn2['reply'][:200]}")

            listed = _text(await session.call_tool("qwen_list_sessions", {}))
            print(f"active sessions: {len(listed['sessions'])} "
                  f"(includes ours: {any(s['session_id']==sid for s in listed['sessions'])})")

            closed = _text(await session.call_tool(
                "qwen_end_session", {"session_id": sid}
            ))
            print(f"closed: {closed}")

            passed = SECRET in turn2["reply"]

    if passed:
        print("OK: live HTTP server retained context across turns")
        return 0
    print(f"FAIL: reply did not contain secret {SECRET!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
