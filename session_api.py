import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Header, Depends, Query, Request
from pydantic import BaseModel

from db import (
    init_db, get_message, get_draft, make_key,
    create_session, get_session, get_open_session, close_session,
    add_session_item, get_session_items, get_session_item_count,
    add_queued_action, get_queued_actions, update_action_status,
    link_message_to_session, mark_status, log_action,
    snapshot_save, snapshot_get_latest, snapshot_cleanup,
)
from assistant_loop import load_policy, classify_email, safe_extract_text
from time_filters import period_to_range, get_date_range_info

router = APIRouter()

INBOXPILOT_API_KEY = os.getenv("INBOXPILOT_API_KEY")

_providers_map = {}

def set_providers(providers: Dict):
    global _providers_map
    _providers_map = providers


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if INBOXPILOT_API_KEY:
        if not x_api_key or x_api_key != INBOXPILOT_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    return True


def _get_provider(name: str):
    if name not in _providers_map:
        raise HTTPException(400, f"Unknown provider: {name}")
    return _providers_map[name]


@router.get("/apple/folders")
def list_apple_folders(_: bool = Depends(check_api_key)):
    if "apple" not in _providers_map:
        raise HTTPException(400, "Apple provider not configured")
    provider = _providers_map["apple"]
    folders = provider.list_folders()
    return {"folders": folders}


class SessionStartRequest(BaseModel):
    providers: List[str] = ["apple", "gmail"]
    folders: List[str] = ["inbox"]
    date_mode: str = "today"
    rolling_days: Optional[int] = 7
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    max_per_provider: int = 50


class PlanAction(BaseModel):
    key: str
    action: str
    body: Optional[str] = None


class PlanRequest(BaseModel):
    session_id: Optional[str] = None
    actions: List[PlanAction]


class ExecuteRequest(BaseModel):
    session_id: Optional[str] = None
    dry_run: bool = False
    max_actions: int = 200


def _get_date_range(date_mode: str, rolling_days: int = 7, from_date: str = None, to_date: str = None):
    """
    Get date range using unified time_filters (America/Sao_Paulo).
    Returns (start_utc, end_utc) tuple.
    """
    info = get_date_range_info(date_mode, rolling_days, from_date, to_date)
    return (info["start_utc"], info["end_utc"])


@router.post("/session/start")
def session_start(request: SessionStartRequest, _: bool = Depends(check_api_key)):
    init_db()
    
    now = datetime.now(timezone.utc)
    session_id = f"sess_{now.strftime('%Y%m%d_%H%M%S')}"
    
    existing = get_open_session()
    if existing:
        close_session(existing["id"])
    
    create_session(session_id, request.providers, request.folders, request.date_mode, request.rolling_days, request.from_date, request.to_date)
    
    date_start, date_end = _get_date_range(request.date_mode, request.rolling_days or 7, request.from_date, request.to_date)
    
    import logging
    logging.info(f"Session {session_id} filters: date_mode={request.date_mode}, rolling_days={request.rolling_days}, from={request.from_date}, to={request.to_date}")
    logging.info(f"Session {session_id} date range: {date_start.isoformat()} to {date_end.isoformat()}")
    
    policy = load_policy()
    counts = {}
    total = 0
    
    for prov_name in request.providers:
        if prov_name not in _providers_map:
            continue
        
        provider = _providers_map[prov_name]
        prov_count = 0
        
        for folder in request.folders:
            try:
                emails = provider.list_emails(
                    folder=folder,
                    limit=request.max_per_provider,
                    date_start=date_start,
                    date_end=date_end,
                    unread_only=False
                )
                
                for email_msg in emails:
                    email_dict = email_msg.to_dict()
                    msg_id = email_dict.get("id", "")
                    key = make_key(prov_name, msg_id)
                    
                    from_addr = email_dict.get("from", "")
                    subject = email_dict.get("subject", "")
                    body = email_dict.get("body", "")
                    body_text = safe_extract_text(body)
                    date_str = email_dict.get("date", "")
                    
                    classification = classify_email(from_addr, subject, body_text, policy)
                    category = classification.get("category", "UNKNOWN")
                    
                    from db import upsert_message
                    upsert_message(
                        key=key,
                        provider=prov_name,
                        msg_id=msg_id,
                        folder=folder,
                        from_addr=from_addr,
                        subject=subject,
                        date=date_str,
                        body=body_text,
                        status="classified",
                        category=category,
                        priority=classification.get("priority", "normal")
                    )
                    
                    link_message_to_session(key, session_id)
                    add_session_item(session_id, key, prov_name, msg_id, date_str, category, subject, from_addr)
                    
                    prov_count += 1
                    total += 1
                    
            except Exception as e:
                continue
        
        counts[prov_name] = prov_count
    
    counts["total"] = total
    
    return {
        "session_id": session_id,
        "reused": False,
        "counts": counts
    }


