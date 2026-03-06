# gmail-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes Gmail actions as tools, allowing AI assistants like Claude to send, read, list, and reply to emails on your behalf.

## Tools

| Tool | Description |
|------|-------------|
| `send_email` | Send an email to a recipient |
| `list_emails` | List emails matching a Gmail search query |
| `read_email` | Read a specific email by message ID |
| `read_emails_from_sender` | Fetch recent emails from a specific sender |
| `reply_email` | Reply to an email, preserving thread headers |

## Requirements

- Python 3.13+
- A Google Cloud project with the Gmail API enabled
- OAuth 2.0 credentials (`google_creds.json`)

## Setup

### 1. Get Google OAuth credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or select an existing one).
3. Enable the **Gmail API**.
4. Under **APIs & Services > Credentials**, create an **OAuth 2.0 Client ID** (Application type: Desktop app).
5. Download the JSON file and save it as `google_creds.json` in the project root.

### 2. Install dependencies

```bash
# Using uv (recommended)
uv sync

# Or pip
pip install -e .
```

### 3. Authenticate

Run the server once manually to trigger the OAuth browser flow:

```bash
python gmail_mcp.py
```

This creates a `token.json` file at `~/.config/gmail-mcp/token.json`. Subsequent runs reuse and auto-refresh this token.

## Running the server

The transport is controlled by the `MCP_TRANSPORT` environment variable (default: `stdio`).

### stdio (default — for Claude Desktop / MCP clients)

```bash
python gmail_mcp.py
```

### Streamable HTTP

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python gmail_mcp.py
# or alias:
MCP_TRANSPORT=http python gmail_mcp.py
```

### SSE

```bash
MCP_TRANSPORT=sse python gmail_mcp.py
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | Transport mode: `stdio`, `sse`, `streamable-http` (alias: `http`) |
| `MCP_HOST` | `127.0.0.1` | Host for HTTP/SSE transports |
| `MCP_PORT` | `8001` | Port for HTTP/SSE transports |
| `GOOGLE_CREDS_PATH` | `~/.config/gmail-mcp/google_creds.json` | Path to OAuth client credentials file |
| `GOOGLE_TOKEN_PATH` | `~/.config/gmail-mcp/token.json` | Path to cached OAuth token file |

## Claude Desktop configuration

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "python",
      "args": ["/absolute/path/to/gmail_mcp.py"]
    }
  }
}
```

## Running tests

```bash
python -m pytest tests/
# or
python -m unittest discover tests/
```

## Project structure

```
gmail-mcp/
├── gmail_mcp.py        # MCP server and tool definitions
├── main.py             # Alternate entry point (HTTP transport)
├── pyproject.toml
└── tests/
    └── test_gmail_mcp.py

~/.config/gmail-mcp/
├── google_creds.json   # OAuth client credentials (not committed)
└── token.json          # Cached OAuth token (generated at runtime)
```

## Permissions

The server requests two OAuth scopes:
- `https://www.googleapis.com/auth/gmail.readonly` — read access to emails
- `https://www.googleapis.com/auth/gmail.send` — permission to send email

This does not grant access to delete messages permanently.
