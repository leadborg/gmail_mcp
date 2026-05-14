import base64
import hashlib
import hmac
import json
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

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
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
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


def _decode_body_data(value: str) -> str:
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_text_body(payload: dict[str, Any]) -> str:
    mime_type = str(payload.get("mimeType") or "").lower()
    body_data = payload.get("body", {}).get("data")
    if mime_type == "text/plain" and isinstance(body_data, str):
        return _decode_body_data(body_data)

    parts = payload.get("parts") or []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                text = _extract_text_body(part)
                if text.strip():
                    return text

    if isinstance(body_data, str):
        return _decode_body_data(body_data)
    return ""


def _post_signed_json(webhook_url: str, signing_secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(signing_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    req = urllib_request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-LeadBorg-Signature": f"sha256={signature}",
        },
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=15) as resp:  # noqa: S310 - operator-provided webhook URL
        response_body = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            parsed = {"body": response_body}
        return {"status_code": resp.status, "response": parsed}


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
            f"Google API request failed during {operation}.",
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
def _get_credentials():
    token_path = _resolve_config_path("GOOGLE_TOKEN_PATH", "token.json")
    creds_path = _resolve_config_path("GOOGLE_CREDS_PATH", "google_creds.json")
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    needs_new_token = not creds or not creds.valid or not set(SCOPES).issubset(set(creds.scopes or []))
    if needs_new_token:
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

    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=_get_credentials())


def get_calendar_service():
    return build("calendar", "v3", credentials=_get_credentials())


# -------------------------
# SEND EMAIL
# -------------------------
@mcp.tool()
def send_email(to: str, subject: str, body: str, reply_to: str | None = None):
    """
    Send an email.
    """
    try:
        service = get_gmail_service()

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        if reply_to and reply_to.strip():
            message["Reply-To"] = reply_to.strip()

        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        send_message = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )

        return {
            "status": "sent",
            "message_id": send_message["id"],
            "provider_message_id": send_message["id"],
            "thread_id": send_message.get("threadId"),
            "provider_thread_id": send_message.get("threadId"),
        }
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
            "threadId": msg.get("threadId", ""),
            "from": sender,
            "subject": subject,
            "snippet": msg.get("snippet", ""),
            "body": _extract_text_body(msg.get("payload", {})),
        }
    except Exception as exc:
        return _handle_tool_error("read_email", exc)


@mcp.tool()
def forward_recent_replies_to_webhook(
    webhook_url: str,
    signing_secret: str,
    query: str = "to:leadborg+ newer_than:7d",
    max_results: int = 10,
):
    """
    Forward recent Gmail messages matching a query to a LeadBorg email webhook.
    """
    try:
        clean_url = webhook_url.strip()
        clean_secret = signing_secret.strip()
        clean_query = query.strip()
        if not clean_url:
            raise ValueError("webhook_url is required")
        if not clean_secret:
            raise ValueError("signing_secret is required")
        if not clean_query:
            raise ValueError("query is required")
        if max_results < 1:
            raise ValueError("max_results must be >= 1")

        service = get_gmail_service()
        results = (
            service.users()
            .messages()
            .list(userId="me", q=clean_query, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])
        forwarded: list[dict[str, Any]] = []

        for item in messages:
            message_id = item.get("id")
            if not message_id:
                continue
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            headers = msg.get("payload", {}).get("headers", [])
            payload = {
                "provider_message_id": message_id,
                "provider_thread_id": msg.get("threadId", item.get("threadId", "")),
                "from_email": _extract_header(headers, "From"),
                "to": _extract_header(headers, "To"),
                "subject": _extract_header(headers, "Subject"),
                "text": _extract_text_body(msg.get("payload", {})) or msg.get("snippet", ""),
            }
            post_result = _post_signed_json(clean_url, clean_secret, payload)
            forwarded.append(
                {
                    "message_id": message_id,
                    "thread_id": payload["provider_thread_id"],
                    "webhook_status_code": post_result["status_code"],
                    "webhook_response": post_result["response"],
                }
            )

        return {
            "status": "forwarded",
            "query": clean_query,
            "count": len(forwarded),
            "messages": forwarded,
        }
    except Exception as exc:
        return _handle_tool_error("forward_recent_replies_to_webhook", exc)


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


# -------------------------
# LIST CALENDARS
# -------------------------
@mcp.tool()
def list_calendars():
    """
    List all calendars the user has access to.
    """
    try:
        service = get_calendar_service()
        calendar_list = service.calendarList().list().execute()
        items = calendar_list.get("items", [])
        return [
            {
                "id": item["id"],
                "summary": item.get("summary", ""),
                "description": item.get("description", ""),
                "primary": item.get("primary", False),
            }
            for item in items
        ]
    except Exception as exc:
        return _handle_tool_error("list_calendars", exc)