@router.get("/session/{session_id}/items")
def session_items(
    session_id: str,
    limit: int = 100,
    provider: Optional[str] = None,
    folder: Optional[str] = None,
    classification: Optional[str] = None,
    _: bool = Depends(check_api_key)
):
    init_db()
    
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")
    
    items = get_session_items(session_id, limit, provider, folder, classification)
    
    result_items = []
    for item in items:
        result_items.append({
            "key": item.get("key"),
            "provider": item.get("provider"),
            "message_id": item.get("message_id"),
            "folder": item.get("folder"),
            "date": item.get("date"),
            "from": item.get("sender"),
            "subject": item.get("subject"),
            "classification": item.get("classification"),
            "status": item.get("status")
        })
    
    return {
        "session_id": session_id,
        "total": len(result_items),
        "items": result_items
    }


@router.get("/assistant/read")
def assistant_read(
    session_id: Optional[str] = None,
    limit: int = 10,
    include_body: bool = False,
    _: bool = Depends(check_api_key)
):
    init_db()
    
    if not session_id:
        session = get_open_session()
        if not session:
            raise HTTPException(404, "No open session found. Call POST /session/start first.")
        session_id = session["id"]
    
    items = get_session_items(session_id, limit)
    
    emails = []
    for idx, item in enumerate(items, 1):
        email_data = {
            "index": idx,
            "key": item.get("key"),
            "provider": item.get("provider"),
            "folder": item.get("folder"),
            "date": item.get("date"),
            "from": item.get("sender"),
            "subject": item.get("subject"),
            "classification": item.get("classification")
        }
        
        msg = get_message(item.get("key"))
        if msg:
            email_data["summary"] = (msg.get("body_text") or "")[:200]
            if include_body:
                email_data["body"] = msg.get("body_text") or ""
        
        emails.append(email_data)
    
    copy_paste = _generate_copy_paste_block(emails, session_id)
    
    return {
        "session_id": session_id,
        "emails": emails,
        "copy_paste_block": copy_paste
    }


def _generate_copy_paste_block(emails: List[Dict], session_id: str) -> str:
    lines = [
        f"=== InboxPilot Session: {session_id} ===",
        f"Total: {len(emails)} emails",
        "",
        "For each email, respond with a JSON action plan:",
        '{"session_id":"' + session_id + '","actions":[{"key":"...","action":"send|delete|mark_read|skip","body":"(optional reply text)"}]}',
        "",
        "EMAILS:",
        ""
    ]
    
    for email in emails:
        lines.append(f"[{email['index']}] {email['classification']} | {email['from']}")
        lines.append(f"    Subject: {email['subject']}")
        lines.append(f"    Key: {email['key']}")
        if email.get("summary"):
            lines.append(f"    Preview: {email['summary'][:100]}...")
        lines.append("")
    
    return "\n".join(lines)


