# InboxPilot v1.1

Email assistant powered by AI that helps you manage emails across multiple providers.

## Features

- **Multi-provider support**: Apple Mail, Gmail, Microsoft Outlook
- **AI-powered replies**: Uses OpenAI to suggest intelligent responses
- **Web Dashboard**: Visual interface at `/dashboard` for email management
- **Chat/Travel Mode**: Copy-paste workflow for using with ChatGPT
- **Batch Processing**: Queue actions and execute in bulk
- **Session Management**: Group emails by work session
- **Two-step Delete**: Safe deletion with pending confirmation

## Quick Start

1. Access the dashboard at `/dashboard`
2. Click "Atualizar" to fetch new emails
3. Select an email to view details
4. Add actions to the queue (delete, mark_read, send, skip)
5. Click "Executar Tudo" to process the queue

## Usage Modes

### Mode A: Web Dashboard (Desktop)

1. Go to `/dashboard`
2. Use filters to select period, providers, and folders
3. Click "Atualizar" to start a session and fetch emails
4. Click on emails to view full content
5. Use action buttons to queue operations
6. Execute all queued actions with one click

### Mode B: Chat/Travel Mode (Mobile/ChatGPT)

1. Call `GET /assistant/read?limit=20` to get email summary
2. Copy the `copy_paste_block` field
3. Paste into ChatGPT and ask for action recommendations
4. ChatGPT returns a JSON action plan
5. Paste the JSON into `POST /assistant/plan`
6. Execute with `POST /automation/execute`

Example JSON plan from ChatGPT:
```json
{
  "session_id": "sess_20260119_120000",
  "actions": [
    {"key": "apple:12345", "action": "delete"},
    {"key": "gmail:abc123", "action": "mark_read"},
    {"key": "apple:67890", "action": "send", "body": "Thank you for your email..."}
  ]
}
```

## API Endpoints

### Health & Status
- `GET /` - Health check (returns version 1.1)
- `GET /health` - Quick health check
- `GET /setup` - Provider configuration status

### Session Management
- `POST /session/start` - Start new session and collect emails
- `GET /session/{session_id}/items` - List items in session

### Assistant (Chat Mode)
- `GET /assistant/read` - Get emails with copy-paste block
- `GET /assistant/email/{key}` - Get full email details
- `POST /assistant/plan` - Queue actions from JSON plan

### Automation
- `POST /automation/execute` - Execute queued actions
- `GET /automation/report` - View execution report
- `POST /automation/run` - Run automation engine

### Dashboard
- `GET /dashboard` - Main web dashboard
- `GET /ui` - Alternative dashboard URL

## Example Flow (curl)

```bash
# 1. Start session
curl -X POST http://localhost:5000/session/start \
  -H "Content-Type: application/json" \
  -d '{"providers":["apple","gmail"],"folders":["inbox"],"range_filter":"today"}'

# 2. Read emails
curl "http://localhost:5000/assistant/read?limit=10"

# 3. View specific email
curl "http://localhost:5000/assistant/email/apple:12345"

# 4. Queue actions
curl -X POST http://localhost:5000/assistant/plan \
  -H "Content-Type: application/json" \
  -d '{"actions":[{"key":"apple:12345","action":"mark_read"}]}'

# 5. Execute (dry run first)
curl -X POST http://localhost:5000/automation/execute \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true}'

# 6. Execute for real
curl -X POST http://localhost:5000/automation/execute \
  -H "Content-Type: application/json" \
  -d '{"dry_run":false}'
```

## Environment Variables

Required:
- `APPLE_EMAIL` - Apple/iCloud email address
- `APPLE_APP_PASSWORD` - App-specific password for Apple Mail
- `CLIENT_ID` - OAuth client ID (Gmail/Microsoft)
- `CLIENT_SECRET` - OAuth client secret
- `OPENAI_API_KEY` - OpenAI API key for AI features

Optional:
- `INBOXPILOT_API_KEY` - API key to protect endpoints
- `BASE_URL` - Base URL for OAuth callbacks

## Security

When `INBOXPILOT_API_KEY` is set:
- Mutating endpoints require `X-API-Key` header
- Dashboard remains accessible for viewing
- Actions (send, delete, etc.) are protected

Integração Outlook local via pywin32 - testando sync GitHub/Replit
