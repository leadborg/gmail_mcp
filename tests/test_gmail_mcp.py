import base64
import os
import tempfile
import unittest
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

import gmail_mcp


class GmailMcpTests(unittest.TestCase):
    def _decode_mime(self, raw_message: str):
        return message_from_bytes(base64.urlsafe_b64decode(raw_message.encode()))

    def test_reply_email_sets_threading_headers_and_thread_id(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.get.return_value.execute.return_value = {
            "threadId": "thread-123",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Project update"},
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Message-ID", "value": "<abc123@example.com>"},
                    {"name": "References", "value": "<root@example.com>"},
                ]
            },
        }
        messages_api.send.return_value.execute.return_value = {"id": "new-id"}

        with patch.object(gmail_mcp, "get_gmail_service", return_value=service):
            result = gmail_mcp.reply_email("gmail-message-id", "Thanks for the update")

        self.assertEqual(result["status"], "replied")
        send_kwargs = messages_api.send.call_args.kwargs
        self.assertEqual(send_kwargs["body"]["threadId"], "thread-123")

        mime_msg = self._decode_mime(send_kwargs["body"]["raw"])
        self.assertEqual(mime_msg["In-Reply-To"], "<abc123@example.com>")
        self.assertEqual(
            mime_msg["References"], "<root@example.com> <abc123@example.com>"
        )
        self.assertEqual(mime_msg["Subject"], "Re: Project update")

    def test_reply_email_does_not_duplicate_re_prefix(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.get.return_value.execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Re: Existing thread"},
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Message-ID", "value": "<msg@example.com>"},
                ]
            }
        }
        messages_api.send.return_value.execute.return_value = {"id": "new-id"}

        with patch.object(gmail_mcp, "get_gmail_service", return_value=service):
            gmail_mcp.reply_email("gmail-message-id", "Follow-up")

        send_kwargs = messages_api.send.call_args.kwargs
        mime_msg = self._decode_mime(send_kwargs["body"]["raw"])
        self.assertEqual(mime_msg["Subject"], "Re: Existing thread")

    def test_config_path_defaults_are_module_relative(self):
        expected = Path("~/.config/gmail-mcp/google_creds.json").expanduser()
        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    resolved = gmail_mcp._resolve_config_path(
                        "GOOGLE_CREDS_PATH", "google_creds.json"
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(resolved, expected)

    def test_send_email_sets_reply_to_and_returns_thread_metadata(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.send.return_value.execute.return_value = {
            "id": "sent-id",
            "threadId": "thread-1",
        }

        with patch.object(gmail_mcp, "get_gmail_service", return_value=service):
            result = gmail_mcp.send_email(
                "bob@example.com",
                "Hi",
                "Test body",
                reply_to="leadborg+abc@example.com",
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider_message_id"], "sent-id")
        self.assertEqual(result["provider_thread_id"], "thread-1")
        send_kwargs = messages_api.send.call_args.kwargs
        mime_msg = self._decode_mime(send_kwargs["body"]["raw"])
        self.assertEqual(mime_msg["Reply-To"], "leadborg+abc@example.com")

    def test_transport_defaults_to_stdio_and_maps_http_alias(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gmail_mcp._resolve_transport(), "stdio")

        with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
            self.assertEqual(gmail_mcp._resolve_transport(), "streamable-http")

    def test_send_email_maps_http_error_to_structured_payload(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        response = SimpleNamespace(status=403, reason="Forbidden")
        messages_api.send.return_value.execute.side_effect = HttpError(
            response, b'{"error":{"message":"Forbidden"}}'
        )

        with patch.object(gmail_mcp, "get_gmail_service", return_value=service):
            result = gmail_mcp.send_email(
                "bob@example.com", "Hi", "Test body"
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["operation"], "send_email")
        self.assertEqual(result["error"]["code"], "auth_error")
        self.assertEqual(result["error"]["details"]["status"], 403)

    def test_read_emails_from_sender_returns_messages(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.list.return_value.execute.return_value = {
            "messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ]
        }
        messages_api.get.return_value.execute.side_effect = [
            {
                "threadId": "t1",
                "snippet": "first snippet",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "Subject", "value": "First"},
                        {"name": "Date", "value": "Fri, 6 Mar 2026"},
                    ]
                },
            },
            {
                "threadId": "t2",
                "snippet": "second snippet",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "Subject", "value": "Second"},
                        {"name": "Date", "value": "Thu, 5 Mar 2026"},
                    ]
                },
            },
        ]

        with patch.object(gmail_mcp, "get_gmail_service", return_value=service):
            result = gmail_mcp.read_emails_from_sender("alice@example.com", 2)

        self.assertEqual(result["sender"], "alice@example.com")
        self.assertEqual(result["query"], "from:alice@example.com")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["messages"][0]["id"], "m1")
        self.assertEqual(result["messages"][0]["subject"], "First")
        self.assertEqual(result["messages"][1]["id"], "m2")
        self.assertEqual(result["messages"][1]["subject"], "Second")
        messages_api.list.assert_called_once_with(
            userId="me", q="from:alice@example.com", maxResults=2
        )

    def test_read_emails_from_sender_requires_sender(self):
        result = gmail_mcp.read_emails_from_sender("   ", 5)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["operation"], "read_emails_from_sender")
        self.assertEqual(result["error"]["code"], "invalid_request")

    # ----- Calendar helpers -----

    def _make_calendar_event(self, event_id="evt-1", summary="Test Event", start=None, end=None):
        return {
            "id": event_id,
            "summary": summary,
            "description": "A test event",
            "location": "Conference Room",
            "start": start or {"dateTime": "2026-05-14T10:00:00", "timeZone": "UTC"},
            "end": end or {"dateTime": "2026-05-14T11:00:00", "timeZone": "UTC"},
            "status": "confirmed",
            "creator": {"email": "creator@example.com"},
            "organizer": {"email": "organizer@example.com"},
            "attendees": [],
            "htmlLink": "https://calendar.google.com/event?eid=abc",
            "recurrence": [],
            "reminders": {"useDefault": True},
        }

    def test_list_calendars_returns_calendars(self):
        service = MagicMock()
        cal_list_api = service.calendarList.return_value.list.return_value
        cal_list_api.execute.return_value = {
            "items": [
                {"id": "primary", "summary": "My Calendar", "description": "", "primary": True},
                {"id": "sec", "summary": "Secondary", "description": "Work", "primary": False},
            ]
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.list_calendars()

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "primary")
        self.assertEqual(result[0]["primary"], True)
        self.assertEqual(result[1]["summary"], "Secondary")

    def test_list_events_returns_formatted_events(self):
        service = MagicMock()
        events_api = service.events.return_value.list.return_value
        events_api.execute.return_value = {
            "items": [
                self._make_calendar_event("e1", "Meeting 1"),
                self._make_calendar_event("e2", "Meeting 2"),
            ]
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.list_events("primary", 5)

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["events"][0]["summary"], "Meeting 1")
        self.assertEqual(result["events"][1]["id"], "e2")
        service.events.return_value.list.assert_called_once()

    def test_list_events_validates_max_results(self):
        result = gmail_mcp.list_events("primary", 0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_get_event_returns_event(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = (
            self._make_calendar_event("evt-1", "All-Hands")
        )

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.get_event("primary", "evt-1")

        self.assertEqual(result["summary"], "All-Hands")
        self.assertEqual(result["id"], "evt-1")
        self.assertIn("htmlLink", result)

    def test_get_event_validates_params(self):
        result = gmail_mcp.get_event("", "evt-1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_request")

        result = gmail_mcp.get_event("primary", "")
        self.assertEqual(result["status"], "error")

    def test_create_event_creates_and_returns_metadata(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {
            "id": "new-evt",
            "htmlLink": "https://calendar.google.com/event?eid=new",
            "summary": "New Event",
            "start": {"dateTime": "2026-05-14T14:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-14T15:00:00", "timeZone": "UTC"},
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.create_event(
                "primary",
                "New Event",
                "2026-05-14T14:00:00",
                "2026-05-14T15:00:00",
                timezone="UTC",
                description="desc",
                location="Rm 1",
                attendees=["alice@example.com"],
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["event_id"], "new-evt")
        self.assertEqual(result["summary"], "New Event")
        call_kwargs = service.events.return_value.insert.call_args.kwargs
        self.assertEqual(call_kwargs["body"]["description"], "desc")
        self.assertEqual(call_kwargs["body"]["location"], "Rm 1")
        self.assertEqual(call_kwargs["body"]["attendees"], [{"email": "alice@example.com"}])

    def test_create_event_validates_required_fields(self):
        result = gmail_mcp.create_event("primary", "", "2026-01-01T00:00:00", "2026-01-01T01:00:00")
        self.assertEqual(result["status"], "error")

    def test_update_event_updates_fields(self):
        existing = self._make_calendar_event("evt-1", "Old Summary")
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = existing
        service.events.return_value.update.return_value.execute.return_value = {
            **existing,
            "summary": "Updated Summary",
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.update_event("primary", "evt-1", summary="Updated Summary")

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["summary"], "Updated Summary")
        update_kwargs = service.events.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs["body"]["summary"], "Updated Summary")

    def test_update_event_validates_params(self):
        result = gmail_mcp.update_event("", "evt-1", summary="x")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_delete_event_deletes(self):
        service = MagicMock()
        delete_api = service.events.return_value.delete.return_value

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.delete_event("primary", "evt-1")

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["event_id"], "evt-1")
        delete_api.execute.assert_called_once()

    def test_respond_to_event_sets_response_status(self):
        existing = self._make_calendar_event("evt-1", "Invite")
        existing["attendees"] = [
            {"email": "me@example.com", "responseStatus": "needsAction", "self": True},
        ]
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = existing
        service.calendarList.return_value.get.return_value.execute.return_value = {
            "id": "me@example.com"
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.respond_to_event("primary", "evt-1", "accepted")

        self.assertEqual(result["status"], "responded")
        self.assertEqual(result["response_status"], "accepted")
        update_kwargs = service.events.return_value.update.call_args.kwargs
        self.assertEqual(
            update_kwargs["body"]["attendees"][0]["responseStatus"], "accepted"
        )

    def test_respond_to_event_validates_status(self):
        result = gmail_mcp.respond_to_event("primary", "evt-1", "maybe")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_suggest_time_returns_suggestions(self):
        service = MagicMock()
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {
                "primary": {"busy": []}
            }
        }

        with patch.object(gmail_mcp, "get_calendar_service", return_value=service):
            result = gmail_mcp.suggest_time(
                duration_minutes=60,
                time_min="2026-05-14T09:00:00+00:00",
                time_max="2026-05-14T17:00:00+00:00",
                timezone="UTC",
            )

        self.assertIn("suggestions", result)
        self.assertGreater(len(result["suggestions"]), 0)

    def test_suggest_time_validates_duration(self):
        result = gmail_mcp.suggest_time(duration_minutes=0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_forward_recent_replies_posts_signed_payloads(self):
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "m1", "threadId": "t1"}]
        }
        messages_api.get.return_value.execute.return_value = {
            "threadId": "t1",
            "snippet": "reply snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "To", "value": "leadborg+abc@example.com"},
                    {"name": "Subject", "value": "Re: Hello"},
                ]
            },
        }

        posted = []

        def _fake_post(url, secret, payload):
            posted.append((url, secret, payload))
            return {"status_code": 200, "response": {"ok": True}}

        with (
            patch.object(gmail_mcp, "get_gmail_service", return_value=service),
            patch.object(gmail_mcp, "_post_signed_json", _fake_post),
        ):
            result = gmail_mcp.forward_recent_replies_to_webhook(
                "https://leadborg.example/api/webhooks/email/mcp",
                "secret",
                query="to:leadborg+",
                max_results=1,
            )

        self.assertEqual(result["status"], "forwarded")
        self.assertEqual(result["count"], 1)
        self.assertEqual(posted[0][0], "https://leadborg.example/api/webhooks/email/mcp")
        self.assertEqual(posted[0][1], "secret")
        self.assertEqual(posted[0][2]["provider_message_id"], "m1")
        self.assertEqual(posted[0][2]["to"], "leadborg+abc@example.com")


if __name__ == "__main__":
    unittest.main()
