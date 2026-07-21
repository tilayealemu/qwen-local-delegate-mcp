#!/usr/bin/env bash
# Checks an existing qwen-local-delegate-mcp install end to end. Read-only — makes
# no changes. Run after install.sh, or any time something seems off.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${QWEN_MODEL:-qwen3.6:35b-a3b}"

PLIST_LABEL="local.qwen-local-delegate-mcp"
MCP_URL="http://localhost:11435/mcp"

pass() { printf "  \033[1;32m\xe2\x9c\x94\033[0m %s\n" "$*"; }
fail() { printf "  \033[1;31m\xe2\x9c\x98\033[0m %s\n" "$*"; FAILED=1; }
say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }

FAILED=0

say "Ollama daemon"
if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
    pass "reachable at http://localhost:11434"
else
    fail "not reachable — start it with 'brew services start ollama' or 'ollama serve'"
fi

say "Model"
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "${MODEL}"; then
    pass "${MODEL} is pulled"
else
    fail "${MODEL} not pulled — run 'ollama pull ${MODEL}'"
fi

say "LaunchAgent"
if launchctl list 2>/dev/null | grep -q "${PLIST_LABEL}"; then
    pass "${PLIST_LABEL} is loaded"
else
    fail "${PLIST_LABEL} not loaded — run ./install.sh"
fi

say "MCP server"
if curl -sf -o /dev/null -X POST "${MCP_URL}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'; then
    pass "responds on ${MCP_URL}"
else
    fail "not responding on ${MCP_URL} — check 'lsof -iTCP:11435 -sTCP:LISTEN' and ${REPO_DIR}/data/server.log"
fi

say "Claude Code registration"
if command -v claude >/dev/null 2>&1; then
    if claude mcp get qwen-local-delegate 2>&1 | grep -q "Connected"; then
        pass "qwen-local-delegate registered and connected"
    else
        fail "qwen-local-delegate not connected — run 'claude mcp get qwen-local-delegate' for detail"
    fi
else
    fail "'claude' CLI not found on PATH"
fi

say "CLAUDE.md guidance"
if grep -rq "Delegation to local Qwen" "${HOME}/.claude/CLAUDE.md" 2>/dev/null \
    || grep -rq "Delegation to local Qwen" "$(pwd)/CLAUDE.md" 2>/dev/null; then
    pass "delegation guidance block found"
else
    fail "no delegation guidance found in ~/.claude/CLAUDE.md or ./CLAUDE.md — copy the block from ${REPO_DIR}/CLAUDE.md (Claude won't reliably use the tools without it)"
fi

echo
if [[ "${FAILED}" -eq 0 ]]; then
    printf "\033[1;32m✔ All checks passed.\033[0m\n"
    exit 0
else
    printf "\033[1;31m✘ Some checks failed — see above.\033[0m\n"
    exit 1
fi
