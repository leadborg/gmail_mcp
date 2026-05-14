"""One-time OAuth setup: generates token.json for headless agent use.
Run this once interactively — a browser will open for Google sign-in.
After this, agents can use the MCP server without any browser prompt.
"""

from gmail_mcp import SCOPES, _resolve_config_path
from google_auth_oauthlib.flow import InstalledAppFlow


def main():
    creds_path = _resolve_config_path("GOOGLE_CREDS_PATH", "google_creds.json")
    token_path = _resolve_config_path("GOOGLE_TOKEN_PATH", "token.json")

    if not creds_path.exists():
        print(f"Error: Google OAuth credentials file not found at {creds_path}")
        print("Download your OAuth client JSON from Google Cloud Console")
        print("and save it to that path, or set the GOOGLE_CREDS_PATH env var.")
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)

    print(f"Token saved to {token_path}")
    print("Agents can now use the MCP server headlessly.")
    print(f"Scopes granted: {creds.scopes}")


if __name__ == "__main__":
    main()
