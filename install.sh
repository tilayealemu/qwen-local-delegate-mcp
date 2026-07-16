#!/usr/bin/env bash
# One-shot installer for qwen-delegate-mcp. Idempotent — safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${QWEN_MODEL:-qwen3.6:35b-a3b}"

PLIST_LABEL="local.qwen-delegate-mcp"
PLIST_TEMPLATE="${REPO_DIR}/qwen-delegate-mcp.plist.template"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
MCP_URL="http://localhost:11435/mcp"

say() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
die() { printf "\033[1;31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

say "Checking prerequisites"
[[ "$(uname -s)" == "Darwin" ]] || die "This installer is macOS-only (uses launchd)."
command -v brew    >/dev/null || die "Homebrew required — install from https://brew.sh"
command -v uv      >/dev/null || die "uv required — 'brew install uv'"
command -v claude  >/dev/null || die "claude (Claude Code) required"

UV_PATH="$(command -v uv)"

if ! command -v ollama >/dev/null; then
    say "Installing Ollama"
    brew install ollama
fi

if ! curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
    say "Starting Ollama service"
    brew services start ollama
    for _ in {1..15}; do
        curl -sf http://localhost:11434/api/version >/dev/null 2>&1 && break
        sleep 1
    done
fi

if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "${MODEL}"; then
    say "Pulling ${MODEL} (~24 GB, one-time)"
    ollama pull "${MODEL}"
else
    say "${MODEL} already pulled"
fi

say "Rendering LaunchAgent from template → ${PLIST_DST}"
mkdir -p "$(dirname "${PLIST_DST}")" "${REPO_DIR}/data/sessions"
sed \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__UV_PATH__|${UV_PATH}|g" \
    -e "s|__PATH__|${PATH}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_DST}"

launchctl unload "${PLIST_DST}" 2>/dev/null || true
launchctl load "${PLIST_DST}"

say "Waiting for MCP server on port 11435"
for _ in {1..20}; do
    if curl -sf -o /dev/null -X POST "${MCP_URL}" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"install","version":"0"}}}'; then
        break
    fi
    sleep 1
done

say "Registering with Claude Code"
# Drop any existing entry first so re-runs don't trip over "already exists".
claude mcp remove qwen-delegate --scope user >/dev/null 2>&1 || true
claude mcp add --transport http --scope user qwen-delegate "${MCP_URL}" \
    || die "'claude mcp add' failed — qwen-delegate was not registered. Resolve the error above and re-run."

say "Verifying registration"
claude mcp get qwen-delegate 2>&1 | grep -E "Status|URL" || true

say "Running protocol test"
uv run --script "${REPO_DIR}/tests/test_protocol.py"

cat <<EOF

$(printf "\033[1;32m✔ Install complete.\033[0m")

Next steps:
  1. Add the delegation guidance to your CLAUDE.md — see CLAUDE.md in this repo
     for the exact wording. Without it Claude won't reliably use the tools.
  2. Restart Claude Code so it picks up the new MCP.
  3. In a new session, run /mcp — you should see qwen-delegate with 5 tools.

Logs: ${REPO_DIR}/data/server.log
Unload: launchctl unload ${PLIST_DST}
EOF