# -------------------------
# LIST EVENTS
# -------------------------
@mcp.tool()
def list_events(
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
):
    """
    List events from a calendar.
    """
    try:
        if max_results < 1:
            raise ValueError("max_results must be >= 1")

        service = get_calendar_service()
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if query:
            params["q"] = query

        events_result = service.events().list(**params).execute()
        events = events_result.get("items", [])

        formatted = []
        for event in events:
            formatted.append({
                "id": event.get("id"),
                "summary": event.get("summary", ""),
                "description": event.get("description", ""),
                "location": event.get("location", ""),
                "start": event.get("start", {}),
                "end": event.get("end", {}),
                "status": event.get("status", ""),
                "creator": event.get("creator", {}),
                "attendees": event.get("attendees", []),
                "htmlLink": event.get("htmlLink", ""),
            })

        return {"count": len(formatted), "events": formatted}
    except Exception as exc:
        return _handle_tool_error("list_events", exc)


# -------------------------
# GET EVENT
# -------------------------
@mcp.tool()
def get_event(calendar_id: str, event_id: str):
    """
    Get a specific event by ID.
    """
    try:
        if not calendar_id.strip():
            raise ValueError("calendar_id is required")
        if not event_id.strip():
            raise ValueError("event_id is required")

        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        return {
            "id": event.get("id"),
            "summary": event.get("summary", ""),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": event.get("start", {}),
            "end": event.get("end", {}),
            "status": event.get("status", ""),
            "creator": event.get("creator", {}),
            "organizer": event.get("organizer", {}),
            "attendees": event.get("attendees", []),
            "htmlLink": event.get("htmlLink", ""),
            "recurrence": event.get("recurrence", []),
            "reminders": event.get("reminders", {}),
        }
    except Exception as exc:
        return _handle_tool_error("get_event", exc)


# -------------------------
# CREATE EVENT
# -------------------------
@mcp.tool()
def create_event(
    calendar_id: str,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
):
    """
    Create a new calendar event.
    """
    try:
        if not summary.strip():
            raise ValueError("summary is required")
        if not start_datetime.strip():
            raise ValueError("start_datetime is required")
        if not end_datetime.strip():
            raise ValueError("end_datetime is required")

        service = get_calendar_service()

        event_body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_datetime, "timeZone": timezone},
            "end": {"dateTime": end_datetime, "timeZone": timezone},
        }
        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location
        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees]

        created = service.events().insert(calendarId=calendar_id, body=event_body).execute()

        return {
            "status": "created",
            "event_id": created.get("id"),
            "htmlLink": created.get("htmlLink", ""),
            "summary": created.get("summary", ""),
            "start": created.get("start", {}),
            "end": created.get("end", {}),
        }
    except Exception as exc:
        return _handle_tool_error("create_event", exc)


# -------------------------
# UPDATE EVENT
# -------------------------
@mcp.tool()
def update_event(
    calendar_id: str,
    event_id: str,
    summary: str | None = None,
    description: str | None = None,
    location: str | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    timezone: str | None = None,
    attendees: list[str] | None = None,
):
    """
    Update an existing calendar event.
    """
    try:
        if not calendar_id.strip():
            raise ValueError("calendar_id is required")
        if not event_id.strip():
            raise ValueError("event_id is required")

        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        if summary is not None:
            event["summary"] = summary
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location
        if start_datetime is not None:
            event["start"] = {
                "dateTime": start_datetime,
                "timeZone": timezone or event["start"].get("timeZone", "UTC"),
            }
        if end_datetime is not None:
            event["end"] = {
                "dateTime": end_datetime,
                "timeZone": timezone or event["end"].get("timeZone", "UTC"),
            }
        if attendees is not None:
            event["attendees"] = [{"email": a} for a in attendees]

        updated = service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()

        return {
            "status": "updated",
            "event_id": updated.get("id"),
            "htmlLink": updated.get("htmlLink", ""),
            "summary": updated.get("summary", ""),
            "start": updated.get("start", {}),
            "end": updated.get("end", {}),
        }
    except Exception as exc:
        return _handle_tool_error("update_event", exc)


# -------------------------
# DELETE EVENT
# -------------------------
@mcp.tool()
def delete_event(calendar_id: str, event_id: str):
    """
    Delete a calendar event.
    """
    try:
        if not calendar_id.strip():
            raise ValueError("calendar_id is required")
        if not event_id.strip():
            raise ValueError("event_id is required")

        service = get_calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

        return {"status": "deleted", "event_id": event_id}
    except Exception as exc:
        return _handle_tool_error("delete_event", exc)


