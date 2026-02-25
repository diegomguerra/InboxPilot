import os
import time
import threading
import logging
from datetime import datetime

from db import (
    init_db, job_claim_next, job_update, job_get,
    get_message, rate_limit_check,
)
from llm_client import call_llm, call_llm_multi, parse_json_response, LLM_MAX_INPUT_CHARS
from utils.text import clean_text, truncate_text, build_email_llm_context

logger = logging.getLogger("llm_worker")

LLM_QUEUE_POLL_S = float(os.getenv("LLM_QUEUE_POLL_MS", "1000")) / 1000.0
LLM_RETRY_BASE_S = float(os.getenv("LLM_RETRY_BASE_MS", "2500")) / 1000.0
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RATE_LIMIT_RPM = int(os.getenv("LLM_RATE_LIMIT_PER_MINUTE", "20"))
LLM_MIN_INTERVAL = int(os.getenv("LLM_MIN_SECONDS_BETWEEN_CALLS", "3"))

_worker_thread = None
_worker_running = False
_worker_last_heartbeat = 0


def _get_email_data_safe(key: str) -> dict:
    msg = get_message(key)
    if msg:
        return msg
    return {"key": key, "from_addr": "", "subject": "", "body_text": "", "date": "", "category": "human"}


def _process_suggest_reply(job: dict) -> dict:
    payload = job.get("payload", {})
    key = payload.get("key", "")
    tone = payload.get("tone", "neutral")
    language = payload.get("language", "pt")

    email_data = _get_email_data_safe(key)
    context = build_email_llm_context(email_data, LLM_MAX_INPUT_CHARS)

    tone_map = {
        "neutral": "tom neutro e profissional",
        "formal": "tom formal e respeitoso",
        "short": "tom direto e curto (maximo 3 frases)",
        "friendly": "tom amigavel e cordial",
    }
    tone_instruction = tone_map.get(tone, tone_map["neutral"])
    lang_map = {"pt": "portugues brasileiro", "en": "English"}
    lang_instruction = lang_map.get(language, lang_map["pt"])

    system_prompt = f"""Voce e um assistente de email executivo. Gere uma resposta profissional.
Regras:
- Responda em {lang_instruction}, com {tone_instruction}
- Nao invente fatos. Se faltar informacao, pergunte ao remetente.
- Seja objetivo e conciso
- Assinatura opcional: "Diego"
- Retorne JSON valido com os campos: classification, suggested_action, draft_body, notes
- classification: human|newsletter|otp|automated|no-reply
- suggested_action: send|skip|mark_read|delete
- draft_body: texto da resposta sugerida
- notes: lista de observacoes curtas (array de strings)"""

    user_prompt = f"""Analise o email abaixo e gere uma resposta adequada:

{context}

Retorne APENAS o JSON com: classification, suggested_action, draft_body, notes"""

    result = call_llm(
        system=system_prompt,
        user=user_prompt,
        action="suggest_reply",
        email_key=key,
        session_id=job.get("session_id", ""),
        json_mode=True,
    )

    if not result["ok"]:
        raise LLMCallError(result.get("reason", "unknown"), _classify_error(result.get("reason", "")))

    parsed = parse_json_response(result["text"])
    if parsed:
        return {
            "ok": True,
            "key": key,
            "classification": parsed.get("classification", "human"),
            "suggested_action": parsed.get("suggested_action", "send"),
            "draft_body": parsed.get("draft_body", ""),
            "notes": parsed.get("notes", []),
            "cached": result.get("cached", False),
        }
    return {
        "ok": True,
        "key": key,
        "classification": "human",
        "suggested_action": "send",
        "draft_body": result["text"],
        "notes": ["Parse JSON falhou, texto direto"],
        "cached": result.get("cached", False),
    }


