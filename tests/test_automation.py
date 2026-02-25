import pytest
from unittest.mock import patch, MagicMock
import os
import json


class TestPolicy:
    def test_policy_file_exists(self):
        assert os.path.exists("policy.json")

    def test_policy_structure(self):
        with open("policy.json", "r") as f:
            policy = json.load(f)
        
        assert "block_reply_domains" in policy
        assert "never_reply_keywords" in policy
        assert "max_send_per_run" in policy
        assert "delete_strategy" in policy
        assert policy["delete_strategy"] == "two_step"


class TestAutomationEngine:
    @patch.dict(os.environ, {
        "CLIENT_ID": "test",
        "CLIENT_SECRET": "test",
        "BASE_URL": "https://test.com",
        "APPLE_EMAIL": "test@icloud.com",
        "APPLE_APP_PASSWORD": "test",
    })
    def test_classify_otp_email(self):
        from automation import AutomationEngine
        
        engine = AutomationEngine({})
        email = {
            "from": "security@bank.com",
            "subject": "Seu código de verificação",
            "body": "Use o código 123456 para acessar sua conta"
        }
        
        category, priority, action, reason = engine.classify_email(email)
        assert category == "otp"
        assert action == "mark_read"

    @patch.dict(os.environ, {
        "CLIENT_ID": "test",
        "CLIENT_SECRET": "test",
        "BASE_URL": "https://test.com",
        "APPLE_EMAIL": "test@icloud.com",
        "APPLE_APP_PASSWORD": "test",
    })
    def test_classify_noreply_email(self):
        from automation import AutomationEngine
        
        engine = AutomationEngine({})
        email = {
            "from": "no-reply@company.com",
            "subject": "Order shipped",
            "body": "Your order has been shipped and is on the way"
        }
        
        category, priority, action, reason = engine.classify_email(email)
        assert category == "automated"
        assert action == "mark_read"

    @patch.dict(os.environ, {
        "CLIENT_ID": "test",
        "CLIENT_SECRET": "test",
        "BASE_URL": "https://test.com",
        "APPLE_EMAIL": "test@icloud.com",
        "APPLE_APP_PASSWORD": "test",
    })
    def test_classify_newsletter(self):
        from automation import AutomationEngine
        
        engine = AutomationEngine({})
        email = {
            "from": "news@company.com",
            "subject": "Weekly Newsletter",
            "body": "Click here to unsubscribe from this mailing list"
        }
        
        category, priority, action, reason = engine.classify_email(email)
        assert category == "newsletter"
        assert action == "delete_candidate"

    @patch.dict(os.environ, {
        "CLIENT_ID": "test",
        "CLIENT_SECRET": "test",
        "BASE_URL": "https://test.com",
        "APPLE_EMAIL": "test@icloud.com",
        "APPLE_APP_PASSWORD": "test",
    })
    def test_classify_human_email(self):
        from automation import AutomationEngine
        
        engine = AutomationEngine({})
        email = {
            "from": "john@client.com",
            "subject": "Meeting tomorrow",
            "body": "Hi, can we schedule a call for tomorrow?"
        }
        
        category, priority, action, reason = engine.classify_email(email)
        assert category == "human"
        assert action == "suggest_reply"


class TestAutomationEndpoints:
    @pytest.fixture
    def client(self):
        with patch.dict(os.environ, {
            "CLIENT_ID": "test-client-id",
            "CLIENT_SECRET": "test-client-secret",
            "BASE_URL": "https://test.example.com",
            "APPLE_EMAIL": "test@icloud.com",
            "APPLE_APP_PASSWORD": "test-password",
            "OPENAI_API_KEY": "test-openai-key",
        }):
            from main import app
            from fastapi.testclient import TestClient
            yield TestClient(app)

    def test_get_policy(self, client):
        response = client.get("/automation/policy")
        assert response.status_code == 200
        data = response.json()
        assert "policy" in data
        assert "max_send_per_run" in data["policy"]

    def test_get_logs(self, client):
        response = client.get("/automation/logs")
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data
        assert "count" in data

    def test_dry_run_mode(self, client, mock_imap):
        mock_imap.uid.return_value = ('OK', [b''])
        
        response = client.post("/automation/run", json={
            "providers": ["apple"],
            "folders": ["inbox"],
            "max_per_provider": 1,
            "mode": "dry_run",
            "since_hours": 24
        })
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "dry_run"
        assert "processed" in data
        assert "executed" in data
        assert "skipped" in data
