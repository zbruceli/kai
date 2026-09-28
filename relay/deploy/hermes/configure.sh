#!/usr/bin/env bash
# Apply Kai's Hermes configuration: model, lock-down, approvals and the Kai MCP server. Safe to re-run.
# Run on the Hermes host after `docker compose up -d`; secrets live in ~/kai-hermes/.env (see README.md).
set -euo pipefail
H="docker exec -u $(id -u):$(id -g) kai-hermes hermes"
set_() { $H config set "$1" "$2" | tail -1; }

# Model: Gemini through the native provider (GEMINI_API_KEY in ~/kai-hermes/.env).
set_ model.provider gemini
set_ model.default gemini-3.8-flash
$H config unset model.base_url >/dev/null 2>&1 || true

# Lock-down: no shell, files, code, browser or computer control. Kai's jobs need none of them, and
# they're the biggest attack surface.
set_ agent.disabled_toolsets "[terminal, file, code_execution, browser, computer_use, image_gen, vision, tts, clarify, video, video_gen, homeassistant, spotify, kanban]"
set_ agent.max_turns 30

# Nothing acts unattended; self-written skills need approval.
set_ approvals.mode manual
set_ approvals.cron_mode deny
set_ approvals.unattended_mode deny
set_ approvals.single_query_mode deny
set_ skills.write_approval true
set_ updates.check false

# The Kai MCP server inside the relay (token from ~/kai-hermes/.env, never written into config.yaml).
set_ mcp_servers.kai.url "http://127.0.0.1:8766/mcp"
set_ mcp_servers.kai.headers.Authorization 'Bearer ${KAI_MCP_TOKEN}'
set_ mcp_servers.kai.tools.include "[kai_notify, kai_profile_brief, get_tides, get_weather, get_sun_times, list_notes]"
set_ mcp_servers.kai.prompts false
set_ mcp_servers.kai.resources false

cp "$(dirname "$0")/SOUL.md" "${KAI_HERMES_HOME:-$HOME/kai-hermes}/SOUL.md"
docker restart kai-hermes >/dev/null
echo "Configured. Checks:"
for _ in $(seq 40); do curl -fs http://127.0.0.1:8642/health >/dev/null && break; sleep 2; done
curl -fs http://127.0.0.1:8642/health && echo
$H mcp test kai 2>&1 | grep -E "Connected|Tools discovered|✗" || true