@router.get("/session/{session_id}/export")
def session_export(
    session_id: str,
    format: str = "text",
    _: bool = Depends(check_api_key)
):
    init_db()
    
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")
    
    items = get_session_items(session_id, limit=500)
    
    providers = json.loads(session.get("providers", "[]"))
    folders = json.loads(session.get("folders", "[]"))
    date_mode = session.get("date_mode") or session.get("range_filter", "today")
    rolling_days = session.get("rolling_days") or 7
    stored_from_date = session.get("from_date")
    stored_to_date = session.get("to_date")
    
    date_start, date_end = _get_date_range(date_mode, rolling_days, stored_from_date, stored_to_date)
    
    emails = []
    for idx, item in enumerate(items, 1):
        msg = get_message(item.get("key"))
        body_preview = ""
        if msg:
            body_preview = (msg.get("body_text") or "")[:500]
        
        emails.append({
            "index": idx,
            "key": item.get("key"),
            "provider": item.get("provider"),
            "folder": item.get("folder", "inbox"),
            "classification": item.get("classification"),
            "from": item.get("sender"),
            "subject": item.get("subject"),
            "date": item.get("date"),
            "preview": body_preview
        })
    
    if format == "json":
        return {
            "session_id": session_id,
            "filters": {
                "providers": providers,
                "folders": folders,
                "date_mode": date_mode,
                "date_range": f"{date_start.isoformat()} → {date_end.isoformat()}"
            },
            "total_emails": len(emails),
            "emails": emails
        }
    
    lines = [
        f"=== InboxPilot Session: {session_id} ===",
        f"Total: {len(emails)} emails",
        "",
        "For each email, respond with a JSON action plan:",
        '{',
        f'  "session_id":"{session_id}",',
        '  "actions":[',
        '    {',
        '      "key":"provider:uid",',
        '      "action":"send|delete|mark_read|skip",',
        '      "body":"optional reply text"',
        '    }',
        '  ]',
        '}',
        "",
        "EMAILS:",
        ""
    ]
    
    for email in emails:
        lines.append("------------------------------------")
        lines.append(f"[{email['index']}] Indice sequencial")
        lines.append(f"Provider: {email['provider']}")
        lines.append(f"Key: {email['key']}")
        lines.append(f"Classification: {email['classification']}")
        lines.append(f"From: {email['from']}")
        lines.append(f"Subject: {email['subject']}")
        lines.append(f"Date: {email['date']}")
        lines.append(f"Unread: true")
        lines.append(f"Preview: {email['preview'][:300]}...")
        lines.append("------------------------------------")
        lines.append("")
    
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="\n".join(lines), media_type="text/plain")


@router.get("/assistant/email/{key:path}")
def assistant_email(key: str, _: bool = Depends(check_api_key)):
    init_db()
    
    msg = get_message(key)
    if not msg:
        raise HTTPException(404, f"Email not found: {key}")
    
    draft = get_draft(key)
    
    return {
        "key": key,
        "provider": msg.get("provider"),
        "id": msg.get("msg_id"),
        "from": msg.get("from_addr"),
        "subject": msg.get("subject"),
        "date": msg.get("date"),
        "folder": msg.get("folder"),
        "body": msg.get("body_text") or "",
        "classification": msg.get("category"),
        "status": msg.get("status"),
        "has_draft": draft is not None,
        "draft": draft
    }


@router.post("/assistant/plan")
def assistant_plan(request: PlanRequest, _: bool = Depends(check_api_key)):
    init_db()
    
    session_id = request.session_id
    if not session_id:
        session = get_open_session()
        if session:
            session_id = session["id"]
    
    queued = 0
    errors = []
    
    valid_actions = {"send", "delete", "mark_read", "skip"}
    
    for action_item in request.actions:
        if action_item.action not in valid_actions:
            errors.append({"key": action_item.key, "error": f"Invalid action: {action_item.action}"})
            continue
        
        msg = get_message(action_item.key)
        if not msg:
            errors.append({"key": action_item.key, "error": "Email not found"})
            continue
        
        if msg.get("status") in ("sent", "deleted"):
            errors.append({"key": action_item.key, "error": f"Already processed: {msg.get('status')}"})
            continue
        
        add_queued_action(action_item.key, action_item.action, session_id, action_item.body)
        queued += 1
    
    return {
        "ok": True,
        "queued": queued,
        "errors": errors
    }


@router.post("/automation/execute")
def automation_execute(request: ExecuteRequest, _: bool = Depends(check_api_key)):
    init_db()
    
    session_id = request.session_id
    if not session_id:
        session = get_open_session()
        if session:
            session_id = session["id"]
    
    actions = get_queued_actions(session_id, request.max_actions)
    
    executed = 0
    failed = 0
    details = []
    
    send_actions = [a for a in actions if a["action"] == "send"]
    mark_read_actions = [a for a in actions if a["action"] == "mark_read"]
    delete_actions = [a for a in actions if a["action"] == "delete"]
    skip_actions = [a for a in actions if a["action"] == "skip"]
    
    for action in send_actions:
        result = _execute_action(action, request.dry_run)
        details.append(result)
        if result["status"] == "success":
            executed += 1
        else:
            failed += 1
    
    for action in mark_read_actions:
        result = _execute_action(action, request.dry_run)
        details.append(result)
        if result["status"] == "success":
            executed += 1
        else:
            failed += 1
    
    for action in delete_actions:
        result = _execute_action(action, request.dry_run)
        details.append(result)
        if result["status"] == "success":
            executed += 1
        else:
            failed += 1
    
    for action in skip_actions:
        result = _execute_action(action, request.dry_run)
        details.append(result)
        if result["status"] == "success":
            executed += 1
        else:
            failed += 1
    
    return {
        "ok": True,
        "dry_run": request.dry_run,
        "session_id": session_id,
        "executed": executed,
        "failed": failed,
        "details": details
    }


