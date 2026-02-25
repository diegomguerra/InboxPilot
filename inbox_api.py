import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Header, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from db import (
    init_db, upsert_message, get_message, set_draft, get_draft,
    log_action, list_logs, mark_status, make_key, get_messages_by_status
)
from assistant_loop import load_policy, classify_email, safe_extract_text, sanitize_reply

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


def _parse_range(range_val: str) -> datetime:
    now = datetime.now(timezone.utc)
    if range_val == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_val == "week":
        return now - timedelta(days=7)
    elif range_val == "month":
        return now - timedelta(days=30)
    else:
        return now - timedelta(days=7)


class QueueAddRequest(BaseModel):
    key: str
    action: str
    reply: Optional[Dict[str, str]] = None


class QueueCommitRequest(BaseModel):
    dispatch_type: Optional[str] = None
    executed_at: Optional[str] = None
    actions: Optional[List[Dict[str, Any]]] = None


@router.get("/inbox/list")
def inbox_list(
    range_filter: str = Query("today", alias="range"),
    providers: str = "apple,gmail",
    folders: str = "inbox",
    limit_per_provider: int = 20,
    _: bool = Depends(check_api_key)
):
    provider_list = [p.strip() for p in providers.split(",") if p.strip()]
    folder_list = [f.strip() for f in folders.split(",") if f.strip()]
    
    if not provider_list:
        provider_list = ["apple", "gmail"]
    if not folder_list:
        folder_list = ["inbox"]
    
    cutoff = _parse_range(range_filter)
    cutoff_str = cutoff.isoformat()
    
    init_db()
    from db import _get_conn
    conn = _get_conn()
    cursor = conn.cursor()
    
    provider_placeholders = ",".join("?" * len(provider_list))
    folder_placeholders = ",".join("?" * len(folder_list))
    
    query = f"""
        SELECT * FROM messages 
        WHERE provider IN ({provider_placeholders})
        AND folder IN ({folder_placeholders})
        AND status NOT IN ('sent', 'deleted')
        AND created_ts >= ?
        ORDER BY created_ts DESC
        LIMIT ?
    """
    
    params = provider_list + folder_list + [cutoff_str, limit_per_provider * len(provider_list)]
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    items = []
    for row in rows:
        row_dict = dict(row)
        key = row_dict["key"]
        has_draft = get_draft(key) is not None
        
        body_text = row_dict.get("body_text") or ""
        snippet = body_text[:160] if body_text else (row_dict["subject"] or "")[:160]
        
        items.append({
            "key": key,
            "provider": row_dict["provider"],
            "folder": row_dict["folder"] or "inbox",
            "from": row_dict["from_addr"] or "",
            "subject": row_dict["subject"] or "",
            "date": row_dict["date"] or "",
            "snippet": snippet,
            "classification": row_dict["category"] or "human",
            "has_draft": has_draft,
            "status": row_dict["status"] or "new"
        })
    
    return {
        "range": range_filter,
        "total": len(items),
        "items": items,
        "note": "Run POST /automation/run first to fetch new emails from providers"
    }


