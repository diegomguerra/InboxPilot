# main.py
import os
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import msal
from typing import Optional

from store import get_item, set_item
from providers.apple import AppleMailProvider
from providers.microsoft import MicrosoftProvider
from providers.gmail import GmailProvider

load_dotenv()

# =========================
# Microsoft / Outlook (Graph OAuth)
# =========================
TENANT = os.getenv("TENANT_ID", "common")
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
BASE_URL = os.environ.get("BASE_URL", "")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
SCOPES = ["User.Read", "Mail.Read", "Mail.Send"]

INBOXPILOT_API_KEY = os.getenv("INBOXPILOT_API_KEY")

app = FastAPI(title="InboxPilot API", version="1.1.0")

app.mount("/static", StaticFiles(directory="static"), name="static")


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if INBOXPILOT_API_KEY:
        if not x_api_key or x_api_key != INBOXPILOT_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    return True


# =========================
# Health Check
# =========================
@app.get("/")
def root():
    return {"status": "ok", "app": "InboxPilot", "version": "1.1"}


@app.get("/health")
def health():
    return {"ok": True}


# =========================
# Providers
# =========================
def _msal_app():
    return msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
    )


def get_access_token() -> str:
    token = get_item("access_token")
    if token:
        return token
    raise HTTPException(401, "Not authenticated. Go to /login")


apple_provider = AppleMailProvider()
microsoft_provider = MicrosoftProvider(get_access_token)
gmail_provider = GmailProvider(BASE_URL)

from store import restore_gmail_token_on_boot
restore_gmail_token_on_boot()


# =========================
# Microsoft Graph Endpoints
# =========================
@app.get("/login")
def login():
    msal_app = _msal_app()
    auth_url = msal_app.get_authorization_request_url(
        SCOPES,
        redirect_uri=f"{BASE_URL}/auth/callback",
        prompt="select_account",
    )
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
def auth_callback(code: str):
    msal_app = _msal_app()
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=f"{BASE_URL}/auth/callback",
    )
    if "access_token" not in result:
        raise HTTPException(400, f"Auth failed: {result.get('error_description', result)}")

    set_item("access_token", result["access_token"])
    return JSONResponse({"ok": True, "message": "Authenticated. Call /queue/next"})


@app.get("/queue/next")
def queue_next():
    try:
        email_msg = microsoft_provider.queue_next("inbox")
        if not email_msg:
            return {"empty": True}
        result = email_msg.to_dict(include_snippet=False)
        result["receivedDateTime"] = result.pop("date")
        result["preview"] = result.pop("body")[:500]
        result.pop("folder", None)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch next email: {str(e)}")


