#!/usr/bin/env bash
# Checks an existing qwen-local-delegate-mcp install end to end. Read-only — makes
# no changes. Run after install.sh, or any time something seems off.
#
# Deliberately no `-e` (every check must run, not just the ones before the first
# failure) and no `pipefail`. Every pipeline here ends in `grep -q`, which exits
# the moment it matches; that SIGPIPEs the producer, and under pipefail the
# pipeline then reports 141 even though the match succeeded — an intermittent
# false failure that depends on whether the producer's output fit the pipe
# buffer. When the producer fails for real it prints nothing and grep fails
# anyway, so pipefail buys nothing here.
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PLIST_LABEL="local.qwen-local-delegate-mcp"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
MCP_URL="http://localhost:11435/mcp"

# The installed LaunchAgent is the source of truth for which tag the daemon will
# actually ask Ollama for, since install.sh bakes QWEN_MODEL into it. Reading it
# back is what keeps a custom-model install from tripping a false "not pulled"
# failure below. An explicit QWEN_MODEL in the environment still wins.
MODEL="${QWEN_MODEL:-}"
if [[ -z "${MODEL}" ]]; then
    MODEL="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:QWEN_MODEL' \
        "${PLIST_DST}" 2>/dev/null)"
fi
MODEL="${MODEL:-qwen3.6:35b-a3b}"

pass() { printf "  \033[1;32m\xe2\x9c\x94\033[0m %s\n" "$*"; }
fail() { printf "  \033[1;31m\xe2\x9c\x98\033[0m %s\n" "$*"; FAILED=1; }
say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }

FAILED=0

say "Ollama daemon"
if curl -sf --max-time 5 http://localhost:11434/api/version >/dev/null 2>&1; then
    pass "reachable at http://localhost:11434"
else
    fail "not reachable — start it with 'brew services start ollama' or 'ollama serve'"
fi

say "Model"
# -F: model tags contain regex metacharacters ('.' in qwen3.6), so an
# unescaped pattern can match a model that isn't the one asked for.
if ollama list 2>/dev/null | awk '{print $1}' | grep -Fqx "${MODEL}"; then
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
MCP_HEADERS="$(mktemp)"
if curl -sf -o /dev/null -D "${MCP_HEADERS}" --max-time 10 -X POST "${MCP_URL}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'; then
    pass "responds on ${MCP_URL}"
else
    fail "not responding on ${MCP_URL} — check 'lsof -iTCP:11435 -sTCP:LISTEN' and ${REPO_DIR}/data/server.log"
fi
# Close the session that initialize just opened, so repeated verify runs don't
# accumulate dead sessions in the server's session manager.
MCP_SESSION_ID="$(grep -i '^mcp-session-id:' "${MCP_HEADERS}" 2>/dev/null | tr -d '\r' | awk '{print $2}')"
if [[ -n "${MCP_SESSION_ID}" ]]; then
    curl -sf -o /dev/null --max-time 5 -X DELETE "${MCP_URL}" \
        -H "mcp-session-id: ${MCP_SESSION_ID}" >/dev/null 2>&1 || true
fi
rm -f "${MCP_HEADERS}"

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
# This repo's own CLAUDE.md is the copy-paste *source*: it always contains the
# marker, so matching it proves nothing about whether the block was installed
# anywhere Claude will actually load it. Check user scope, plus the current
# project's CLAUDE.md only when that isn't this repo's own file.
GUIDANCE_CANDIDATES=("${HOME}/.claude/CLAUDE.md")
if [[ "$(pwd -P)" != "$(cd "${REPO_DIR}" && pwd -P)" ]]; then
    GUIDANCE_CANDIDATES+=("$(pwd -P)/CLAUDE.md")
fi

GUIDANCE_FOUND=""
for candidate in "${GUIDANCE_CANDIDATES[@]}"; do
    if grep -q "Delegation to local Qwen" "${candidate}" 2>/dev/null; then
        GUIDANCE_FOUND="${candidate}"
        break
    fi
done

if [[ -n "${GUIDANCE_FOUND}" ]]; then
    pass "delegation guidance block found in ${GUIDANCE_FOUND/#${HOME}/~}"
else
    fail "no delegation guidance in ${GUIDANCE_CANDIDATES[*]/#${HOME}/~} — copy the block from ${REPO_DIR}/CLAUDE.md into your own CLAUDE.md (Claude won't reliably use the tools without it)"
fi

echo
if [[ "${FAILED}" -eq 0 ]]; then
    printf "\033[1;32m✔ All checks passed.\033[0m\n"
    exit 0
else
    printf "\033[1;31m✘ Some checks failed — see above.\033[0m\n"
    exit 1
fi
