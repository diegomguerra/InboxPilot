import pytest
from unittest.mock import MagicMock, patch
from providers.base import EmailMessage, DebugStatus


@pytest.fixture
def mock_email_message():
    return EmailMessage(
        id="test-123",
        provider="test",
        from_addr="sender@example.com",
        subject="Test Subject",
        body="This is a test email body.",
        date="2026-01-18T10:00:00Z",
    )


@pytest.fixture
def mock_debug_status():
    return DebugStatus(
        connection="OK",
        email="test@example.com",
        folders=["INBOX", "Sent", "Trash"],
        inbox_total=100,
        inbox_unseen=5,
        junk_total=10,
        junk_unseen=2,
    )


@pytest.fixture
def mock_imap():
    with patch('imaplib.IMAP4_SSL') as mock:
        mail = MagicMock()
        mock.return_value = mail
        mail.login.return_value = ('OK', [])
        mail.select.return_value = ('OK', [b'1'])
        mail.logout.return_value = ('OK', [])
        yield mail


@pytest.fixture
def mock_smtp():
    with patch('smtplib.SMTP') as mock:
        server = MagicMock()
        mock.return_value = server
        yield server


@pytest.fixture
def mock_graph_requests():
    with patch('graph.requests') as mock:
        response = MagicMock()
        response.json.return_value = {}
        response.text = '{}'
        response.raise_for_status = MagicMock()
        mock.get.return_value = response
        mock.post.return_value = response
        mock.patch.return_value = response
        mock.delete.return_value = response
        yield mock


@pytest.fixture
def mock_openai():
    with patch('llm.requests.post') as mock:
        response = MagicMock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"summary": "Test", "priority": "Low", "suggested_reply": "Reply text", "questions_to_answer": []}'
                }
            }]
        }
        response.raise_for_status = MagicMock()
        mock.return_value = response
        yield mock
