# grok-bridge

Local REST API bridge to Grok (grok.com) via Playwright + Chrome. Query Grok from scripts, CLI tools, or AI agents without manual browser interaction.

Linux port of [ythx-101/grok-bridge](https://github.com/ythx-101/grok-bridge). Replaces Safari + AppleScript with Playwright + Chrome.

## Features

- **REST API**: POST `/chat`, `/new`, `/eval`; GET `/health`, `/history`
- **Mode selection**: auto, fast, expert, heavy (Grok's thinking modes)
- **Structured sources**: Extracts source URLs with text snippets and X/Twitter timestamps
- **Persistent sessions**: Chrome profile saved at `~/.grok-bridge/chrome-data/`
- **Headed by default**: Visible browser window avoids Cloudflare/bot detection

## Requirements

- Linux with X11 display
- Google Chrome (system-installed)
- Python 3.10+
- Playwright (`pip install playwright && playwright install chromium`)

## Setup

```bash
# First time: login to grok.com
python3 grok_bridge.py --login
# Browser opens, log in with your X/Twitter account, then close the browser

# Start the bridge
python3 grok_bridge.py --port 19998
```

## Usage

```bash
# Chat
curl -s -X POST http://localhost:19998/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt":"hello","timeout":60}'

# Chat with mode selection
curl -s -X POST http://localhost:19998/chat \
  -d '{"prompt":"search for recent AI news","mode":"expert"}'

# New conversation
curl -X POST http://localhost:19998/new

# Health check
curl http://localhost:19998/health

# Page history
curl http://localhost:19998/history

# Evaluate/score a response
curl -s -X POST http://localhost:19998/eval \
  -d '{"prompt":"rate this response","context":"..."}'
```

## Response Format

```json
{
  "status": "ok",
  "response": "Grok's answer...",
  "elapsed": 15.2,
  "query": "your prompt",
  "mode": "auto",
  "sources": [
    {"url": "https://x.com/user/status/123", "text": "@user", "timestamp": "2026-04-10T14:30:00Z"},
    {"url": "https://example.com/article", "text": "Article Title"}
  ],
  "source_count": 12
}
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Send prompt, wait for response. Body: `{"prompt":"...","timeout":120,"mode":"auto"}` |
| POST | `/new` | Start new conversation |
| POST | `/eval` | Evaluate/score content |
| GET | `/health` | Health check (browser state, URL, login status) |
| GET | `/history` | Read current page conversation |

## Architecture

```
Client (curl, scripts, Claude Code)
  |
  | HTTP (localhost:19998)
  v
grok_bridge.py (Python, single-threaded HTTP server)
  |
  | Playwright CDP
  v
Chrome (persistent profile at ~/.grok-bridge/chrome-data/)
  |
  | HTTPS
  v
grok.com (authenticated via X/Twitter session)
```

## Files

```
grok_bridge.py      Single-file server (~500 lines)
docs/
  plans/            Implementation plans
  solutions/        Documented learnings
```

## Options

```
python3 grok_bridge.py --login          # Headed login flow
python3 grok_bridge.py --port 19998     # Start server (default port)
python3 grok_bridge.py --headless       # Run headless (may trigger Cloudflare)
```
