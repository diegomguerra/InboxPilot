import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict

from db import (
    init_db, log_action as db_log_action, list_logs,
    upsert_message, get_message, mark_status, make_key, set_draft
)

POLICY_PATH = "policy.json"


def load_policy() -> dict:
    if os.path.exists(POLICY_PATH):
        with open(POLICY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "allow_auto_reply_domains": [],
        "block_reply_domains": ["no-reply", "noreply"],
        "never_reply_keywords": ["código", "otp", "senha", "verification"],
        "auto_delete_senders": [],
        "safe_newsletter_keywords": ["unsubscribe", "newsletter"],
        "require_manual_categories": ["financeiro", "jurídico"],
        "max_send_per_run": 3,
        "since_hours_default": 72,
        "delete_strategy": "two_step",
        "pending_delete_hours": 6,
        "default_signature": "— Diego"
    }


def log_action(provider: str, msg_id: str, action: str, status: str, reason: str = "", meta: dict = None):
    key = make_key(provider, msg_id) if msg_id else ""
    db_log_action(key, provider, msg_id, action, status, reason, meta)


def get_logs(limit: int = 100) -> List[dict]:
    return list_logs(limit)


@dataclass
class ProcessedEmail:
    provider: str
    id: str
    from_addr: str
    subject: str
    category: str
    priority: str
    recommended_action: str
    reason: str
    draft_reply: Optional[str] = None


@dataclass
class ExecutedAction:
    provider: str
    id: str
    action: str
    status: str
    reason: str


@dataclass
class SkippedEmail:
    provider: str
    id: str
    reason: str


