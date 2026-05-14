# Gmail + Calendar MCP Server Guide

This MCP server provides both **Gmail** and **Google Calendar** tools for AI agents.

---

## Prerequisites

- Python 3.13+
- `uv` installed
- Google OAuth credentials file at `~/.config/gmail-mcp/google_creds.json` or `./google_creds.json`
- Calendar API enabled in your GCP project:
  https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=YOUR_PROJECT_NUMBER

## Quick Start

```bash
uv sync
```

This creates/refreshes `.venv` and installs all dependencies.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_TOKEN_PATH` | `~/.config/gmail-mcp/token.json` | OAuth token path |
| `GOOGLE_CREDS_PATH` | `~/.config/gmail-mcp/google_creds.json` | OAuth client secrets path |
| `MCP_TRANSPORT` | `stdio` | One of: `stdio`, `sse`, `http` |
| `MCP_HOST` | `127.0.0.1` | HTTP bind host |
| `MCP_PORT` | `8001` | HTTP bind port |

---

## Configuring MCP Clients

### Option 1: Claude Desktop (stdio)

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gmail-calendar": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/gmail-mcp",
        "run",
        "gmail_mcp"
      ]
    }
  }
}
```

### Option 2: Claude Desktop / Any MCP Client (HTTP)

Start the server in HTTP mode:

```bash
MCP_TRANSPORT=http uv run python main.py
```

Then configure:

```json
{
  "mcpServers": {
    "gmail-calendar": {
      "transport": "http",
      "url": "http://127.0.0.1:8001/mcp"
    }
  }
}
```

### Option 3: Gemini CLI

Add to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "gmail-calendar": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/gmail-mcp",
        "run",
        "gmail_mcp"
      ]
    }
  }
}
```

### Option 4: Claude.ai Custom Connector

1. Run the server on HTTP:
   ```bash
   MCP_TRANSPORT=http uv run python main.py
   ```
2. If local, use a tunnel (e.g. `ngrok http 8001`)
3. Configure in Claude.ai Settings > Connectors > Add custom connector:
   - **Server name**: `Gmail Calendar`
   - **Remote MCP server URL**: `https://your-tunnel-url/mcp`
   - **Transport**: HTTP (no auth needed for local tunnel)

---

## Available Tools

### Gmail Tools

| Tool | Description |
|---|---|
| `send_email(to, subject, body, reply_to?)` | Send an email |
| `list_emails(query, max_results)` | List emails by Gmail search query |
| `read_email(message_id)` | Read a specific email's full content |
| `reply_email(message_id, body)` | Reply to an existing email |
| `read_emails_from_sender(sender, max_results)` | Read recent emails from a sender |
| `forward_recent_replies_to_webhook(webhook_url, signing_secret, query, max_results)` | Forward emails to a webhook |

### Calendar Tools

| Tool | Description |
|---|---|
| `list_calendars()` | List all accessible calendars |
| `list_events(calendar_id, max_results, time_min, time_max, query)` | List events with optional filters |
| `get_event(calendar_id, event_id)` | Get details of a specific event |
| `create_event(calendar_id, summary, start_datetime, end_datetime, timezone, description?, location?, attendees?)` | Create a new event |
| `update_event(calendar_id, event_id, summary?, description?, location?, start_datetime?, end_datetime?, timezone?, attendees?)` | Update an existing event |
| `delete_event(calendar_id, event_id)` | Delete an event |
| `respond_to_event(calendar_id, event_id, response_status)` | Accept/tentative/decline an invitation |
| `suggest_time(duration_minutes, time_min?, time_max?, calendar_ids?, working_hours_start?, working_hours_end?, timezone?)` | Find available time slots via freebusy |

---

## First-Time Authentication

**You must run auth once interactively** so agents don't get a browser popup.

### Step 1: One-time auth setup

```bash
uv run python auth.py
```

This opens a browser for Google OAuth, then saves `token.json` with a refresh token. Subsequent runs are fully headless — the refresh token is used silently.

> If you add new scopes later, delete `token.json` and re-run `auth.py`.

### Step 2: Verify token works (headless)

```bash
uv run python -c "
from gmail_mcp import list_calendars
print(list_calendars())
"
```

No browser should open. If it does, the refresh token may have expired — re-run `auth.py`.

## Verifying It Works

```bash
GOOGLE_TOKEN_PATH=/path/to/gmail-mcp/token.json GOOGLE_CREDS_PATH=/path/to/gmail-mcp/google_creds.json uv run python -c "
from gmail_mcp import list_calendars, list_emails
print('Calendars:', list_calendars())
print('Emails:', list_emails(max_results=2))
"
```
