import pytest
from unittest.mock import MagicMock, patch
import os

from providers.base import EmailProvider, EmailMessage, DebugStatus


class TestEmailMessage:
    def test_to_dict(self, mock_email_message):
        result = mock_email_message.to_dict()
        
        assert result["id"] == "test-123"
        assert result["provider"] == "test"
        assert result["from"] == "sender@example.com"
        assert result["subject"] == "Test Subject"
        assert result["body"] == "This is a test email body."
        assert result["date"] == "2026-01-18T10:00:00Z"
        assert result["folder"] == "inbox"
        assert "snippet" in result

    def test_email_message_creation(self):
        msg = EmailMessage(
            id="abc",
            provider="gmail",
            from_addr="test@test.com",
            subject="Hello",
            body="World",
            date="2026-01-01",
        )
        assert msg.id == "abc"
        assert msg.provider == "gmail"
        assert msg.folder == "inbox"

    def test_snippet_length(self):
        long_body = "A" * 500
        msg = EmailMessage(
            id="test",
            provider="test",
            from_addr="test@test.com",
            subject="Test",
            body=long_body,
            date="2026-01-01",
        )
        result = msg.to_dict()
        assert len(result["snippet"]) == 160


class TestDebugStatus:
    def test_to_dict(self, mock_debug_status):
        result = mock_debug_status.to_dict()
        
        assert result["connection"] == "OK"
        assert result["email"] == "test@example.com"
        assert result["folders"] == ["INBOX", "Sent", "Trash"]
        assert result["inbox_total"] == 100
        assert result["inbox_unseen"] == 5
        assert result["junk_total"] == 10
        assert result["junk_unseen"] == 2


class TestAppleMailProvider:
    @patch.dict(os.environ, {"APPLE_EMAIL": "test@icloud.com", "APPLE_APP_PASSWORD": "testpass"})
    def test_init(self):
        from providers.apple import AppleMailProvider
        provider = AppleMailProvider()
        assert provider.provider_name == "apple"
        assert provider.email == "test@icloud.com"

    @patch.dict(os.environ, {"APPLE_EMAIL": "", "APPLE_APP_PASSWORD": ""})
    def test_require_creds_raises(self):
        from providers.apple import AppleMailProvider
        provider = AppleMailProvider()
        with pytest.raises(Exception) as exc:
            provider._require_creds()
        assert "not configured" in str(exc.value)


class TestMicrosoftProvider:
    def test_init(self):
        from providers.microsoft import MicrosoftProvider
        mock_token_func = MagicMock(return_value="test-token")
        provider = MicrosoftProvider(mock_token_func)
        assert provider.provider_name == "microsoft"

    def test_queue_next_empty(self, mock_graph_requests):
        from providers.microsoft import MicrosoftProvider
        mock_token_func = MagicMock(return_value="test-token")
        provider = MicrosoftProvider(mock_token_func)
        
        mock_graph_requests.get.return_value.json.return_value = {"value": []}
        
        result = provider.queue_next()
        assert result is None

    def test_queue_next_returns_message(self, mock_graph_requests):
        from providers.microsoft import MicrosoftProvider
        mock_token_func = MagicMock(return_value="test-token")
        provider = MicrosoftProvider(mock_token_func)
        
        mock_graph_requests.get.return_value.json.return_value = {
            "value": [{
                "id": "msg-123",
                "subject": "Test",
                "from": {"emailAddress": {"address": "sender@test.com"}},
                "receivedDateTime": "2026-01-18",
                "body": {"content": "Hello", "contentType": "text"},
            }]
        }
        
        result = provider.queue_next()
        assert result is not None
        assert result.id == "msg-123"
        assert result.provider == "microsoft"


class TestGmailProvider:
    @patch.dict(os.environ, {"GMAIL_CLIENT_ID": "client123", "GMAIL_CLIENT_SECRET": "secret456"})
    def test_init(self):
        from providers.gmail import GmailProvider
        provider = GmailProvider(base_url="https://example.com")
        assert provider.provider_name == "gmail"
        assert provider.client_id == "client123"

    @patch.dict(os.environ, {"GMAIL_CLIENT_ID": "", "GMAIL_CLIENT_SECRET": ""})
    def test_require_creds_raises(self):
        from providers.gmail import GmailProvider
        provider = GmailProvider(base_url="https://example.com")
        with pytest.raises(Exception) as exc:
            provider._require_creds()
        assert "not configured" in str(exc.value)

    @patch.dict(os.environ, {"GMAIL_CLIENT_ID": "client123", "GMAIL_CLIENT_SECRET": "secret456"})
    def test_handle_callback_validates_state(self):
        from providers.gmail import GmailProvider
        from store import set_item
        
        provider = GmailProvider(base_url="https://example.com")
        set_item("gmail_oauth_state", "valid-state-123")
        
        with pytest.raises(Exception) as exc:
            provider.handle_callback(code="test-code", state="invalid-state")
        assert "Invalid OAuth state" in str(exc.value) or "CSRF" in str(exc.value)

    @patch.dict(os.environ, {"GMAIL_CLIENT_ID": "client123", "GMAIL_CLIENT_SECRET": "secret456"})
    def test_handle_callback_requires_stored_state(self):
        from providers.gmail import GmailProvider
        from store import set_item
        
        provider = GmailProvider(base_url="https://example.com")
        set_item("gmail_oauth_state", "")
        
        with pytest.raises(Exception) as exc:
            provider.handle_callback(code="test-code", state="any-state")
        assert "OAuth state not found" in str(exc.value)

    @patch.dict(os.environ, {"GMAIL_CLIENT_ID": "client123", "GMAIL_CLIENT_SECRET": "secret456"})
    def test_get_service_without_token(self):
        from providers.gmail import GmailProvider
        from store import set_item
        
        provider = GmailProvider(base_url="https://example.com")
        set_item("gmail_token", "")
        
        with pytest.raises(Exception) as exc:
            provider._get_service()
        assert "Not authenticated" in str(exc.value)
