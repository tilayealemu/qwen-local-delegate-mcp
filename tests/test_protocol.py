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
import tempfile
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
            # entry is a fast, clear error even with Ollama unreachable.
            start = json.loads((await session.call_tool(
                "qwen_start_session", {"topic": "files validation test"}
            )).content[0].text)
            sid = start["session_id"]
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    binary = Path(tmp) / "blob.bin"
                    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00not text")
                    oversize = Path(tmp) / "huge.txt"
                    oversize.write_text("x" * 2_000_001)

                    cases = [
                        (["relative/path.txt"], "absolute paths"),
                        (["/nonexistent/path/for/sure.txt"], "File not found"),
                        ([str(binary)], "binary file"),
                        ([str(oversize)], "attachment budget"),
                    ]
                    for files, expected in cases:
                        resp = await session.call_tool("qwen_send", {
                            "session_id": sid,
                            "message": "irrelevant",
                            "files": files,
                        })
                        text = resp.content[0].text
                        if not resp.isError or expected not in text:
                            print(f"FAIL: qwen_send accepted files={files} or missed "
                                  f"{expected!r}: {text[:200]}")
                            return 1
                    print(f"qwen_send -> rejected {len(cases)} bad files[] cases cleanly")

                    # A rejected turn must leave the session untouched, so a
                    # corrected retry doesn't stack orphaned user messages.
                    hist = json.loads((await session.call_tool(
                        "qwen_get_history", {"session_id": sid}
                    )).content[0].text)
                    non_system = [m for m in hist["messages"] if m["role"] != "system"]
                    if non_system:
                        print(f"FAIL: rejected files[] turns left {len(non_system)} "
                              f"message(s) in the session")
                        return 1
                    print("qwen_send -> rejected turns left the session clean")
            finally:
                await session.call_tool("qwen_end_session", {"session_id": sid})

    print("OK: protocol test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