# -------------------------
# RESPOND TO EVENT
# -------------------------
@mcp.tool()
def respond_to_event(calendar_id: str, event_id: str, response_status: str):
    """
    Respond to an event invitation (accepted, tentative, declined).
    """
    try:
        if not calendar_id.strip():
            raise ValueError("calendar_id is required")
        if not event_id.strip():
            raise ValueError("event_id is required")

        valid_statuses = {"accepted", "tentative", "declined"}
        clean_status = response_status.strip().lower()
        if clean_status not in valid_statuses:
            raise ValueError(
                f"response_status must be one of: {', '.join(sorted(valid_statuses))}"
            )

        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        # Get the user's email from the primary calendar for matching
        user_email = None
        try:
            cal = service.calendarList().get(calendarId="primary").execute()
            user_email = cal.get("id")
        except Exception:
            pass

        attendees = event.get("attendees", [])

        # Find the attendee that represents the current user
        target = None
        for attendee in attendees:
            if attendee.get("self"):
                target = attendee
                break

        if target is None and user_email:
            for attendee in attendees:
                if attendee.get("email", "").lower() == user_email.lower():
                    target = attendee
                    break

        if target is not None:
            target["responseStatus"] = clean_status
        else:
            # No matching attendee found; add one for the current user
            attendee_entry = {"responseStatus": clean_status}
            if user_email:
                attendee_entry["email"] = user_email
            attendees.append(attendee_entry)

        event["attendees"] = attendees
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()

        return {
            "status": "responded",
            "event_id": event_id,
            "response_status": clean_status,
        }
    except Exception as exc:
        return _handle_tool_error("respond_to_event", exc)


# -------------------------
# SUGGEST TIME
# -------------------------
@mcp.tool()
def suggest_time(
    duration_minutes: int = 60,
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_ids: list[str] | None = None,
    working_hours_start: str = "09:00",
    working_hours_end: str = "17:00",
    timezone: str = "UTC",
):
    """
    Suggest available time slots based on freebusy queries.
    Uses working hours (default 9am-5pm) to find gaps.
    """
    try:
        from datetime import datetime, timedelta, timezone as dt_timezone

        if duration_minutes < 1:
            raise ValueError("duration_minutes must be >= 1")

        now = datetime.now(dt_timezone.utc)
        if time_min:
            window_start = datetime.fromisoformat(time_min)
        else:
            window_start = now

        if time_max:
            window_end = datetime.fromisoformat(time_max)
        else:
            window_end = window_start + timedelta(days=3)

        service = get_calendar_service()

        # Freebusy query
        freebusy_body: dict[str, Any] = {
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "timeZone": timezone,
        }
        if calendar_ids:
            freebusy_body["items"] = [{"id": cid} for cid in calendar_ids]
        else:
            freebusy_body["items"] = [{"id": "primary"}]

        fb_result = service.freebusy().query(body=freebusy_body).execute()
        calendars_data = fb_result.get("calendars", {})

        # Collect busy periods across all queried calendars
        busy_periods: list[tuple[datetime, datetime]] = []
        for cal_data in calendars_data.values():
            for busy in cal_data.get("busy", []):
                busy_start = datetime.fromisoformat(busy.get("start", ""))
                busy_end = datetime.fromisoformat(busy.get("end", ""))
                busy_periods.append((busy_start, busy_end))

        busy_periods.sort()

        # Merge overlapping busy periods
        merged_busy: list[tuple[datetime, datetime]] = []
        for start, end in busy_periods:
            if merged_busy and start <= merged_busy[-1][1]:
                merged_busy[-1] = (merged_busy[-1][0], max(merged_busy[-1][1], end))
            else:
                merged_busy.append((start, end))

        # Parse working hours
        wh_start_parts = working_hours_start.split(":")
        wh_end_parts = working_hours_end.split(":")
        wh_start_hour = int(wh_start_parts[0])
        wh_start_min = int(wh_start_parts[1]) if len(wh_start_parts) > 1 else 0
        wh_end_hour = int(wh_end_parts[0])
        wh_end_min = int(wh_end_parts[1]) if len(wh_end_parts) > 1 else 0

        duration = timedelta(minutes=duration_minutes)

        suggestions: list[dict[str, str]] = []
        current = window_start

        while current + duration <= window_end:
            day_start = current.replace(hour=wh_start_hour, minute=wh_start_min, second=0, microsecond=0)
            day_end = current.replace(hour=wh_end_hour, minute=wh_end_min, second=0, microsecond=0)

            if current < day_start:
                current = day_start
            if current > day_end:
                current = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                continue

            slot_end = current + duration
            if slot_end > day_end:
                current = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                continue

            # Check if slot overlaps with any busy period
            overlap = False
            for b_start, b_end in merged_busy:
                if current < b_end and slot_end > b_start:
                    overlap = True
                    # Jump to end of this busy period
                    current = b_end
                    break

            if overlap:
                continue

            suggestions.append({
                "start": current.isoformat(),
                "end": slot_end.isoformat(),
            })
            current = slot_end

            if len(suggestions) >= 5:
                break

        return {"suggestions": suggestions, "timezone": timezone}
    except Exception as exc:
        return _handle_tool_error("suggest_time", exc)


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
