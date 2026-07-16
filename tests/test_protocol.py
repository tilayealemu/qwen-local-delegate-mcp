# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2.0"]
# ///
"""
Protocol-level test: spawn the MCP server over stdio, initialize, list tools,
and call one that doesn't need the model. No Ollama required.
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "src" / "server.py"

EXPECTED_TOOLS = {
    "qwen_start_session",
    "qwen_send",
    "qwen_end_session",
    "qwen_list_sessions",
    "qwen_get_history",
}


async def main() -> int:
    params = StdioServerParameters(
        command="uv",
        args=["run", "--script", str(SERVER)],
        env={"MCP_TRANSPORT": "stdio", "PATH": __import__("os").environ["PATH"]},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            tools_resp = await session.list_tools()
            got = {t.name for t in tools_resp.tools}
            missing = EXPECTED_TOOLS - got
            extra = got - EXPECTED_TOOLS
            print(f"tools: {sorted(got)}")
            if missing:
                print(f"FAIL: missing tools: {missing}")
                return 1
            if extra:
                print(f"note: extra tools: {extra}")

            resp = await session.call_tool("qwen_list_sessions", {})
            print(f"qwen_list_sessions -> {resp.content[0].text[:200]}")

    print("OK: protocol test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