def _process_triage(job: dict) -> dict:
    payload = job.get("payload", {})
    keys = payload.get("keys", [])[:10]
    language = payload.get("language", "pt")

    email_items = []
    for key in keys:
        email_data = _get_email_data_safe(key)
        from_addr = email_data.get("from_addr", email_data.get("from", ""))
        subject = email_data.get("subject", "") or "(sem assunto)"
        date = email_data.get("date", "")
        body = email_data.get("body_text", email_data.get("body", ""))
        body_clean = clean_text(body)
        body_short = truncate_text(body_clean, 1200)
        email_items.append({
            "key": key, "from": from_addr, "subject": subject,
            "date": date, "body_preview": body_short
        })

    if not email_items:
        return {"ok": True, "items": [], "total": 0}

    lang_map = {"pt": "portugues brasileiro", "en": "English"}
    lang_instruction = lang_map.get(language, lang_map["pt"])

    system_prompt = f"""Voce e um assistente de triagem de emails. Analise cada email e retorne um JSON.
Regras:
- Responda em {lang_instruction}
- Para cada email, retorne: key, summary (max 2 frases), suggested_action (reply|delete|skip|mark_read), priority (low|med|high)
- Retorne JSON valido: {{ "items": [...] }}"""

    emails_text = ""
    for item in email_items:
        emails_text += f"\n---\nKEY: {item['key']}\nDe: {item['from']}\nAssunto: {item['subject']}\nData: {item['date']}\nCorpo:\n{item['body_preview']}\n"

    user_prompt = f"""Faca triagem dos seguintes emails:
{emails_text}

Retorne APENAS o JSON com: {{ "items": [{{ "key": "...", "summary": "...", "suggested_action": "...", "priority": "..." }}] }}"""

    result = call_llm(
        system=system_prompt,
        user=user_prompt,
        action="triage",
        email_key=",".join(keys),
        session_id=job.get("session_id", ""),
        json_mode=True,
    )

    if not result["ok"]:
        raise LLMCallError(result.get("reason", "unknown"), _classify_error(result.get("reason", "")))

    parsed = parse_json_response(result["text"])
    items = []
    if parsed:
        raw = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
        if isinstance(raw, list):
            for item in raw:
                items.append({
                    "key": item.get("key", ""),
                    "summary": item.get("summary", "Sem resumo"),
                    "suggested_action": item.get("suggested_action", "skip"),
                    "priority": item.get("priority", "med"),
                })

    return {"ok": True, "items": items, "total": len(items)}


def _process_chat(job: dict) -> dict:
    payload = job.get("payload", {})
    message = payload.get("message", "")
    visible_keys = payload.get("visible_keys", [])
    session_id = job.get("session_id", "")

    from db import add_chat_message, get_chat_history

    email_context = ""
    if visible_keys:
        snippets = []
        for key in visible_keys[:10]:
            try:
                email_data = _get_email_data_safe(key)
                from_addr = email_data.get("from_addr", email_data.get("from", ""))
                subject = email_data.get("subject", "")
                body = email_data.get("body_text", email_data.get("body", ""))
                body_clean = clean_text(body)
                body_short = truncate_text(body_clean, 800)
                snippets.append(f"KEY: {key}\nDe: {from_addr}\nAssunto: {subject}\nCorpo:\n{body_short}")
            except Exception:
                continue
        if snippets:
            email_context = "\n\nEmails visiveis:\n---\n" + "\n---\n".join(snippets)

    history = get_chat_history(session_id, limit=10)

    base_prompt = """Voce e o assistente de email InboxPilot. Ajude o usuario a gerenciar emails.
Regras:
- Responda em portugues brasileiro, de forma direta e objetiva
- Nao invente fatos. Se nao souber, diga que nao sabe.
- Quando sugerir acoes, retorne JSON com campo "proposed_actions" como array de objetos com: key, action (send|delete|mark_read|skip), body (opcional)
- Acoes validas: send (enviar resposta), delete (deletar), mark_read (marcar lido), skip (ignorar)
- NUNCA execute acoes. Apenas proponha e espere aprovacao.
- Se o usuario pedir para responder um email, gere o texto e proponha como acao send com body."""

    system_prompt = base_prompt + email_context
    if len(system_prompt) > LLM_MAX_INPUT_CHARS:
        system_prompt = system_prompt[:LLM_MAX_INPUT_CHARS]

    messages = [{"role": "system", "content": system_prompt}]
    remaining_budget = LLM_MAX_INPUT_CHARS - len(system_prompt)
    for msg in history[-8:]:
        content = msg["content"]
        if len(content) > remaining_budget:
            break
        messages.append({"role": msg["role"], "content": content})
        remaining_budget -= len(content)
    messages.append({"role": "user", "content": truncate_text(message, min(remaining_budget, 2000))})

    result = call_llm_multi(
        messages=messages,
        action="chat",
        session_id=session_id,
        max_tokens=500,
    )

    if not result["ok"]:
        raise LLMCallError(result.get("reason", "unknown"), _classify_error(result.get("reason", "")))

    answer_text = result["text"]
    add_chat_message(session_id, "assistant", answer_text)

    import re
    proposed_actions = []
    parsed = parse_json_response(answer_text)
    if parsed and isinstance(parsed, dict):
        proposed_actions = parsed.get("proposed_actions", [])
        if not proposed_actions and "action" in parsed and "key" in parsed:
            proposed_actions = [parsed]

    clean_answer = answer_text
    if proposed_actions:
        clean_answer = re.sub(r'```json\s*[\s\S]*?```', '', answer_text).strip()
        clean_answer = re.sub(r'\{[\s\S]*"proposed_actions"[\s\S]*\}', '', clean_answer).strip()
        if not clean_answer:
            clean_answer = "Acoes propostas abaixo:"

    return {
        "ok": True,
        "answer": clean_answer,
        "proposed_actions": proposed_actions,
    }