def _execute_action(action: Dict, dry_run: bool) -> Dict:
    action_id = action["id"]
    key = action["key"]
    action_type = action["action"]
    meta = json.loads(action.get("meta_json") or "{}")
    
    parts = key.split(":", 1)
    provider_name = parts[0] if len(parts) > 1 else ""
    msg_id = parts[1] if len(parts) > 1 else key
    
    result = {
        "action_id": action_id,
        "key": key,
        "action": action_type,
        "status": "success",
        "message": ""
    }
    
    if dry_run:
        result["message"] = f"DRY RUN: Would execute {action_type}"
        update_action_status(action_id, "dry_run", result["message"])
        return result
    
    try:
        if provider_name not in _providers_map:
            raise Exception(f"Provider not available: {provider_name}")
        
        provider = _providers_map[provider_name]
        
        if action_type == "send":
            body = meta.get("body")
            if not body:
                draft = get_draft(key)
                body = draft
            
            if body:
                provider.send_reply(msg_id, body)
                mark_status(key, "sent")
                log_action(key, provider_name, msg_id, "send", "success", "Email sent")
                result["message"] = "Email sent"
            else:
                raise Exception("No body or draft available")
        
        elif action_type == "mark_read":
            provider.mark_read(msg_id)
            mark_status(key, "read")
            log_action(key, provider_name, msg_id, "mark_read", "success", "Marked as read")
            result["message"] = "Marked as read"
        
        elif action_type == "delete":
            msg = get_message(key)
            current_status = msg.get("status") if msg else None
            
            if current_status == "pending_delete":
                provider.delete(msg_id)
                mark_status(key, "deleted")
                log_action(key, provider_name, msg_id, "delete", "success", "Email deleted")
                result["message"] = "Email deleted"
            else:
                mark_status(key, "pending_delete")
                log_action(key, provider_name, msg_id, "delete", "pending", "Marked for deletion (two-step)")
                result["message"] = "Marked for deletion (will delete on next execution)"
        
        elif action_type == "skip":
            mark_status(key, "skipped")
            log_action(key, provider_name, msg_id, "skip", "success", "Skipped")
            result["message"] = "Skipped"
        
        update_action_status(action_id, "done", result["message"])
        
    except Exception as e:
        result["status"] = "failed"
        result["message"] = str(e)
        update_action_status(action_id, "failed", str(e))
        log_action(key, provider_name, msg_id, action_type, "error", str(e))
    
    return result


@router.get("/automation/report")
def automation_report(
    session_id: Optional[str] = None,
    limit: int = 100,
    _: bool = Depends(check_api_key)
):
    init_db()
    
    from db import _get_conn
    conn = _get_conn()
    cursor = conn.cursor()
    
    if session_id:
        cursor.execute("""
            SELECT action, status, COUNT(*) as count 
            FROM actions 
            WHERE session_id = ?
            GROUP BY action, status
        """, (session_id,))
    else:
        cursor.execute("""
            SELECT action, status, COUNT(*) as count 
            FROM actions 
            GROUP BY action, status
        """)
    
    summary = {}
    for row in cursor.fetchall():
        action = row["action"]
        status = row["status"]
        count = row["count"]
        if action not in summary:
            summary[action] = {}
        summary[action][status] = count
    
    if session_id:
        cursor.execute("""
            SELECT * FROM actions 
            WHERE session_id = ? 
            ORDER BY ts DESC LIMIT ?
        """, (session_id, limit))
    else:
        cursor.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,))
    
    recent = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return {
        "session_id": session_id,
        "summary": summary,
        "recent_actions": recent
    }


