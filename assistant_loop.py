import json
import os
import re
from typing import Dict, Any, Optional
from html import unescape

POLICY_PATH = "policy.json"

_policy_cache = None
_policy_mtime = 0


def load_policy() -> dict:
    global _policy_cache, _policy_mtime
    if os.path.exists(POLICY_PATH):
        mtime = os.path.getmtime(POLICY_PATH)
        if _policy_cache is not None and mtime == _policy_mtime:
            return _policy_cache
        with open(POLICY_PATH, "r", encoding="utf-8") as f:
            _policy_cache = json.load(f)
        _policy_mtime = mtime
        return _policy_cache
    return {
        "allow_auto_reply_domains": [],
        "block_reply_domains": ["no-reply", "noreply"],
        "never_reply_keywords": ["código", "otp", "senha", "verification", "code"],
        "newsletter_keywords": ["unsubscribe", "newsletter"],
        "require_manual_categories": ["financeiro", "juridico"],
        "max_send_per_run": 3,
        "since_hours_default": 72,
        "delete_strategy": "two_step",
        "pending_delete_hours": 6,
        "default_signature": "— Diego"
    }


def safe_extract_text(body: str) -> str:
    if not body:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()[:5000]


def sanitize_reply(text: str, policy: dict = None) -> str:
    if not text:
        return ""
    policy = policy or load_policy()
    signature = policy.get("default_signature", "— Diego")
    
    text = text.strip()
    if signature and signature not in text:
        text = f"{text}\n\n{signature}"
    
    return text


def _matches_keywords(text: str, keywords: list) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _is_blocked_sender(from_addr: str, policy: dict) -> bool:
    from_lower = from_addr.lower()
    for domain in policy.get("block_reply_domains", []):
        if domain.lower() in from_lower:
            return True
    return False


def classify_email(from_addr: str, subject: str, body_text: str, policy: dict = None) -> Dict[str, Any]:
    policy = policy or load_policy()
    
    never_reply_kw = policy.get("never_reply_keywords", [])
    newsletter_kw = policy.get("newsletter_keywords", []) or policy.get("safe_newsletter_keywords", [])
    
    check_text = f"{subject} {body_text[:1000]}"
    
    if _matches_keywords(check_text, never_reply_kw):
        return {
            "category": "otp",
            "priority": "low",
            "recommended_action": "mark_read",
            "reason": "Email contém código/OTP/2FA - nunca responder automaticamente"
        }
    
    if _is_blocked_sender(from_addr, policy):
        return {
            "category": "automated",
            "priority": "low",
            "recommended_action": "mark_read",
            "reason": "Remetente no-reply/automático - não requer resposta"
        }
    
    if _matches_keywords(body_text[:2000], newsletter_kw):
        return {
            "category": "newsletter",
            "priority": "low",
            "recommended_action": "pending_delete",
            "reason": "Newsletter detectada - candidato a exclusão"
        }
    
    return {
        "category": "human",
        "priority": "medium",
        "recommended_action": "suggest_reply",
        "reason": "Email de pessoa - sugerir resposta"
    }


def should_send_auto(classification: dict, policy: dict = None) -> bool:
    policy = policy or load_policy()
    
    if classification.get("category") in ("otp", "automated"):
        return False
    
    if classification.get("category") == "newsletter":
        return False
    
    require_manual = policy.get("require_manual_categories", [])
    if classification.get("category") in require_manual:
        return False
    
    if classification.get("recommended_action") != "suggest_reply":
        return False
    
    return classification.get("category") == "human"