@router.get("/inbox/message/{key:path}")
def inbox_message(
    key: str,
    _: bool = Depends(check_api_key)
):
    if ":" not in key:
        raise HTTPException(400, "Invalid key format. Use provider:id")
    
    provider_name, msg_id = key.split(":", 1)
    
    if provider_name not in _providers_map:
        raise HTTPException(400, f"Unknown provider: {provider_name}")
    
    provider = _providers_map[provider_name]
    
    existing = get_message(key)
    if existing:
        has_draft = get_draft(key) is not None
        draft_text = get_draft(key) if has_draft else None
        body_text = existing.get("body_text") or existing.get("subject", "")
        return {
            "key": key,
            "provider": provider_name,
            "id": msg_id,
            "from": existing.get("from_addr", ""),
            "subject": existing.get("subject", ""),
            "date": existing.get("date", ""),
            "folder": existing.get("folder", "inbox"),
            "body": body_text,
            "status": existing.get("status", "new"),
            "classification": existing.get("category", ""),
            "has_draft": has_draft,
            "draft": draft_text
        }
    
    try:
        email_msg = provider.get_message(msg_id)
        if not email_msg:
            raise HTTPException(404, "Message not found")
        
        email_dict = email_msg.to_dict()
        body_text = safe_extract_text(email_dict.get("body", ""))
        
        policy = load_policy()
        classification = classify_email(
            email_dict.get("from", ""),
            email_dict.get("subject", ""),
            body_text,
            policy
        )
        
        upsert_message(
            key=key,
            provider=provider_name,
            msg_id=msg_id,
            folder=email_dict.get("folder", "inbox"),
            from_addr=email_dict.get("from", ""),
            subject=email_dict.get("subject", ""),
            date=email_dict.get("date", ""),
            body=body_text,
            status="classified",
            category=classification["category"],
            priority=classification["priority"]
        )
        
        has_draft = get_draft(key) is not None
        
        return {
            "key": key,
            "provider": provider_name,
            "id": msg_id,
            "from": email_dict.get("from", ""),
            "subject": email_dict.get("subject", ""),
            "date": email_dict.get("date", ""),
            "folder": email_dict.get("folder", "inbox"),
            "body": body_text,
            "status": "classified",
            "classification": classification["category"],
            "has_draft": has_draft,
            "draft": None
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch message: {str(e)}")


@router.post("/inbox/message/{key:path}/suggest-reply")
def inbox_suggest_reply(
    key: str,
    _: bool = Depends(check_api_key)
):
    if ":" not in key:
        raise HTTPException(400, "Invalid key format. Use provider:id")
    
    provider_name, msg_id = key.split(":", 1)
    
    if provider_name not in _providers_map:
        raise HTTPException(400, f"Unknown provider: {provider_name}")
    
    provider = _providers_map[provider_name]
    
    existing_draft = get_draft(key)
    if existing_draft:
        return {"key": key, "draft": existing_draft, "cached": True}
    
    try:
        result = provider.suggest_reply(msg_id)
        draft_text = result.get("suggested_reply", "")
        
        if draft_text:
            policy = load_policy()
            draft_text = sanitize_reply(draft_text, policy)
            set_draft(key, draft_text)
        
        return {"key": key, "draft": draft_text, "cached": False}
    except Exception as e:
        raise HTTPException(500, f"Failed to generate reply: {str(e)}")


@router.post("/queue/add")
def queue_add(
    request: QueueAddRequest,
    _: bool = Depends(check_api_key)
):
    key = request.key
    action = request.action
    
    if action not in ("send", "delete", "mark_read", "skip"):
        raise HTTPException(400, f"Invalid action: {action}. Use: send, delete, mark_read, skip")
    
    if ":" not in key:
        raise HTTPException(400, "Invalid key format. Use provider:id")
    
    provider_name, msg_id = key.split(":", 1)
    
    existing = get_message(key)
    if existing and existing.get("status") in ("sent", "deleted"):
        return {"status": "skipped", "reason": f"Already {existing['status']}"}
    
    if request.reply and action == "send":
        reply_body = request.reply.get("body", "")
        if reply_body:
            set_draft(key, reply_body)
    
    mark_status(key, f"pending_{action}")
    log_action(key, provider_name, msg_id, f"queue_add", "pending", f"Queued for {action}")
    
    return {"status": "queued", "key": key, "action": action}


@router.get("/queue")
def queue_list(_: bool = Depends(check_api_key)):
    init_db()
    from db import _get_conn
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM messages 
        WHERE status LIKE 'pending_%'
        ORDER BY created_ts DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    
    items = []
    by_provider = {}
    
    for row in rows:
        row_dict = dict(row)
        key = row_dict["key"]
        provider = row_dict["provider"]
        status = row_dict["status"]
        action = status.replace("pending_", "") if status.startswith("pending_") else status
        
        item = {
            "key": key,
            "provider": provider,
            "id": row_dict["msg_id"],
            "from": row_dict["from_addr"],
            "subject": row_dict["subject"],
            "action": action,
            "has_draft": get_draft(key) is not None
        }
        items.append(item)
        
        if provider not in by_provider:
            by_provider[provider] = []
        by_provider[provider].append(item)
    
    return {
        "total": len(items),
        "by_provider": by_provider,
        "items": items
    }


@router.post("/queue/commit")
def queue_commit(
    request: QueueCommitRequest = None,
    _: bool = Depends(check_api_key)
):
    actions_to_execute = []
    
    if request and request.actions:
        actions_to_execute = request.actions
    else:
        init_db()
        from db import _get_conn
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM messages 
            WHERE status LIKE 'pending_%'
            ORDER BY created_ts ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        
        for row in rows:
            row_dict = dict(row)
            status = row_dict["status"]
            action = status.replace("pending_", "") if status.startswith("pending_") else "skip"
            draft = get_draft(row_dict["key"])
            
            action_item = {
                "key": row_dict["key"],
                "provider": row_dict["provider"],
                "action": action
            }
            if action == "send" and draft:
                action_item["reply"] = {"body": draft}
            
            actions_to_execute.append(action_item)
    
    results = {
        "status": "completed",
        "summary": {
            "total": len(actions_to_execute),
            "sent": 0,
            "deleted": 0,
            "marked_read": 0,
            "skipped": 0,
            "errors": 0
        },
        "details": []
    }
    
    for action_item in actions_to_execute:
        key = action_item.get("key", "")
        action = action_item.get("action", "skip")
        provider_name = action_item.get("provider", "")
        
        if ":" in key and not provider_name:
            provider_name = key.split(":")[0]
        
        msg_id = key.split(":", 1)[1] if ":" in key else key
        
        if provider_name not in _providers_map:
            results["details"].append({
                "key": key,
                "action": action,
                "status": "error",
                "message": f"Unknown provider: {provider_name}"
            })
            results["summary"]["errors"] += 1
            continue
        
        provider = _providers_map[provider_name]
        
        try:
            if action == "send":
                reply_data = action_item.get("reply", {})
                reply_body = reply_data.get("body") if reply_data else get_draft(key)
                
                if reply_body:
                    set_draft(key, reply_body)
                
                result = provider.send(msg_id)
                provider.mark_read(msg_id)
                mark_status(key, "sent")
                log_action(key, provider_name, msg_id, "send", "success", "Email enviado via commit")
                results["summary"]["sent"] += 1
                results["details"].append({
                    "key": key,
                    "action": "send",
                    "status": "ok",
                    "message": "Sent successfully"
                })
                
            elif action == "delete":
                result = provider.delete(msg_id)
                mark_status(key, "deleted")
                log_action(key, provider_name, msg_id, "delete", "success", "Email excluído via commit")
                results["summary"]["deleted"] += 1
                results["details"].append({
                    "key": key,
                    "action": "delete",
                    "status": "ok",
                    "message": "Deleted successfully"
                })
                
            elif action == "mark_read":
                result = provider.mark_read(msg_id)
                mark_status(key, "read")
                log_action(key, provider_name, msg_id, "mark_read", "success", "Marcado como lido via commit")
                results["summary"]["marked_read"] += 1
                results["details"].append({
                    "key": key,
                    "action": "mark_read",
                    "status": "ok",
                    "message": "Marked as read"
                })
                
            elif action == "skip":
                mark_status(key, "skipped")
                log_action(key, provider_name, msg_id, "skip", "success", "Ignorado via commit")
                results["summary"]["skipped"] += 1
                results["details"].append({
                    "key": key,
                    "action": "skip",
                    "status": "ok",
                    "message": "Skipped"
                })
                
        except Exception as e:
            log_action(key, provider_name, msg_id, action, "error", str(e))
            results["summary"]["errors"] += 1
            results["details"].append({
                "key": key,
                "action": action,
                "status": "error",
                "message": str(e)
            })
    
    return results


CHATGPT_CONTEXT = """CONTEXTO — INBOXPILOT EMAIL ASSISTANT
Você é meu assistente de e-mails.
Estou usando o InboxPilot, que unifica Apple Mail e Gmail.
Fluxo esperado:
1) Você lê os emails numerados que eu colar.
2) Quando eu disser "Email X", você diz remetente, assunto e lê/ resume corpo.
3) Você sugere uma ação: Apagar / Marcar como lido / Sugerir resposta / Ignorar.
4) Se eu pedir "Sugerir resposta", você propõe um texto curto, claro e profissional.
5) Eu aprovo, ajusto ou rejeito.
6) Você guarda a decisão em uma fila.
7) Ao final, quando eu disser "Despachar", você gera um JSON de despacho por email.
Não execute nada por conta própria. Aja email por email."""


@router.get("/export/chatgpt", response_class=PlainTextResponse)
def export_chatgpt(
    range_filter: str = Query("today", alias="range"),
    providers: str = "apple,gmail",
    folders: str = "inbox",
    limit_per_provider: int = 20,
    _: bool = Depends(check_api_key)
):
    inbox_data = inbox_list(range_filter, providers, folders, limit_per_provider, True)
    items = inbox_data.get("items", [])
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    output = [CHATGPT_CONTEXT]
    output.append(f"\nEMAILS — {range_filter} — {now}")
    output.append("=" * 50)
    
    for i, item in enumerate(items, 1):
        key = item.get("key", "")
        msg = get_message(key)
        body = ""
        if msg:
            from db import _get_conn
            conn = _get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT body_hash FROM messages WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
        
        output.append(f"\nEmail {i}")
        output.append(f"Key: {item.get('key', '')}")
        output.append(f"Provider: {item.get('provider', '')}")
        output.append(f"Folder: {item.get('folder', '')}")
        output.append(f"From: {item.get('from', '')}")
        output.append(f"Subject: {item.get('subject', '')}")
        output.append(f"Date: {item.get('date', '')}")
        output.append(f"Classification: {item.get('classification', '')}")
        output.append(f"Body:\n{item.get('snippet', '')[:500]}")
        output.append("-" * 40)
    
    return "\n".join(output)


@router.get("/export/dispatch.json")
def export_dispatch(_: bool = Depends(check_api_key)):
    queue_data = queue_list(True)
    items = queue_data.get("items", [])
    
    actions = []
    for item in items:
        action_item = {
            "key": item["key"],
            "provider": item["provider"],
            "action": item["action"]
        }
        if item["action"] == "send" and item.get("has_draft"):
            draft = get_draft(item["key"])
            if draft:
                action_item["reply"] = {
                    "subject": "Re: ...",
                    "body": draft
                }
        actions.append(action_item)
    
    return {
        "dispatch_type": "inboxpilot_batch",
        "executed_at": datetime.utcnow().isoformat() + "Z",
        "actions": actions
    }