class LLMCallError(Exception):
    def __init__(self, message: str, error_code: str = "unknown"):
        super().__init__(message)
        self.error_code = error_code


def _classify_error(reason: str) -> str:
    reason_lower = reason.lower() if reason else ""
    if "429" in reason_lower or "rate" in reason_lower or "limite" in reason_lower:
        return "rate_limited"
    if "auth" in reason_lower or "billing" in reason_lower or "invalid_api_key" in reason_lower or "401" in reason_lower:
        return "auth_or_billing"
    if "timeout" in reason_lower or "tempo" in reason_lower:
        return "timeout"
    return "unknown"


HANDLERS = {
    "suggest_reply": _process_suggest_reply,
    "triage": _process_triage,
    "chat": _process_chat,
}


def _process_one_job(job: dict):
    global _worker_last_heartbeat
    _worker_last_heartbeat = int(datetime.utcnow().timestamp())

    job_id = job["job_id"]
    job_type = job.get("job_type", "")
    user_id = job.get("user_id", "default")
    attempts = job.get("attempts", 0)

    handler = HANDLERS.get(job_type)
    if not handler:
        job_update(job_id, "error", error_code="invalid_type", error_message=f"Unknown job type: {job_type}")
        return

    rl = rate_limit_check(user_id, LLM_RATE_LIMIT_RPM, LLM_MIN_INTERVAL)
    if not rl["ok"]:
        retry_after = rl.get("retry_after", 3)
        now = int(datetime.utcnow().timestamp())
        job_update(job_id, "retry_wait", next_run_at=now + max(retry_after, 2))
        return

    try:
        result = handler(job)
        job_update(job_id, "done", result=result)
    except LLMCallError as e:
        if e.error_code == "rate_limited" and attempts < LLM_MAX_RETRIES:
            now = int(datetime.utcnow().timestamp())
            backoff = int(LLM_RETRY_BASE_S * (attempts + 1))
            job_update(job_id, "retry_wait", error_code=e.error_code, error_message=str(e), next_run_at=now + backoff)
        elif e.error_code == "auth_or_billing":
            job_update(job_id, "error", error_code=e.error_code, error_message=str(e))
        elif attempts >= LLM_MAX_RETRIES:
            job_update(job_id, "error", error_code=e.error_code, error_message=f"Max retries exceeded: {str(e)}")
        else:
            now = int(datetime.utcnow().timestamp())
            backoff = int(LLM_RETRY_BASE_S * (attempts + 1))
            job_update(job_id, "retry_wait", error_code=e.error_code, error_message=str(e), next_run_at=now + backoff)
    except Exception as e:
        job_update(job_id, "error", error_code="internal", error_message=str(e)[:500])


def _worker_loop():
    global _worker_running, _worker_last_heartbeat
    logger.info("LLM worker started")
    init_db()

    while _worker_running:
        try:
            job = job_claim_next()
            if job:
                _process_one_job(job)
                _worker_last_heartbeat = int(datetime.utcnow().timestamp())
            else:
                time.sleep(LLM_QUEUE_POLL_S)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(2)


def start_worker():
    global _worker_thread, _worker_running, _worker_last_heartbeat
    if _worker_running:
        return
    _worker_running = True
    _worker_last_heartbeat = int(datetime.utcnow().timestamp())
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    logger.info("LLM worker thread started")


def stop_worker():
    global _worker_running
    _worker_running = False


def get_worker_status() -> dict:
    return {
        "running": _worker_running,
        "last_heartbeat": _worker_last_heartbeat,
        "alive": _worker_running and (int(datetime.utcnow().timestamp()) - _worker_last_heartbeat) < 30,
    }