@router.get("/setup")
def setup_status():
    status = {
        "app": "InboxPilot",
        "version": "1.1",
        "providers": {}
    }
    
    apple_email = os.getenv("APPLE_EMAIL")
    apple_pass = os.getenv("APPLE_APP_PASSWORD")
    status["providers"]["apple"] = {
        "configured": bool(apple_email and apple_pass),
        "email": apple_email[:3] + "***" if apple_email else None
    }
    
    gmail_id = os.getenv("GMAIL_CLIENT_ID") or os.getenv("CLIENT_ID")
    gmail_secret = os.getenv("GMAIL_CLIENT_SECRET") or os.getenv("CLIENT_SECRET")
    from store import get_item
    gmail_token = get_item("gmail_token")
    status["providers"]["gmail"] = {
        "configured": bool(gmail_id and gmail_secret),
        "has_token": bool(gmail_token)
    }
    
    ms_id = os.getenv("CLIENT_ID")
    ms_secret = os.getenv("CLIENT_SECRET")
    status["providers"]["microsoft"] = {
        "configured": bool(ms_id and ms_secret)
    }
    
    openai_key = os.getenv("OPENAI_API_KEY")
    status["openai"] = {
        "configured": bool(openai_key)
    }
    
    api_key = os.getenv("INBOXPILOT_API_KEY")
    status["security"] = {
        "api_key_required": bool(api_key)
    }
    
    return status


@router.get("/gmail/token-status")
def gmail_token_status(_: bool = Depends(check_api_key)):
    from store import get_gmail_token
    token = get_gmail_token()
    if token:
        return {
            "ok": True,
            "has_refresh_token": bool(token.get("refresh_token")),
            "needs_reauth": token.get("needs_reauth", False),
            "storage_source": token.get("storage_source", "unknown"),
        }
    return {"ok": False, "has_refresh_token": False}


