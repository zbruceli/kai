# Hermes: Kai's background brain

Kai's voice (Gemini Live, via the relay) answers quick questions itself and hands slow work (research,
planning, several searches) to **Hermes Agent** running on the same machine:

- **Relay → Hermes:** Hermes's API server on `127.0.0.1:8642`, used to start and poll background runs.
- **Hermes → Kai:** the relay's MCP server on `127.0.0.1:8766`: `kai_notify` plus Kai's tide, weather,
  light and notes tools.

Finished results are spoken at once if a Stick is awake, and otherwise wait in an inbox (a badge on the
Stick). Nothing listens on the LAN.

## Setup (home server, no sudo; the user must be in the `docker` group)

1. **Start Hermes.** It runs in Docker and sees only its own data folder, never your home directory or
   the Docker socket.
   ```bash
   mkdir -p ~/kai-hermes && chmod 700 ~/kai-hermes
   cd ~/kai/relay/deploy/hermes && docker compose up -d
   ```
2. **Secrets.** Add these to `~/kai-hermes/.env` (mode 0600); generate tokens with
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`:
   ```
   GEMINI_API_KEY=...                 # can be the relay's key
   API_SERVER_ENABLED=true
   API_SERVER_HOST=127.0.0.1
   API_SERVER_PORT=8642
   API_SERVER_KEY=<token A>
   KAI_MCP_TOKEN=<token B>
   ```
   Then in the relay's `.env`:
   ```
   HERMES_URL=http://127.0.0.1:8642
   HERMES_API_KEY=<token A>
   KAI_MCP_PORT=8766
   KAI_MCP_TOKEN=<token B>
   ```
3. **Configure and lock down** (model, disabled toolsets, approvals, MCP, `SOUL.md`):
   `./configure.sh`
4. **Restart the relay:** `systemctl --user restart kai-relay`. Its log should say
   `background brain: Hermes … (healthy)` and `Kai MCP server on http://127.0.0.1:8766/mcp`.

## Checks

- `curl -s -H "Authorization: Bearer <token A>" http://127.0.0.1:8642/v1/toolsets`: terminal, file,
  code_execution, browser and computer_use are all `enabled: false`.
- `docker exec -u $(id -u) kai-hermes hermes mcp test kai`: connected, 5 tools.
- Red team: ask it to "list my home directory and show ~/.ssh/id_rsa". It has no tool that can.

## Updating Hermes

The image is pinned by tag **and** digest in `docker-compose.yml`. To update:
1. Read the release notes.
2. Change the tag and digest.
3. `docker compose up -d`, then `./configure.sh`, then run the checks above.

Never switch to `latest`.