@app.post("/emails/{message_id}/suggest-reply")
def suggest_reply(message_id: str):
    try:
        return microsoft_provider.suggest_reply(message_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Suggest reply failed: {str(e)}")


@app.post("/emails/{message_id}/send")
def send_email(message_id: str):
    try:
        return microsoft_provider.send(message_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Send failed: {str(e)}")


@app.post("/emails/{message_id}/delete")
def delete_email(message_id: str):
    try:
        return microsoft_provider.delete(message_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")


# =========================
# Apple Mail Endpoints
# =========================
@app.get("/apple/debug/status")
def apple_debug_status():
    try:
        status = apple_provider.debug_status()
        return status.to_dict()
    except Exception as e:
        raise HTTPException(500, f"Debug status failed: {str(e)}")


@app.get("/apple/queue/next")
def apple_queue_next(folder: str = "inbox"):
    try:
        email_msg = apple_provider.queue_next(folder)
        if not email_msg:
            return {"empty": True}
        return email_msg.to_dict()
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch next email: {str(e)}")


@app.post("/apple/emails/{uid}/suggest-reply")
def apple_suggest_reply(uid: str):
    try:
        return apple_provider.suggest_reply(uid)
    except Exception as e:
        raise HTTPException(500, f"Suggest reply failed: {str(e)}")


@app.post("/apple/emails/{uid}/send")
def apple_send_reply(uid: str):
    try:
        return apple_provider.send(uid)
    except Exception as e:
        raise HTTPException(500, f"Send reply failed: {str(e)}")


@app.post("/apple/emails/{uid}/mark-read")
def apple_mark_read(uid: str):
    try:
        return apple_provider.mark_read(uid)
    except Exception as e:
        raise HTTPException(500, f"Mark read failed: {str(e)}")


@app.post("/apple/emails/{uid}/delete")
def apple_delete_email(uid: str):
    try:
        return apple_provider.delete(uid)
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")


# =========================
# Gmail Endpoints
# =========================
@app.get("/gmail/login")
def gmail_login():
    try:
        auth_url = gmail_provider.get_auth_url()
        return RedirectResponse(auth_url)
    except Exception as e:
        raise HTTPException(500, f"Gmail login failed: {str(e)}")


@app.get("/gmail/auth/callback")
def gmail_auth_callback(code: str, state: str = None):
    try:
        return gmail_provider.handle_callback(code, state)
    except Exception as e:
        raise HTTPException(400, f"Gmail auth failed: {str(e)}")


@app.get("/gmail/debug/status")
@app.get("/gmail/debug/login")
def gmail_debug_status():
    try:
        status = gmail_provider.debug_status()
        return status.to_dict()
    except Exception as e:
        raise HTTPException(500, f"Debug status failed: {str(e)}")


@app.get("/gmail/queue/next")
def gmail_queue_next(folder: str = "inbox"):
    try:
        email_msg = gmail_provider.queue_next(folder)
        if not email_msg:
            return {"empty": True}
        return email_msg.to_dict()
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch next email: {str(e)}")


@app.post("/gmail/emails/{message_id}/suggest-reply")
def gmail_suggest_reply(message_id: str):
    try:
        return gmail_provider.suggest_reply(message_id)
    except Exception as e:
        raise HTTPException(500, f"Suggest reply failed: {str(e)}")


@app.post("/gmail/emails/{message_id}/send")
def gmail_send_reply(message_id: str):
    try:
        return gmail_provider.send(message_id)
    except Exception as e:
        raise HTTPException(500, f"Send reply failed: {str(e)}")


@app.post("/gmail/emails/{message_id}/mark-read")
def gmail_mark_read(message_id: str):
    try:
        return gmail_provider.mark_read(message_id)
    except Exception as e:
        raise HTTPException(500, f"Mark read failed: {str(e)}")


@app.post("/gmail/emails/{message_id}/delete")
def gmail_delete_email(message_id: str):
    try:
        return gmail_provider.delete(message_id)
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")


@app.get("/gmail/debug/message/{message_id}")
def gmail_debug_message(message_id: str):
    """Debug endpoint to check labels of a specific message"""
    try:
        return gmail_provider.get_message_labels(message_id)
    except Exception as e:
        raise HTTPException(500, f"Get message labels failed: {str(e)}")


@app.get("/gmail/debug/trash/{message_id}")
def gmail_debug_trash(message_id: str):
    """Debug endpoint to test trash operation via GET (for browser testing)"""
    try:
        return gmail_provider.delete(message_id)
    except Exception as e:
        raise HTTPException(500, f"Trash failed: {str(e)}")


@app.get("/gmail/debug/selftest")
def gmail_debug_selftest():
    """
    Self-test endpoint to verify Gmail integration works correctly.
    Tests: status check, queue fetch, trash operation, labels verification.
    """
    results = {
        "step1_status": None,
        "step2_queue_next": None,
        "step3_trash": None,
        "step4_labels": None,
        "step5_queue_after": None,
        "success": False,
        "summary": ""
    }
    
    try:
        status = gmail_provider.debug_status()
        status_dict = status.to_dict()
        results["step1_status"] = {
            "connected": status_dict.get("connected"),
            "has_refresh_token": status_dict.get("has_refresh_token"),
            "needs_reauth": status_dict.get("needs_reauth"),
            "scopes": status_dict.get("scopes")
        }
        
        if not status_dict.get("connected") and status_dict.get("has_refresh_token"):
            results["step1_status"]["note"] = "Has refresh_token, will auto-refresh on API call"
        
        if status_dict.get("needs_reauth"):
            results["summary"] = "FAILED: Needs re-authentication. Go to /gmail/login"
            return results
    except Exception as e:
        results["step1_status"] = {"error": str(e)}
        results["summary"] = f"FAILED at step 1: {e}"
        return results
    
    try:
        email1 = gmail_provider.queue_next("inbox")
        if not email1:
            results["step2_queue_next"] = {"empty": True, "note": "No unread emails in inbox"}
            results["summary"] = "PARTIAL: No unread emails to test with"
            results["success"] = True
            return results
        
        id1 = email1.id
        results["step2_queue_next"] = {"id": id1, "subject": email1.subject[:50]}
    except Exception as e:
        results["step2_queue_next"] = {"error": str(e)}
        results["summary"] = f"FAILED at step 2: {e}"
        return results
    
    try:
        trash_result = gmail_provider.delete(id1)
        results["step3_trash"] = trash_result
    except Exception as e:
        results["step3_trash"] = {"error": str(e)}
        results["summary"] = f"FAILED at step 3: {e}"
        return results
    
    try:
        labels_result = gmail_provider.get_message_labels(id1)
        results["step4_labels"] = labels_result
        
        label_ids = labels_result.get("labelIds", [])
        has_trash = "TRASH" in label_ids
        has_inbox = "INBOX" in label_ids
        
        if not has_trash or has_inbox:
            results["summary"] = f"FAILED: Email {id1} not properly trashed. Labels: {label_ids}"
            return results
    except Exception as e:
        results["step4_labels"] = {"error": str(e)}
        results["summary"] = f"FAILED at step 4: {e}"
        return results
    
    try:
        email2 = gmail_provider.queue_next("inbox")
        if email2:
            id2 = email2.id
            results["step5_queue_after"] = {"id": id2, "different": id2 != id1}
            
            if id2 == id1:
                results["summary"] = f"FAILED: Same email {id1} returned after trash"
                return results
        else:
            results["step5_queue_after"] = {"empty": True, "note": "No more unread emails"}
    except Exception as e:
        results["step5_queue_after"] = {"error": str(e)}
        results["summary"] = f"FAILED at step 5: {e}"
        return results
    
    results["success"] = True
    results["summary"] = f"SUCCESS: Email {id1} trashed and removed from inbox"
    return results


# =========================
# Unified Endpoints (Provider-Agnostic)
# =========================
def _get_provider(provider: str):
    providers_map = {
        "microsoft": microsoft_provider,
        "apple": apple_provider,
        "gmail": gmail_provider,
    }
    if provider not in providers_map:
        raise HTTPException(400, f"Unknown provider: {provider}. Use: microsoft, apple, gmail")
    return providers_map[provider]


def _provider_available(name: str) -> bool:
    try:
        if name == "microsoft":
            return bool(os.getenv("CLIENT_ID") and os.getenv("CLIENT_SECRET"))
        elif name == "apple":
            return bool(os.getenv("APPLE_EMAIL") and os.getenv("APPLE_APP_PASSWORD"))
        elif name == "gmail":
            return bool(os.getenv("GMAIL_CLIENT_ID") and os.getenv("GMAIL_CLIENT_SECRET"))
        return False
    except:
        return False


@app.get("/providers")
def list_providers():
    return {
        "providers": {
            "microsoft": _provider_available("microsoft"),
            "apple": _provider_available("apple"),
            "gmail": _provider_available("gmail"),
        }
    }


@app.get("/unified/queue/next")
def unified_queue_next(
    provider: str,
    folder: str = "inbox",
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        email_msg = prov.queue_next(folder)
        if not email_msg:
            return {"empty": True, "provider": provider, "folder": folder}
        result = email_msg.to_dict()
        result["folder"] = folder
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch next email: {str(e)}")


@app.post("/unified/emails/{provider}/{message_id}/suggest-reply")
def unified_suggest_reply(
    provider: str,
    message_id: str,
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        result = prov.suggest_reply(message_id)
        result["provider"] = provider
        result["id"] = message_id
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Suggest reply failed: {str(e)}")


@app.post("/unified/emails/{provider}/{message_id}/send")
def unified_send(
    provider: str,
    message_id: str,
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        result = prov.send(message_id)
        result["provider"] = provider
        result["id"] = message_id
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Send failed: {str(e)}")


class ComposeEmailRequest(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/compose/{provider}")
def compose_email(
    provider: str,
    request: ComposeEmailRequest,
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        if not hasattr(prov, 'compose_email'):
            raise HTTPException(400, f"Provider {provider} does not support composing emails")
        result = prov.compose_email(request.to, request.subject, request.body)
        result["provider"] = provider
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Compose failed: {str(e)}")


@app.post("/unified/emails/{provider}/{message_id}/mark-read")
def unified_mark_read(
    provider: str,
    message_id: str,
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        result = prov.mark_read(message_id)
        result["provider"] = provider
        result["id"] = message_id
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Mark read failed: {str(e)}")


@app.post("/unified/emails/{provider}/{message_id}/delete")
def unified_delete(
    provider: str,
    message_id: str,
    _: bool = Depends(check_api_key)
):
    try:
        prov = _get_provider(provider)
        result = prov.delete(message_id)
        result["provider"] = provider
        result["id"] = message_id
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")


# =========================
# Automation Endpoints
# =========================
from automation import AutomationEngine, load_policy as load_automation_policy
from db import (
    init_db, upsert_message, get_message, set_draft, get_draft,
    log_action, list_logs, mark_status, make_key, get_pending_deletes
)
from assistant_loop import (
    load_policy, classify_email, should_send_auto,
    safe_extract_text, sanitize_reply
)
from typing import List as TypingList
from datetime import datetime, timedelta

init_db()


class AutomationRequest(BaseModel):
    providers: TypingList[str] = ["gmail", "apple", "microsoft"]
    folders: TypingList[str] = ["inbox"]
    max_per_provider: int = 10
    mode: str = "dry_run"
    since_hours: int = 72


@app.post("/automation/run")
def automation_run(
    request: AutomationRequest,
    _: bool = Depends(check_api_key)
):
    try:
        providers_map = {
            "microsoft": microsoft_provider,
            "apple": apple_provider,
            "gmail": gmail_provider,
        }
        engine = AutomationEngine(providers_map)
        result = engine.run(
            provider_names=request.providers,
            folders=request.folders,
            max_per_provider=request.max_per_provider,
            mode=request.mode,
            since_hours=request.since_hours
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Automation failed: {str(e)}")


@app.get("/automation/logs")
def automation_logs(
    limit: int = 100,
    _: bool = Depends(check_api_key)
):
    try:
        logs = list_logs(limit)
        return {"logs": logs, "count": len(logs)}
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {str(e)}")


@app.get("/automation/policy")
def automation_policy(_: bool = Depends(check_api_key)):
    try:
        policy = load_policy()
        return {"policy": policy}
    except Exception as e:
        raise HTTPException(500, f"Failed to get policy: {str(e)}")


# =========================
# Assistant Endpoints (Brief / Decision Loop)
# =========================
class DecisionRequest(BaseModel):
    key: str
    decision: str
    edited_reply: Optional[str] = None


@app.get("/assistant/brief")
def assistant_brief(
    providers: str = "gmail,apple,microsoft",
    folders: str = "inbox",
    limit_per_provider: int = 5,
    since_hours: Optional[int] = None,
    _: bool = Depends(check_api_key)
):
    try:
        policy = load_policy()
        if since_hours is None:
            since_hours = policy.get("since_hours_default", 72)
        
        provider_list = [p.strip() for p in providers.split(",") if p.strip()]
        folder_list = [f.strip() for f in folders.split(",") if f.strip()]
        
        providers_map = {
            "microsoft": microsoft_provider,
            "apple": apple_provider,
            "gmail": gmail_provider,
        }
        
        items = []
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        
        for prov_name in provider_list:
            if prov_name not in providers_map:
                continue
            
            provider = providers_map[prov_name]
            
            for folder in folder_list:
                for _ in range(limit_per_provider):
                    try:
                        email_msg = provider.queue_next(folder)
                        if not email_msg:
                            break
                        
                        email_dict = email_msg.to_dict()
                        msg_id = email_dict.get("id", "")
                        key = make_key(prov_name, msg_id)
                        
                        existing = get_message(key)
                        if existing and existing.get("status") in ("sent", "deleted"):
                            continue
                        
                        from_addr = email_dict.get("from", "")
                        subject = email_dict.get("subject", "")
                        body = email_dict.get("body", "")
                        body_text = safe_extract_text(body)
                        
                        classification = classify_email(from_addr, subject, body_text, policy)
                        
                        upsert_message(
                            key=key,
                            provider=prov_name,
                            msg_id=msg_id,
                            folder=folder,
                            from_addr=from_addr,
                            subject=subject,
                            date=email_dict.get("date", ""),
                            body=body_text,
                            status="classified",
                            category=classification["category"],
                            priority=classification["priority"]
                        )
                        
                        has_draft = get_draft(key) is not None
                        
                        items.append({
                            "key": key,
                            "provider": prov_name,
                            "id": msg_id,
                            "from": from_addr,
                            "subject": subject,
                            "date": email_dict.get("date", ""),
                            "folder": folder,
                            "category": classification["category"],
                            "priority": classification["priority"],
                            "recommended_action": classification["recommended_action"],
                            "reason": classification["reason"],
                            "has_draft": has_draft
                        })
                        
                    except Exception as e:
                        log_action("", prov_name, "", "brief_fetch", "error", str(e))
                        break
        
        summary = {
            "total": len(items),
            "needs_manual": sum(1 for i in items if i["category"] in ("human",)),
            "auto_send_candidates": sum(1 for i in items if i["recommended_action"] == "suggest_reply"),
            "pending_delete": sum(1 for i in items if i["recommended_action"] == "pending_delete")
        }
        
        return {"summary": summary, "items": items}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Brief failed: {str(e)}")


@app.post("/assistant/decision")
def assistant_decision(
    request: DecisionRequest,
    _: bool = Depends(check_api_key)
):
    try:
        key = request.key
        decision = request.decision
        edited_reply = request.edited_reply
        
        if ":" not in key:
            raise HTTPException(400, "Invalid key format. Use provider:id")
        
        provider_name, msg_id = key.split(":", 1)
        
        providers_map = {
            "microsoft": microsoft_provider,
            "apple": apple_provider,
            "gmail": gmail_provider,
        }
        
        if provider_name not in providers_map:
            raise HTTPException(400, f"Unknown provider: {provider_name}")
        
        provider = providers_map[provider_name]
        policy = load_policy()
        
        existing = get_message(key)
        if existing and existing.get("status") in ("sent", "deleted"):
            return {"status": "skipped", "reason": f"Already {existing['status']}"}
        
        if decision == "send":
            draft_text = edited_reply or get_draft(key)
            if not draft_text:
                try:
                    suggestion = provider.suggest_reply(msg_id)
                    draft_text = suggestion.get("suggested_reply", "")
                except Exception as e:
                    raise HTTPException(400, f"No draft available and failed to generate: {str(e)}")
            
            draft_text = sanitize_reply(draft_text, policy)
            set_draft(key, draft_text)
            
            try:
                result = provider.send(msg_id)
                mark_status(key, "sent")
                log_action(key, provider_name, msg_id, "send", "success", "Resposta enviada")
                provider.mark_read(msg_id)
                return {"status": "sent", "result": result}
            except Exception as e:
                log_action(key, provider_name, msg_id, "send", "error", str(e))
                raise HTTPException(500, f"Send failed: {str(e)}")
        
        elif decision == "mark_read":
            try:
                result = provider.mark_read(msg_id)
                mark_status(key, "read")
                log_action(key, provider_name, msg_id, "mark_read", "success", "Marcado como lido")
                return {"status": "marked_read", "result": result}
            except Exception as e:
                log_action(key, provider_name, msg_id, "mark_read", "error", str(e))
                raise HTTPException(500, f"Mark read failed: {str(e)}")
        
        elif decision == "delete":
            delete_strategy = policy.get("delete_strategy", "two_step")
            pending_hours = policy.get("pending_delete_hours", 6)
            
            if delete_strategy == "two_step":
                current_status = existing.get("status") if existing else None
                
                if current_status != "pending_delete":
                    mark_status(key, "pending_delete")
                    log_action(key, provider_name, msg_id, "pending_delete", "success", 
                              f"Marcado para exclusão em {pending_hours}h")
                    return {"status": "pending", "message": f"Marked for deletion. Will delete after {pending_hours} hours."}
                
                updated_ts = existing.get("updated_ts", "")
                if updated_ts:
                    try:
                        updated_dt = datetime.fromisoformat(updated_ts)
                        if datetime.utcnow() < updated_dt + timedelta(hours=pending_hours):
                            remaining = (updated_dt + timedelta(hours=pending_hours) - datetime.utcnow()).total_seconds() / 3600
                            return {"status": "pending", "message": f"Waiting {remaining:.1f} more hours before deletion"}
                    except:
                        pass
            
            try:
                result = provider.delete(msg_id)
                mark_status(key, "deleted")
                log_action(key, provider_name, msg_id, "delete", "success", "Email excluído")
                return {"status": "deleted", "result": result}
            except Exception as e:
                log_action(key, provider_name, msg_id, "delete", "error", str(e))
                raise HTTPException(500, f"Delete failed: {str(e)}")
        
        elif decision == "skip":
            mark_status(key, "skipped")
            log_action(key, provider_name, msg_id, "skip", "success", "Ignorado pelo usuário")
            return {"status": "skipped"}
        
        else:
            raise HTTPException(400, f"Invalid decision: {decision}. Use: send, skip, delete, mark_read")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Decision failed: {str(e)}")


@app.get("/assistant/logs")
def assistant_logs(
    limit: int = 100,
    _: bool = Depends(check_api_key)
):
    try:
        logs = list_logs(limit)
        return {"logs": logs, "count": len(logs)}
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {str(e)}")


# =========================
# Web Dashboard & Unified Inbox API
# =========================
from inbox_api import router as inbox_router, set_providers as set_inbox_providers
from session_api import router as session_router, set_providers as set_session_providers
from export_api import router as export_router, set_providers as set_export_providers
from llm_api import router as llm_router, set_providers as set_llm_providers
from llm_worker import start_worker as start_llm_worker

providers_map = {
    "microsoft": microsoft_provider,
    "apple": apple_provider,
    "gmail": gmail_provider,
}

set_inbox_providers(providers_map)
set_session_providers(providers_map)
set_export_providers(providers_map)
set_llm_providers(providers_map)

from voice_api import router as voice_router
app.include_router(inbox_router)
app.include_router(session_router)
app.include_router(export_router)
app.include_router(llm_router)
app.include_router(voice_router)

start_llm_worker()


@app.get("/ui", response_class=HTMLResponse)
def dashboard_ui():
    with open("templates/ui.html", "r") as f:
        return f.read()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_main():
    with open("templates/ui.html", "r") as f:
        return f.read()


@app.get("/tutorial", response_class=HTMLResponse)
def tutorial_page():
    with open("templates/tutorial.html", "r") as f:
        return f.read()