@router.post("/gmail/token-sync")
def gmail_token_sync(_: bool = Depends(check_api_key)):
    import httpx
    deploy_url = os.environ.get("BASE_URL", "").rstrip("/")
    if not deploy_url:
        raise HTTPException(400, "BASE_URL not configured")
    
    api_key = os.getenv("INBOXPILOT_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    
    try:
        resp = httpx.get(f"{deploy_url}/gmail/token-export", headers=headers, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(502, f"Deploy returned {resp.status_code}")
        data = resp.json()
        if not data.get("ok") or not data.get("token"):
            raise HTTPException(404, "No token found on deploy")
        
        from store import set_gmail_token
        t = data["token"]
        set_gmail_token(
            access_token=t.get("access_token"),
            refresh_token=t.get("refresh_token"),
            scope=t.get("scope"),
            expiry_ts=t.get("expiry_ts"),
            client_id=t.get("client_id"),
            client_secret=t.get("client_secret"),
            needs_reauth=False,
            preserve_refresh_token=True,
        )
        return {"ok": True, "message": "Token synced from deploy"}
    except httpx.RequestError as e:
        raise HTTPException(502, f"Failed to reach deploy: {e}")


@router.get("/gmail/token-export")
def gmail_token_export(_: bool = Depends(check_api_key)):
    from store import get_gmail_token
    token = get_gmail_token()
    if not token or not token.get("refresh_token"):
        return {"ok": False, "token": None}
    safe_token = {
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "scope": token.get("scope"),
        "expiry_ts": token.get("expiry_ts"),
        "client_id": token.get("client_id"),
        "client_secret": token.get("client_secret"),
    }
    return {"ok": True, "token": safe_token}


@router.get("/ui/messages")
def ui_messages(
    providers: str = Query("apple,gmail", description="Comma-separated provider names"),
    folders: str = Query("inbox", description="Comma-separated folder names"),
    range: str = Query("today", description="today|current_week|last_n_days|custom"),
    n: int = Query(7, description="Number of days for last_n_days"),
    start: Optional[str] = Query(None, description="Start date for custom (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date for custom (YYYY-MM-DD)"),
    unread_only: int = Query(0, description="1=only unread, 0=all"),
    limit: int = Query(50, description="Max emails per provider"),
    session_id: str = Query("global", description="Session ID for snapshot"),
    _: bool = Depends(check_api_key)
):
    """
    Unified messages endpoint for dashboard, PDF export, and GPT text.
    Uses consistent timezone-aware date filtering (America/Sao_Paulo).
    
    Range options:
    - today: 00:00-23:59 local time
    - current_week: Monday 00:00 to Sunday 23:59 (calendar week)
    - last_n_days: now - n days (use n parameter)
    - custom: start to end dates (use start/end parameters)
    """
    init_db()
    from assistant_loop import classify_email as _classify
    
    date_mode = range
    if range == "last_n_days":
        rolling_days = n
    else:
        rolling_days = 7
    
    info = get_date_range_info(date_mode, rolling_days, start, end)
    date_start = info["start_utc"]
    date_end = info["end_utc"]
    
    provider_list = [p.strip() for p in providers.split(",") if p.strip()]
    folder_list = [f.strip() for f in folders.split(",") if f.strip()]
    
    import logging
    logging.info(f"UI Messages: range={range}, n={n}, date_mode={date_mode}")
    logging.info(f"UI Messages: {info['description']}")
    logging.info(f"UI Messages: UTC range {date_start.isoformat()} to {date_end.isoformat()}")
    
    items = []
    counts = {"total_available": 0, "loaded": 0, "unread": 0, "by_provider": {}}
    provider_status = {}
    
    apple_email = os.getenv("APPLE_EMAIL")
    apple_pass = os.getenv("APPLE_APP_PASSWORD")
    provider_status["apple"] = {
        "configured": bool(apple_email and apple_pass),
        "connected": bool(apple_email and apple_pass),
        "needs_reauth": False,
    }
    
    from store import get_gmail_token
    gmail_token = get_gmail_token()
    gmail_connected = bool(gmail_token and gmail_token.get("refresh_token") and not gmail_token.get("needs_reauth"))
    provider_status["gmail"] = {
        "configured": bool(os.getenv("GMAIL_CLIENT_ID") or os.getenv("CLIENT_ID")),
        "connected": gmail_connected,
        "needs_reauth": not gmail_connected,
        "has_refresh_token": bool(gmail_token and gmail_token.get("refresh_token")) if gmail_token else False,
    }
    
    def _fetch_provider(prov_name):
        ps = provider_status.get(prov_name, {})
        if not ps.get("connected", True):
            logging.info(f"Skipping {prov_name}: not connected")
            return prov_name, [], False
        
        provider = _providers_map[prov_name]
        prov_items = []
        auth_error = False
        
        for folder in folder_list:
            try:
                messages = provider.list_emails(
                    folder=folder,
                    date_start=date_start,
                    date_end=date_end,
                    limit=limit
                )
                
                for msg in messages:
                    if unread_only and not getattr(msg, 'unread', True):
                        continue
                    
                    body_text = msg.body[:2000] if msg.body else ''
                    cls = _classify(msg.from_addr or '', msg.subject or '', body_text)
                    cat = cls.get("category", "human")
                    
                    item = {
                        "key": f"{prov_name}:{folder}:{msg.id}",
                        "id": msg.id,
                        "provider": prov_name,
                        "folder": folder,
                        "from": msg.from_addr,
                        "subject": msg.subject,
                        "date": msg.date.isoformat() if hasattr(msg.date, 'isoformat') else str(msg.date),
                        "snippet": (getattr(msg, 'snippet', '') or (msg.body[:200] if msg.body else '') or msg.subject)[:200],
                        "unread": getattr(msg, 'unread', True),
                        "classification": cat
                    }
                    prov_items.append(item)
            except Exception as e:
                logging.error(f"Error fetching {prov_name}/{folder}: {e}")
                if "login" in str(e).lower() or "authenticat" in str(e).lower() or "reauth" in str(e).lower():
                    auth_error = True
        
        return prov_name, prov_items, auth_error

    eligible_providers = [p for p in provider_list if p in _providers_map]
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=len(eligible_providers) or 1) as executor:
        futures = {executor.submit(_fetch_provider, pn): pn for pn in eligible_providers}
        for future in as_completed(futures):
            try:
                prov_name, prov_items, auth_error = future.result()
                if auth_error:
                    provider_status[prov_name]["connected"] = False
                    provider_status[prov_name]["needs_reauth"] = True
                counts["by_provider"][prov_name] = len(prov_items)
                items.extend(prov_items)
            except Exception as exc:
                failed_prov = futures[future]
                logging.error(f"Provider {failed_prov} thread failed: {exc}")
                counts["by_provider"][failed_prov] = 0
    
    items.sort(key=lambda x: x["date"], reverse=True)
    
    counts["total_available"] = len(items)
    counts["unread"] = sum(1 for it in items if it.get("unread", True))
    counts["read"] = counts["total_available"] - counts["unread"]
    
    by_category = {}
    for it in items:
        cat = it.get("classification", "human")
        by_category[cat] = by_category.get(cat, 0) + 1
    counts["by_category"] = by_category
    
    if len(items) > limit:
        items = items[:limit]
    counts["loaded"] = len(items)

    if items:
        try:
            import uuid as _uuid
            snap_id = f"snap_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:6]}"
            snap_keys = [it["key"] for it in items]
            snap_payload = []
            for it in items:
                snap_payload.append({
                    "key": it["key"],
                    "provider": it.get("provider", ""),
                    "folder": it.get("folder", "inbox"),
                    "from": it.get("from", ""),
                    "subject": it.get("subject", ""),
                    "date": it.get("date", ""),
                    "snippet": it.get("snippet", "")[:200],
                    "classification": it.get("classification", ""),
                    "unread": it.get("unread", True),
                })
            snap_filters = {
                "range": range,
                "n": n,
                "start": start,
                "end": end,
                "unread_only": unread_only,
            }
            snapshot_save(snap_id, session_id, provider_list, folder_list, snap_filters, snap_keys, snap_payload)
            snapshot_cleanup(10)
            logging.info(f"Snapshot saved: {snap_id} with {len(items)} items")
        except Exception as snap_err:
            logging.warning(f"Snapshot save failed: {snap_err}")

    return {
        "items": items,
        "counts": counts,
        "provider_status": provider_status,
        "range_info": {
            "filter_type": info["filter_type"],
            "description": info["description"],
            "tz_name": info["tz_name"],
            "start_local": info["start_local_iso"],
            "end_local": info["end_local_iso"],
            "start_utc": date_start.isoformat(),
            "end_utc": date_end.isoformat()
        }
    }


@router.get("/handsfree/context")
def handsfree_context(
    session_id: str = Query("global", description="Session ID"),
    providers: str = Query("", description="Comma-separated providers to filter"),
    _: bool = Depends(check_api_key)
):
    init_db()
    snap = snapshot_get_latest(session_id)
    if not snap:
        snap = snapshot_get_latest(None)
    if not snap:
        return {"ok": False, "reason": "no_snapshot", "count": 0, "items": [],
                "message": "Nenhum snapshot encontrado. Clique em Atualizar no dashboard."}

    items = snap["payload_json"]

    if providers:
        provider_list = [p.strip().lower() for p in providers.split(",") if p.strip()]
        if provider_list:
            items = [it for it in items if it.get("provider", "").lower() in provider_list]

    import hashlib, json as _json
    context_id = hashlib.md5(_json.dumps([it.get("key","") for it in items], sort_keys=True).encode()).hexdigest()[:12]

    return {
        "ok": True,
        "snapshot_id": snap["snapshot_id"],
        "context_id": context_id,
        "count": len(items),
        "items": items,
        "filters": snap["filters"],
        "created_at": snap["created_at"],
    }


@router.get("/mailbox/stats")
async def mailbox_stats(providers: str = "apple,gmail", folders: str = "inbox"):
    provider_list = [p.strip() for p in providers.split(",") if p.strip()]
    folder_list = [f.strip() for f in folders.split(",") if f.strip()]
    
    results = {}
    
    for prov_name in provider_list:
        if prov_name not in _providers_map:
            continue
        provider = _providers_map[prov_name]
        
        prov_stats = {"total": 0, "unseen": 0, "read": 0, "folders": {}}
        
        for folder in folder_list:
            try:
                if hasattr(provider, 'get_folder_stats'):
                    stats = provider.get_folder_stats(folder)
                    prov_stats["folders"][folder] = stats
                    prov_stats["total"] += stats.get("total", 0)
                    prov_stats["unseen"] += stats.get("unseen", 0)
                    prov_stats["read"] += stats.get("read", 0)
                else:
                    prov_stats["folders"][folder] = {"total": 0, "unseen": 0, "read": 0}
            except Exception as e:
                logging.error(f"Stats error {prov_name}/{folder}: {e}")
                prov_stats["folders"][folder] = {"error": str(e)}
        
        results[prov_name] = prov_stats
    
    grand_total = sum(r.get("total", 0) for r in results.values())
    grand_unseen = sum(r.get("unseen", 0) for r in results.values())
    
    return {
        "ok": True,
        "providers": results,
        "totals": {
            "total": grand_total,
            "unseen": grand_unseen,
            "read": grand_total - grand_unseen,
        }
    }
