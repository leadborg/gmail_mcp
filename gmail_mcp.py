import base64
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---- CONFIG ----
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

mcp = FastMCP("gmail-mcp")
BASE_DIR = Path(__file__).resolve().parent
TRANSPORT_ALIASES = {"http": "streamable-http"}
SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


CONFIG_DIR = Path("~/.config/gmail-mcp").expanduser()


def _resolve_config_path(env_var: str, default_filename: str) -> Path:
    override = os.getenv(env_var)
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR / default_filename


def _resolve_transport() -> str:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    resolved_transport = TRANSPORT_ALIASES.get(transport, transport)
    if resolved_transport not in SUPPORTED_TRANSPORTS:
        raise ValueError(
            "Unsupported MCP transport "
            f"'{transport}'. Use one of: stdio, sse, streamable-http."
        )
    return resolved_transport


def _extract_header(headers: list[dict[str, Any]], header_name: str) -> str:
    expected = header_name.lower()
    for header in headers:
        if str(header.get("name", "")).lower() == expected:
            return str(header.get("value", "")).strip()
    return ""


def _normalize_reply_subject(subject: str) -> str:
    clean_subject = subject.strip()
    if not clean_subject:
        return "Re:"
    if clean_subject.lower().startswith("re:"):
        return clean_subject
    return f"Re: {clean_subject}"


def _error_response(
    operation: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "error": {
            "operation": operation,
            "code": code,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return payload


def _handle_tool_error(operation: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, FileNotFoundError):
        return _error_response(operation, "credentials_missing", str(exc))

    if isinstance(exc, ValueError):
        return _error_response(operation, "invalid_request", str(exc))

    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        code = "gmail_api_error"
        if status in (401, 403):
            code = "auth_error"
        elif status == 404:
            code = "message_not_found"

        details: dict[str, Any] = {}
        if status is not None:
            details["status"] = status
        reason = getattr(exc.resp, "reason", None)
        if reason:
            details["reason"] = str(reason)

        return _error_response(
            operation,
            code,
            f"Gmail API request failed during {operation}.",
            details or None,
        )

    return _error_response(
        operation,
        "internal_error",
        f"Unexpected error during {operation}: {exc}",
    )


# -------------------------
# AUTHENTICATION
# -------------------------
def get_gmail_service():
    token_path = _resolve_config_path("GOOGLE_TOKEN_PATH", "token.json")
    creds_path = _resolve_config_path("GOOGLE_CREDS_PATH", "google_creds.json")
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials file not found at {creds_path}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), SCOPES
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)

    return build("gmail", "v1", credentials=creds)


# -------------------------
# SEND EMAIL
# -------------------------
@mcp.tool()
def send_email(to: str, subject: str, body: str):
    """
    Send an email.
    """
    try:
        service = get_gmail_service()

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        send_message = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )

        return {"status": "sent", "message_id": send_message["id"]}
    except Exception as exc:
        return _handle_tool_error("send_email", exc)


# -------------------------
# LIST EMAILS
# -------------------------
@mcp.tool()
def list_emails(query: str = "is:unread", max_results: int = 5):
    """
    List emails based on Gmail search query.
    """
    try:
        if max_results < 1:
            raise ValueError("max_results must be >= 1")

        service = get_gmail_service()

        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )

        messages = results.get("messages", [])

        return {"count": len(messages), "messages": messages}
    except Exception as exc:
        return _handle_tool_error("list_emails", exc)


# -------------------------
# READ EMAIL
# -------------------------
@mcp.tool()
def read_email(message_id: str):
    """
    Read a specific email by ID.
    """
    try:
        if not message_id.strip():
            raise ValueError("message_id is required")

        service = get_gmail_service()

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        headers = msg.get("payload", {}).get("headers", [])

        subject = _extract_header(headers, "Subject")
        sender = _extract_header(headers, "From")

        return {
            "id": message_id,
            "from": sender,
            "subject": subject,
            "snippet": msg.get("snippet", "")
        }
    except Exception as exc:
        return _handle_tool_error("read_email", exc)


# -------------------------
# READ EMAILS FROM SENDER
# -------------------------
@mcp.tool()
def read_emails_from_sender(sender: str, max_results: int = 5):
    """
    Read recent emails from a specific sender.
    """
    try:
        clean_sender = sender.strip()
        if not clean_sender:
            raise ValueError("sender is required")
        if max_results < 1:
            raise ValueError("max_results must be >= 1")

        service = get_gmail_service()
        query = f"from:{clean_sender}"
        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])

        detailed_messages = []
        for item in messages:
            message_id = item.get("id")
            if not message_id:
                continue

            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = msg.get("payload", {}).get("headers", [])
            detailed_messages.append(
                {
                    "id": message_id,
                    "threadId": msg.get("threadId", item.get("threadId", "")),
                    "from": _extract_header(headers, "From"),
                    "subject": _extract_header(headers, "Subject"),
                    "date": _extract_header(headers, "Date"),
                    "snippet": msg.get("snippet", ""),
                }
            )

        return {
            "sender": clean_sender,
            "query": query,
            "count": len(detailed_messages),
            "messages": detailed_messages,
        }
    except Exception as exc:
        return _handle_tool_error("read_emails_from_sender", exc)


# -------------------------
# REPLY EMAIL
# -------------------------
@mcp.tool()
def reply_email(message_id: str, body: str):
    """
    Reply to an email.
    """
    try:
        if not message_id.strip():
            raise ValueError("message_id is required")

        service = get_gmail_service()

        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Message-ID", "References"],
            )
            .execute()
        )

        headers = original.get("payload", {}).get("headers", [])
        subject = _extract_header(headers, "Subject")
        sender = _extract_header(headers, "From")
        original_message_id = _extract_header(headers, "Message-ID")
        references = _extract_header(headers, "References").strip()
        thread_id = original.get("threadId")

        message = MIMEText(body)
        message["to"] = sender
        message["subject"] = _normalize_reply_subject(subject)

        if original_message_id:
            message["In-Reply-To"] = original_message_id
            if references:
                if original_message_id not in references.split():
                    references = f"{references} {original_message_id}"
                message["References"] = references
            else:
                message["References"] = original_message_id

        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        send_body: dict[str, Any] = {"raw": raw_message}
        if thread_id:
            send_body["threadId"] = thread_id

        send_message = (
            service.users()
            .messages()
            .send(userId="me", body=send_body)
            .execute()
        )

        return {"status": "replied", "message_id": send_message["id"]}
    except Exception as exc:
        return _handle_tool_error("reply_email", exc)


if __name__ == "__main__":
    selected_transport = _resolve_transport()
    if selected_transport == "streamable-http":
        mcp.run(
            transport=selected_transport,
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8001")),
        )
    else:
        mcp.run(transport=selected_transport)