class AutomationEngine:
    def __init__(self, providers_map: dict):
        self.providers = providers_map
        self.policy = load_policy()

    def _matches_keywords(self, text: str, keywords: List[str]) -> bool:
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in keywords)

    def _is_blocked_sender(self, from_addr: str) -> bool:
        from_lower = from_addr.lower()
        for domain in self.policy.get("block_reply_domains", []):
            if domain.lower() in from_lower:
                return True
        return False

    def _is_otp_email(self, subject: str, body: str) -> bool:
        keywords = self.policy.get("never_reply_keywords", [])
        return self._matches_keywords(subject, keywords) or self._matches_keywords(body[:500], keywords)

    def _is_newsletter(self, subject: str, body: str) -> bool:
        keywords = self.policy.get("safe_newsletter_keywords", [])
        return self._matches_keywords(body[:1000], keywords)

    def classify_email(self, email: dict) -> tuple:
        from_addr = email.get("from", "")
        subject = email.get("subject", "")
        body = email.get("body", "")

        if self._is_otp_email(subject, body):
            return "otp", "baixa", "mark_read", "Email contém código/OTP - nunca responder"

        if self._is_blocked_sender(from_addr):
            return "automated", "baixa", "mark_read", "Remetente no-reply - não requer resposta"

        if self._is_newsletter(subject, body):
            return "newsletter", "baixa", "delete_candidate", "Newsletter detectada - candidato a exclusão"

        return "human", "média", "suggest_reply", "Email de pessoa - sugerir resposta"

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(date_str)
        except:
            try:
                return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except:
                return None

    def _is_too_old(self, email_date: str, cutoff: datetime) -> bool:
        parsed = self._parse_date(email_date)
        if not parsed:
            return False
        if parsed.tzinfo:
            from datetime import timezone
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        return parsed < cutoff

    def fetch_emails(self, provider_name: str, folder: str, max_count: int, since_hours: int) -> List[dict]:
        if provider_name not in self.providers:
            return []

        provider = self.providers[provider_name]
        emails = []
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)

        for _ in range(max_count):
            try:
                email_msg = provider.queue_next(folder)
                if not email_msg:
                    break
                email_dict = email_msg.to_dict()
                
                if self._is_too_old(email_dict.get("date", ""), cutoff):
                    log_action(provider_name, email_dict.get("id", "unknown"), "skip", "skipped", 
                              f"Email mais antigo que {since_hours}h")
                    continue
                
                email_dict["_fetched"] = True
                emails.append(email_dict)
            except Exception as e:
                log_action(provider_name, "unknown", "fetch", "error", str(e))
                break

        return emails

    def run(
        self,
        provider_names: List[str],
        folders: List[str],
        max_per_provider: int,
        mode: str,
        since_hours: int
    ) -> dict:
        processed = []
        executed = []
        skipped = []
        send_count = 0
        max_send = self.policy.get("max_send_per_run", 3)

        for provider_name in provider_names:
            if provider_name not in self.providers:
                skipped.append(SkippedEmail(provider_name, "n/a", f"Provider {provider_name} não disponível"))
                continue

            provider = self.providers[provider_name]

            for folder in folders:
                emails = self.fetch_emails(provider_name, folder, max_per_provider, since_hours)

                for email in emails:
                    email_id = email.get("id", "unknown")
                    from_addr = email.get("from", "")
                    subject = email.get("subject", "")
                    body = email.get("body", "")
                    
                    key = make_key(provider_name, email_id)
                    existing = get_message(key)
                    if existing and existing.get("status") in ("sent", "deleted"):
                        skipped.append(SkippedEmail(provider_name, email_id, f"Já processado: {existing['status']}"))
                        continue

                    category, priority, action, reason = self.classify_email(email)
                    
                    upsert_message(
                        key=key,
                        provider=provider_name,
                        msg_id=email_id,
                        folder=folder,
                        from_addr=from_addr,
                        subject=subject,
                        date=email.get("date", ""),
                        body=body,
                        status="classified",
                        category=category,
                        priority=priority
                    )

                    proc = ProcessedEmail(
                        provider=provider_name,
                        id=email_id,
                        from_addr=from_addr,
                        subject=subject,
                        category=category,
                        priority=priority,
                        recommended_action=action,
                        reason=reason
                    )

                    if action == "suggest_reply" and send_count < max_send:
                        try:
                            suggestion = provider.suggest_reply(email_id)
                            if suggestion.get("skip"):
                                proc.recommended_action = "skip"
                                proc.reason = suggestion.get("reason", "Filtrado pelo provider")
                            else:
                                proc.draft_reply = suggestion.get("suggested_reply", "")
                                if proc.draft_reply:
                                    set_draft(key, proc.draft_reply)
                        except Exception as e:
                            proc.draft_reply = f"Erro ao gerar sugestão: {str(e)}"

                    processed.append(asdict(proc))

                    if mode == "dry_run":
                        log_action(provider_name, email_id, action, "dry_run", reason)
                        continue

                    if action == "mark_read":
                        if mode in ("send_only", "full"):
                            try:
                                provider.mark_read(email_id)
                                mark_status(key, "read")
                                executed.append(asdict(ExecutedAction(
                                    provider_name, email_id, "mark_read", "success", reason
                                )))
                                log_action(provider_name, email_id, "mark_read", "success", reason)
                            except Exception as e:
                                executed.append(asdict(ExecutedAction(
                                    provider_name, email_id, "mark_read", "error", str(e)
                                )))
                                log_action(provider_name, email_id, "mark_read", "error", str(e))

                    elif action == "suggest_reply" and proc.draft_reply and send_count < max_send:
                        if mode in ("send_only", "full"):
                            try:
                                provider.send(email_id)
                                send_count += 1
                                mark_status(key, "sent")
                                executed.append(asdict(ExecutedAction(
                                    provider_name, email_id, "send", "success", "Resposta enviada"
                                )))
                                log_action(provider_name, email_id, "send", "success", "Resposta enviada")
                                provider.mark_read(email_id)
                            except Exception as e:
                                executed.append(asdict(ExecutedAction(
                                    provider_name, email_id, "send", "error", str(e)
                                )))
                                log_action(provider_name, email_id, "send", "error", str(e))

                    elif action == "delete_candidate":
                        if mode == "full" and self.policy.get("delete_strategy") == "two_step":
                            try:
                                provider.mark_read(email_id)
                                mark_status(key, "pending_delete")
                                executed.append(asdict(ExecutedAction(
                                    provider_name, email_id, "pending_delete", "success",
                                    "Marcado como lido, pendente exclusão"
                                )))
                                log_action(provider_name, email_id, "pending_delete", "success",
                                          "Marcado como lido, pendente exclusão")
                            except Exception as e:
                                log_action(provider_name, email_id, "pending_delete", "error", str(e))

        return {
            "mode": mode,
            "processed": processed,
            "executed": executed,
            "skipped": [asdict(s) if isinstance(s, SkippedEmail) else s for s in skipped]
        }
