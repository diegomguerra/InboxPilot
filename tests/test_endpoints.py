import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import os


@pytest.fixture
def client():
    with patch.dict(os.environ, {
        "CLIENT_ID": "test-client-id",
        "CLIENT_SECRET": "test-client-secret",
        "BASE_URL": "https://test.example.com",
        "APPLE_EMAIL": "test@icloud.com",
        "APPLE_APP_PASSWORD": "test-password",
        "OPENAI_API_KEY": "test-openai-key",
    }):
        from main import app
        yield TestClient(app)


class TestHealthCheck:
    def test_root(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["app"] == "InboxPilot"
        assert data["version"] == "1.1"

    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] == True


class TestProvidersEndpoint:
    def test_providers_list(self, client):
        response = client.get("/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert "microsoft" in data["providers"]
        assert "apple" in data["providers"]
        assert "gmail" in data["providers"]


class TestUnifiedEndpoints:
    def test_unified_queue_next_invalid_provider(self, client):
        response = client.get("/unified/queue/next?provider=invalid")
        assert response.status_code == 400
        assert "Unknown provider" in response.json()["detail"]

    def test_unified_queue_next_apple_empty(self, client, mock_imap):
        mock_imap.uid.return_value = ('OK', [b''])
        response = client.get("/unified/queue/next?provider=apple")
        assert response.status_code == 200
        data = response.json()
        assert data.get("empty") == True or data.get("provider") == "apple"


class TestMicrosoftEndpoints:
    def test_login_redirects(self, client):
        with patch('main._msal_app') as mock_msal:
            mock_app = MagicMock()
            mock_app.get_authorization_request_url.return_value = "https://login.microsoft.com/auth"
            mock_msal.return_value = mock_app
            
            response = client.get("/login", follow_redirects=False)
            assert response.status_code == 307

    def test_queue_next_unauthenticated(self, client):
        with patch('main.get_item', return_value=None):
            response = client.get("/queue/next")
            assert response.status_code == 401
            assert "Not authenticated" in response.json()["detail"]


class TestAppleEndpoints:
    def test_apple_debug_status(self, client, mock_imap):
        mock_imap.list.return_value = ('OK', [b'INBOX', b'Sent'])
        mock_imap.uid.return_value = ('OK', [b'1 2 3'])
        
        response = client.get("/apple/debug/status")
        assert response.status_code == 200
        data = response.json()
        assert data["connection"] == "OK"

    def test_apple_queue_next_empty(self, client, mock_imap):
        mock_imap.uid.return_value = ('OK', [b''])
        
        response = client.get("/apple/queue/next")
        assert response.status_code == 200
        data = response.json()
        assert data.get("empty") == True or data.get("provider") == "apple"


class TestUtilityFunctions:
    def test_html_to_text(self):
        from utils.text import html_to_text
        
        html = "<html><body><p>Hello <b>World</b></p></body></html>"
        result = html_to_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "<" not in result

    def test_html_to_text_with_scripts(self):
        from utils.text import html_to_text
        
        html = "<html><script>alert('evil')</script><p>Safe content</p></html>"
        result = html_to_text(html)
        assert "Safe content" in result
        assert "alert" not in result

    def test_parse_email_address(self):
        from utils.text import parse_email_address
        
        assert parse_email_address("John Doe <john@example.com>") == "john@example.com"
        assert parse_email_address("simple@example.com") == "simple@example.com"
        assert parse_email_address("") == ""

    def test_normalize_text(self):
        from utils.text import normalize_text
        
        assert normalize_text("Hello &amp; World") == "Hello & World"
        assert normalize_text("  spaces  ") == "spaces"
        assert normalize_text("") == ""


class TestExportEndpoints:
    def test_export_pdf_returns_pdf(self, client):
        response = client.get("/export/pdf?providers=apple&folders=inbox&range=today&limit_per_provider=5")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert b"%PDF" in response.content[:10]

    def test_export_pdf_headers(self, client):
        response = client.get("/export/pdf")
        assert response.status_code == 200
        assert "x-export-id" in response.headers
        assert "x-email-count" in response.headers

    def test_dispatch_example(self, client):
        response = client.get("/dispatch/example")
        assert response.status_code == 200
        data = response.json()
        assert "export_id" in data
        assert "actions" in data
        assert len(data["actions"]) > 0


class TestDispatchImport:
    def test_dispatch_import_dry_run(self, client):
        payload = {
            "export_id": "test-123",
            "dry_run": True,
            "actions": [
                {"key": "apple:nonexistent", "decision": "mark_read"}
            ]
        }
        response = client.post("/dispatch/import", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] == True
        assert data["dry_run"] == True
        assert "counts" in data

    def test_dispatch_import_invalid_decision(self, client):
        payload = {
            "export_id": "test-123",
            "dry_run": True,
            "actions": [
                {"key": "apple:12345", "decision": "invalid_action"}
            ]
        }
        response = client.post("/dispatch/import", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["counts"]["errors"] == 1

    def test_dispatch_import_send_requires_reply(self, client):
        payload = {
            "export_id": "test-123",
            "dry_run": True,
            "actions": [
                {"key": "apple:12345", "decision": "send"}
            ]
        }
        response = client.post("/dispatch/import", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["counts"]["errors"] == 1
        assert "reply required" in data["results"][0]["detail"]

    def test_dispatch_import_malformed_json(self, client):
        response = client.post("/dispatch/import", content="not json", headers={"Content-Type": "application/json"})
        assert response.status_code == 422
