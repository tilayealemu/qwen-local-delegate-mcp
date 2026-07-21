# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2.0"]
# ///
"""
Protocol-level test: spawn the MCP server over stdio, initialize, list tools,
and call one that doesn't need the model. No Ollama required.
"""

import asyncio
import json
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
    "qwen_list_models",
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

            # Ollama may or may not be running wherever this test executes
            # (CI has neither Ollama nor the model; a dev machine usually
            # does). Either way qwen_list_models must behave cleanly: a
            # structured list when Ollama answers, an actionable error when
            # it doesn't — never an uncaught crash.
            resp = await session.call_tool("qwen_list_models", {})
            text = resp.content[0].text
            if resp.isError:
                if "Cannot reach Ollama" not in text:
                    print(f"FAIL: qwen_list_models errored without the actionable message: {text[:200]}")
                    return 1
                print("qwen_list_models -> reported Ollama-down cleanly")
            else:
                data = json.loads(text)
                if "default_model" not in data or "models" not in data:
                    print(f"FAIL: qwen_list_models returned unexpected shape: {text[:200]}")
                    return 1
                print(f"qwen_list_models -> {len(data['models'])} model(s), default={data['default_model']}")

            # files[] validation must fail before any Ollama call, so a bad
            # path is a fast, clear error even with Ollama unreachable.
            start = json.loads((await session.call_tool(
                "qwen_start_session", {"topic": "files validation test"}
            )).content[0].text)
            resp = await session.call_tool("qwen_send", {
                "session_id": start["session_id"],
                "message": "irrelevant",
                "files": ["/nonexistent/path/for/sure.txt"],
            })
            text = resp.content[0].text
            if not resp.isError or "File not found" not in text:
                print(f"FAIL: qwen_send did not reject bad files[] path cleanly: {text[:200]}")
                return 1
            print("qwen_send -> rejected bad files[] path cleanly")
            await session.call_tool("qwen_end_session", {"session_id": start["session_id"]})

    print("OK: protocol test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
