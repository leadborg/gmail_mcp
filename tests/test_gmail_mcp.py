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
        expected = Path(gmail_mcp.__file__).resolve().parent / "google_creds.json"
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


if __name__ == "__main__":
    unittest.main()
