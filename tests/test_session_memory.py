# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2.0"]
# ///
"""
End-to-end test: verify the session actually retains context across turns.

We give Qwen a secret in turn 1, then in turn 2 ask what the secret was
(without mentioning it). If the reply contains the secret, the session
memory is working.

Requires: Ollama running with qwen3.6:35b-a3b pulled.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "src" / "server.py"
SECRET = "aubergine-92-orbital"


def _text(resp) -> dict:
    return json.loads(resp.content[0].text)


async def main() -> int:
    params = StdioServerParameters(
        command="uv",
        args=["run", "--script", str(SERVER)],
        env={"MCP_TRANSPORT": "stdio", "PATH": __import__("os").environ["PATH"]},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            start = _text(await session.call_tool(
                "qwen_start_session",
                {
                    "topic": "memory test",
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
                  f"eval_count={turn1.get('eval_count')}")

            turn2 = _text(await session.call_tool(
                "qwen_send",
                {
                    "session_id": sid,
                    "message": "What was the secret code I just told you? Only output the code.",
                },
            ))
            print(f"turn 2 reply: {turn2['reply'][:200]}")

            passed = SECRET in turn2["reply"]

            await session.call_tool("qwen_end_session", {"session_id": sid})

    if passed:
        print("OK: session context retained across turns")
        return 0
    print(f"FAIL: reply did not contain the secret {SECRET!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
