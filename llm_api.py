import os
import json
import re
import logging
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

import uuid
from db import (
    init_db, get_message, set_draft,
    add_chat_message, get_chat_history, clear_chat_history,
    aq_add, aq_list, aq_remove, aq_get_queued, aq_update_status,
    mark_status, log_action,
    job_create, job_get, job_queue_stats, rate_limit_check, rate_limit_status,
    get_recent_messages, snapshot_get_latest,
)
from llm_client import call_llm, call_llm_multi, parse_json_response, LLM_MAX_INPUT_CHARS
from utils.text import clean_text, truncate_text, build_email_llm_context, parse_email_address

router = APIRouter(prefix="/llm", tags=["llm"])

INBOXPILOT_API_KEY = os.getenv("INBOXPILOT_API_KEY")

_providers_map = {}


def set_providers(providers: Dict):
    global _providers_map
    _providers_map = providers


def _check_api_key(x_api_key: Optional[str] = Header(None)):
    if INBOXPILOT_API_KEY:
        if not x_api_key or x_api_key != INBOXPILOT_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    return True


NO_REPLY_PATTERNS = re.compile(
    r'(no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|mailer[-_.]?daemon)',
    re.IGNORECASE
)


def _classify_for_blocking(from_addr: str, subject: str, category: str) -> dict:
    email_addr = parse_email_address(from_addr).lower()

    if category == "otp":
        return {"blocked": True, "suggested_action": "mark_read", "classification": "otp",
                "notes": ["Email OTP/verificacao - sem resposta necessaria"]}

    if NO_REPLY_PATTERNS.search(email_addr):
        return {"blocked": True, "suggested_action": "skip", "classification": "no-reply",
                "notes": ["Endereco no-reply detectado"]}

    if category in ("newsletter", "promo", "marketing"):
        return {"blocked": True, "suggested_action": "delete", "classification": "newsletter",
                "notes": ["Newsletter/promocional - sugerido deletar"]}

    if category == "automated":
        return {"blocked": False, "suggested_action": "skip", "classification": "automated",
                "notes": ["Email automatizado"]}

    return {"blocked": False, "suggested_action": None, "classification": category or "human",
            "notes": []}


def _get_email_data(key: str) -> dict:
    msg = get_message(key)
    if msg:
        return msg

    parts = key.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(404, f"Email {key} not found")

    provider_name, msg_id = parts
    if provider_name not in _providers_map:
        raise HTTPException(400, f"Unknown provider: {provider_name}")

    provider = _providers_map[provider_name]
    try:
        email_msg = provider.get_message(msg_id)
        if not email_msg:
            raise HTTPException(404, f"Email {key} not found in provider")
        d = email_msg.to_dict()
        return {
            "key": key,
            "provider": provider_name,
            "msg_id": msg_id,
            "from_addr": d.get("from", ""),
            "subject": d.get("subject", ""),
            "date": d.get("date", ""),
            "body_text": d.get("body", ""),
            "category": d.get("classification", "human"),
            "snippet": d.get("snippet", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch email: {str(e)}")


def _auto_fetch_provider_emails(limit: int = 30) -> List[str]:
    from datetime import datetime, timedelta
    import pytz

    keys = []
    tz = pytz.timezone("America/Sao_Paulo")
    now = datetime.now(tz)
    date_start = now - timedelta(days=7)
    date_end = now

    per_provider = max(limit // max(len(_providers_map), 1), 10)
    for provider_name, provider in _providers_map.items():
        if len(keys) >= limit:
            break
        try:
            msgs = provider.list_emails(
                folder="inbox",
                limit=min(limit - len(keys), per_provider),
                date_start=date_start,
                date_end=date_end,
            )
            for msg in msgs:
                key = f"{provider_name}:{msg.id}"
                keys.append(key)
        except Exception as e:
            logging.warning(f"Auto-fetch from {provider_name} failed: {e}")
            continue

    return keys[:limit]


class SuggestReplyRequest(BaseModel):
    session_id: str = ""
    key: str
    tone: str = "neutral"
    language: str = "pt"
    force: bool = False


class TriageRequest(BaseModel):
    session_id: str = ""
    keys: List[str]
    language: str = "pt"


def _enqueue_job(session_id: str, job_type: str, payload: dict) -> dict:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job_create(job_id, "default", session_id, job_type, payload)
    return {"ok": False, "queued": True, "job_id": job_id, "status": "queued",
            "message": "Assistente temporariamente ocupado. Processando em segundo plano."}


@router.post("/suggest-reply")
def llm_suggest_reply(req: SuggestReplyRequest, _: bool = Depends(_check_api_key)):
    init_db()

    email_data = _get_email_data(req.key)
    from_addr = email_data.get("from_addr", email_data.get("from", ""))
    subject = email_data.get("subject", "")
    category = email_data.get("category", "human")

    blocking = _classify_for_blocking(from_addr, subject, category)

    if blocking["blocked"] and not req.force:
        return {
            "ok": True,
            "key": req.key,
            "classification": blocking["classification"],
            "suggested_action": blocking["suggested_action"],
            "draft_body": "",
            "notes": blocking["notes"],
            "cached": False,
        }

    context = build_email_llm_context(email_data, LLM_MAX_INPUT_CHARS)

    tone_map = {
        "neutral": "tom neutro e profissional",
        "formal": "tom formal e respeitoso",
        "short": "tom direto e curto (maximo 3 frases)",
        "friendly": "tom amigavel e cordial",
    }
    tone_instruction = tone_map.get(req.tone, tone_map["neutral"])

    lang_map = {"pt": "portugues brasileiro", "en": "English"}
    lang_instruction = lang_map.get(req.language, lang_map["pt"])

    system_prompt = f"""Voce e um assistente de email executivo. Gere uma resposta profissional.
Regras:
- Responda em {lang_instruction}, com {tone_instruction}
- Nao invente fatos. Se faltar informacao, pergunte ao remetente.
- Seja objetivo e conciso
- Assinatura opcional: "Diego"
- Nao inclua dados sensiveis (CPF, senhas, etc)
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
        email_key=req.key,
        session_id=req.session_id,
        json_mode=True,
    )

    if not result["ok"]:
        reason = result.get("reason", "")
        if "429" in reason or "rate" in reason.lower() or "limite" in reason.lower():
            return _enqueue_job(req.session_id, "suggest_reply", {
                "key": req.key, "tone": req.tone, "language": req.language
            })
        return {"ok": False, "error_code": "llm_error", "message": reason,
                "key": req.key, "draft_body": "", "notes": []}

    parsed = parse_json_response(result["text"])

    if parsed:
        draft_body = parsed.get("draft_body", "")
        classification = parsed.get("classification", blocking["classification"])
        suggested_action = parsed.get("suggested_action", "send")
        notes = parsed.get("notes", [])
    else:
        draft_body = result["text"] if isinstance(result["text"], str) else ""
        classification = blocking["classification"]
        suggested_action = "send"
        notes = ["Resposta gerada (parse JSON falhou, texto direto)"]

    if draft_body:
        set_draft(req.key, draft_body)

    return {
        "ok": True,
        "key": req.key,
        "classification": classification,
        "suggested_action": suggested_action,
        "draft_body": draft_body,
        "notes": notes if isinstance(notes, list) else [str(notes)],
        "cached": result.get("cached", False),
    }


@router.post("/triage")
def llm_triage(req: TriageRequest, _: bool = Depends(_check_api_key)):
    init_db()

    if len(req.keys) > 50:
        req.keys = req.keys[:50]

    email_summaries = []
    for key in req.keys:
        try:
            email_data = _get_email_data(key)
            from_addr = email_data.get("from_addr", email_data.get("from", ""))
            subject = email_data.get("subject", "") or "(sem assunto)"
            date = email_data.get("date", "")
            category = email_data.get("category", "human")
            body = email_data.get("body_text", email_data.get("body", ""))
            body_clean = clean_text(body)
            body_short = truncate_text(body_clean, 1200)

            blocking = _classify_for_blocking(from_addr, subject, category)
            if blocking["blocked"]:
                notes = blocking.get("notes", ["Auto-classificado"])
                email_summaries.append({
                    "key": key,
                    "blocked": True,
                    "summary": f"{subject} - {notes[0] if notes else 'Auto-classificado'}",
                    "suggested_action": blocking["suggested_action"],
                    "priority": "low",
                })
                continue

            email_summaries.append({
                "key": key,
                "blocked": False,
                "from": from_addr,
                "subject": subject,
                "date": date,
                "body_preview": body_short,
            })
        except Exception as e:
            email_summaries.append({
                "key": key,
                "blocked": True,
                "summary": f"Erro ao carregar email: {str(e)[:80]}",
                "suggested_action": "skip",
                "priority": "low",
            })

    blocked_items = [e for e in email_summaries if e.get("blocked")]
    llm_items = [e for e in email_summaries if not e.get("blocked")]

    results = []
    for item in blocked_items:
        results.append({
            "key": item["key"],
            "summary": item["summary"],
            "suggested_action": item["suggested_action"],
            "priority": item["priority"],
        })

    if llm_items:
        lang_map = {"pt": "portugues brasileiro", "en": "English"}
        lang_instruction = lang_map.get(req.language, lang_map["pt"])

        system_prompt = f"""Voce e um assistente de triagem de emails. Analise cada email e retorne um JSON.
Regras:
- Responda em {lang_instruction}
- Para cada email, retorne: key, summary (max 2 frases), suggested_action (reply|delete|skip|mark_read), priority (low|med|high)
- Prioridade alta: emails que exigem resposta urgente ou acao imediata
- Prioridade media: emails que precisam de atencao mas podem esperar
- Prioridade baixa: informativos, newsletters, confirmacoes
- Retorne JSON valido: {{ "items": [...] }}"""

        emails_text = ""
        for item in llm_items:
            emails_text += f"\n---\nKEY: {item['key']}\nDe: {item['from']}\nAssunto: {item['subject']}\nData: {item['date']}\nCorpo:\n{item['body_preview']}\n"

        user_prompt = f"""Faca triagem dos seguintes emails:
{emails_text}

Retorne APENAS o JSON com: {{ "items": [{{ "key": "...", "summary": "...", "suggested_action": "...", "priority": "..." }}] }}"""

        result = call_llm(
            system=system_prompt,
            user=user_prompt,
            action="triage",
            email_key=",".join([i["key"] for i in llm_items]),
            session_id=req.session_id,
            json_mode=True,
        )

        if result["ok"]:
            parsed = parse_json_response(result["text"])
            if parsed:
                items = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
                if isinstance(items, list):
                    for item in items:
                        results.append({
                            "key": item.get("key", ""),
                            "summary": item.get("summary", "Sem resumo"),
                            "suggested_action": item.get("suggested_action", "skip"),
                            "priority": item.get("priority", "med"),
                        })
                else:
                    for item in llm_items:
                        results.append({
                            "key": item["key"],
                            "summary": "Erro no parse da triagem",
                            "suggested_action": "skip",
                            "priority": "low",
                        })
        else:
            reason = result.get("reason", "")
            if "429" in reason or "rate" in reason.lower() or "limite" in reason.lower():
                return _enqueue_job(req.session_id, "triage", {
                    "keys": req.keys, "language": req.language
                })
            for item in llm_items:
                results.append({
                    "key": item["key"],
                    "summary": f"Erro LLM: {reason[:60]}",
                    "suggested_action": "skip",
                    "priority": "low",
                })

    key_order = {k: i for i, k in enumerate(req.keys)}
    results.sort(key=lambda x: key_order.get(x.get("key", ""), 999))

    return {
        "ok": True,
        "items": results,
        "total": len(results),
    }


class ChatResetRequest(BaseModel):
    session_id: str


@router.post("/chat/reset")
def llm_chat_reset(req: ChatResetRequest, _: bool = Depends(_check_api_key)):
    init_db()
    clear_chat_history(req.session_id)
    return {"ok": True, "message": "Historico de conversa limpo."}


class ChatRequest(BaseModel):
    session_id: str
    message: str
    visible_keys: List[str] = []
    providers: str = ""


@router.post("/chat")
def llm_chat(req: ChatRequest, _: bool = Depends(_check_api_key)):
    init_db()

    add_chat_message(req.session_id, "user", req.message)

    email_context = ""
    snippets = []
    provider_filter = [p.strip().lower() for p in req.providers.split(",") if p.strip()] if req.providers else []
    keys_to_use = req.visible_keys if req.visible_keys else []
    if provider_filter and keys_to_use:
        keys_to_use = [k for k in keys_to_use if k.split(":")[0].lower() in provider_filter]

    def _parse_key_label(key: str) -> str:
        parts = key.split(":")
        if len(parts) >= 2:
            provider = parts[0].capitalize()
            folder = parts[1].capitalize() if len(parts) > 1 else ""
            return f"[{provider} {folder}]"
        return ""

    if keys_to_use:
        body_limit = 800 if len(keys_to_use) <= 15 else (400 if len(keys_to_use) <= 30 else 200)
        for idx, key in enumerate(keys_to_use, 1):
            try:
                email_data = _get_email_data(key)
                from_addr = email_data.get("from_addr", email_data.get("from", ""))
                subject = email_data.get("subject", "")
                body = email_data.get("body_text", email_data.get("body", ""))
                body_clean = clean_text(body)
                body_short = truncate_text(body_clean, body_limit)
                label = _parse_key_label(key)
                snippets.append(f"#{idx} {label}\nKEY: {key}\nDe: {from_addr}\nAssunto: {subject}\nCorpo:\n{body_short}")
            except Exception:
                continue

    if not snippets:
        snap = snapshot_get_latest(None)
        if snap and snap.get("payload_json"):
            items = snap["payload_json"]
            if provider_filter:
                items = [it for it in items if it.get("provider", "").lower() in provider_filter]
            logging.info(f"[CHAT] Using snapshot {snap['snapshot_id']} with {len(items)} items (filter={provider_filter})")
            snap_snippet_limit = 400 if len(items) <= 15 else (200 if len(items) <= 30 else 100)
            for idx, item in enumerate(items, 1):
                key = item.get("key", "")
                from_addr = item.get("from", "")
                subject = item.get("subject", "")
                snippet_text = item.get("snippet", "")
                if snippet_text and len(snippet_text) > snap_snippet_limit:
                    snippet_text = snippet_text[:snap_snippet_limit] + "..."
                date_str = item.get("date", "")
                label = _parse_key_label(key)
                snippets.append(f"#{idx} {label}\nKEY: {key}\nDe: {from_addr}\nAssunto: {subject}\nData: {date_str}\nResumo: {snippet_text}")
            keys_to_use = [item.get("key", "") for item in items]

    if not snippets:
        try:
            auto_keys = _auto_fetch_provider_emails(30)
            auto_body_limit = 800 if len(auto_keys) <= 15 else (400 if len(auto_keys) <= 30 else 200)
            for idx, key in enumerate(auto_keys, 1):
                try:
                    email_data = _get_email_data(key)
                    from_addr = email_data.get("from_addr", email_data.get("from", ""))
                    subject = email_data.get("subject", "")
                    body = email_data.get("body_text", email_data.get("body", ""))
                    body_clean = clean_text(body)
                    body_short = truncate_text(body_clean, auto_body_limit)
                    label = _parse_key_label(key)
                    snippets.append(f"#{idx} {label}\nKEY: {key}\nDe: {from_addr}\nAssunto: {subject}\nCorpo:\n{body_short}")
                    keys_to_use.append(key)
                except Exception:
                    continue
        except Exception as e:
            logging.warning(f"[CHAT] auto-fetch failed: {e}")

    if snippets:
        email_context = "\n\nEmails visiveis (na mesma ordem do dashboard):\n---\n" + "\n---\n".join(snippets)

    history = get_chat_history(req.session_id, limit=10)

    email_count = len(keys_to_use)
    provider_label = ""
    if provider_filter:
        provider_names = {"apple": "Apple Mail", "gmail": "Gmail", "microsoft": "Outlook"}
        provider_label = ", ".join(provider_names.get(p, p) for p in provider_filter)
    if email_context:
        filter_note = f" (filtrado: {provider_label})" if provider_label else ""
        context_intro = f"\n\nVoce tem acesso a {email_count} email(s) carregados abaixo{filter_note}. Leia, resuma, e gerencie conforme o usuario pedir."
    else:
        context_intro = "\n\nNenhum email carregado no momento. Sugira ao usuario clicar em 'Atualizar' no dashboard para carregar emails."

    base_prompt = f"""Voce e o assistente de email InboxPilot. Voce TEM acesso direto aos emails do usuario - eles estao listados abaixo.
Regras:
- Responda em portugues brasileiro, de forma direta e objetiva
- Voce PODE e DEVE ler os emails listados abaixo. Nao peca ao usuario para enviar emails - voce ja tem acesso a eles.
- Quando o usuario pedir para listar, ler, resumir ou buscar emails, use os dados dos emails abaixo
- Os emails estao numerados (#1, #2, etc.) na mesma ordem que o usuario ve no dashboard. Use essa numeracao para identificar emails por posicao.
- Cada email tem um rotulo [Provider Folder] (ex: [Apple Inbox], [Gmail Inbox]). Quando o usuario filtrar por provedor (ex: "emails da Apple"), conte apenas os emails desse provedor
- Quando sugerir acoes, retorne um bloco JSON com campo "proposed_actions" como array de objetos com: key, action (send|delete|mark_read|skip), body (opcional para send)
- Acoes validas: send (enviar resposta com body), delete (deletar), mark_read (marcar lido), skip (ignorar)
- Proponha acoes na PRIMEIRA vez que o usuario pedir. NAO peca confirmacao adicional - o sistema cuida disso automaticamente.
- Se o usuario pedir para responder, sugerir resposta, ou criar rascunho de resposta para um email, gere o texto da resposta e proponha como acao send com body. O usuario podera revisar e aprovar antes do envio.
- Se o usuario pedir para deletar, marcar como lido, ou ignorar emails, proponha as acoes correspondentes imediatamente
- Se o usuario confirmar ou aprovar acoes ja propostas, responda apenas "Acoes aprovadas." sem repetir as propostas
- Ao listar emails, inclua: remetente, assunto e um resumo curto do conteudo{context_intro}"""

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
    messages.append({"role": "user", "content": truncate_text(req.message, min(remaining_budget, 2000))})

    result = call_llm_multi(
        messages=messages,
        action="chat",
        session_id=req.session_id,
        max_tokens=500,
    )

    if not result["ok"]:
        reason = result.get("reason", "desconhecido")
        if "429" in reason or "rate" in reason.lower() or "limite" in reason.lower():
            job_resp = _enqueue_job(req.session_id, "chat", {
                "message": req.message, "visible_keys": req.visible_keys
            })
            job_resp["answer"] = job_resp.get("message", "Processando em segundo plano...")
            job_resp["proposed_actions"] = []
            return job_resp
        return {"ok": False, "answer": f"Erro LLM: {reason}", "proposed_actions": []}

    answer_text = result["text"]
    add_chat_message(req.session_id, "assistant", answer_text)

    proposed_actions = []
    parsed = parse_json_response(answer_text)
    if parsed and isinstance(parsed, dict):
        proposed_actions = parsed.get("proposed_actions", [])
        if not proposed_actions and "action" in parsed and "key" in parsed:
            proposed_actions = [parsed]
    elif parsed and isinstance(parsed, list):
        if all(isinstance(item, dict) and "key" in item and "action" in item for item in parsed):
            proposed_actions = parsed

    clean_answer = answer_text
    if proposed_actions:
        clean_answer = re.sub(r'```json\s*[\s\S]*?```', '', answer_text).strip()
        clean_answer = re.sub(r'\{[\s\S]*"proposed_actions"[\s\S]*\}', '', clean_answer).strip()
        clean_answer = re.sub(r'\[\s*\{[\s\S]*"action"[\s\S]*\}\s*\]', '', clean_answer).strip()
        if not clean_answer:
            action_labels = {"delete": "deletar", "send": "enviar", "mark_read": "marcar como lido", "skip": "ignorar"}
            summaries = []
            for pa in proposed_actions[:5]:
                label = action_labels.get(pa.get("action", ""), pa.get("action", ""))
                summaries.append(f"{label} {pa.get('key', '')}")
            clean_answer = f"Tenho {len(proposed_actions)} acoes propostas: {', '.join(summaries)}. Deseja aprovar?"

    return {
        "ok": True,
        "answer": clean_answer,
        "proposed_actions": proposed_actions,
    }


class QueueAddItem(BaseModel):
    key: str
    action: str
    body: Optional[str] = None


class QueueAddBatchRequest(BaseModel):
    session_id: str
    items: List[QueueAddItem]


class QueueExecuteRequest(BaseModel):
    session_id: str
    mode: str = "execute"


class DispatchAction(BaseModel):
    key: str
    action: str
    body: Optional[str] = None


class DispatchRequest(BaseModel):
    session_id: str
    mode: str = "execute"
    actions: List[DispatchAction]
    confirm_delete: bool = False
    context_id: str = ""


@router.post("/queue/add")
def queue_add_batch(req: QueueAddBatchRequest, _: bool = Depends(_check_api_key)):
    init_db()
    added = 0
    for item in req.items:
        if item.action not in ("send", "delete", "mark_read", "skip"):
            continue
        aq_add(req.session_id, item.key, item.action, item.body)
        if item.action == "send" and item.body:
            set_draft(item.key, item.body)
        added += 1
    return {"ok": True, "added": added}


@router.get("/queue/list")
def queue_list_items(session_id: str, _: bool = Depends(_check_api_key)):
    init_db()
    items = aq_list(session_id)
    result = []
    for item in items:
        result.append({
            "id": item["id"],
            "key": item["key"],
            "action": item["action"],
            "body": item.get("body"),
            "subject": item.get("subject") or item["key"],
            "from": item.get("from_addr") or "",
            "provider": item.get("provider") or item["key"].split(":")[0],
        })
    return {"ok": True, "items": result, "total": len(result)}


@router.delete("/queue/remove/{item_id}")
def queue_remove_item(item_id: int, session_id: str = "", _: bool = Depends(_check_api_key)):
    init_db()
    removed = aq_remove(item_id, session_id if session_id else None)
    return {"ok": removed}


@router.post("/queue/execute")
def queue_execute(req: QueueExecuteRequest, _: bool = Depends(_check_api_key)):
    init_db()
    items = aq_get_queued(req.session_id)
    if not items:
        return {"ok": True, "results": [], "message": "Fila vazia"}

    dry_run = req.mode == "dry_run"
    results = []

    send_items = [i for i in items if i["action"] == "send"]
    mark_items = [i for i in items if i["action"] == "mark_read"]
    delete_items = [i for i in items if i["action"] == "delete"]
    skip_items = [i for i in items if i["action"] == "skip"]

    for item in (send_items + mark_items + delete_items + skip_items):
        r = _execute_queue_item(item, dry_run)
        results.append(r)

    return {"ok": True, "dry_run": dry_run, "results": results}


@router.post("/dispatch")
def assistant_dispatch(req: DispatchRequest, _: bool = Depends(_check_api_key)):
    init_db()
    dry_run = req.mode == "dry_run"
    results = []

    print(f"DISPATCH: mode={req.mode}, confirm_delete={req.confirm_delete}, actions={len(req.actions)}", flush=True)
    for a in req.actions:
        print(f"  ACTION: key={a.key}, action={a.action}, body_len={len(a.body) if a.body else 0}", flush=True)

    send_actions = [a for a in req.actions if a.action == "send"]
    mark_actions = [a for a in req.actions if a.action == "mark_read"]
    delete_actions = [a for a in req.actions if a.action == "delete"]
    skip_actions = [a for a in req.actions if a.action == "skip"]

    for action in (send_actions + mark_actions + delete_actions + skip_actions):
        r = _dispatch_action(action.key, action.action, action.body, dry_run, req.session_id, confirm_delete=req.confirm_delete)
        print(f"  RESULT: key={action.key}, status={r['status']}, message={r['message']}", flush=True)
        results.append(r)

    return {"ok": True, "dry_run": dry_run, "results": results}


def _execute_queue_item(item: Dict, dry_run: bool) -> Dict:
    aq_id = item["id"]
    key = item["key"]
    action = item["action"]
    body = item.get("body")

    result = {"key": key, "action": action, "status": "ok", "message": ""}

    if dry_run:
        result["message"] = f"DRY RUN: {action}"
        aq_update_status(aq_id, "dry_run", result["message"])
        return result

    try:
        r = _dispatch_action(key, action, body, False, item.get("session_id", ""))
        result["status"] = r["status"]
        result["message"] = r["message"]
        aq_update_status(aq_id, "executed" if r["status"] == "ok" else "error", r["message"])
    except Exception as e:
        result["status"] = "error"
        result["message"] = str(e)
        aq_update_status(aq_id, "error", str(e))

    return result


def _dispatch_action(key: str, action: str, body: str, dry_run: bool, session_id: str, confirm_delete: bool = False) -> Dict:
    result = {"key": key, "action": action, "status": "ok", "message": "", "provider": ""}

    if dry_run:
        parts = key.split(":", 1)
        result["provider"] = parts[0] if len(parts) > 1 else ""
        result["message"] = f"DRY RUN: {action}"
        return result

    parts = key.split(":", 1)
    if len(parts) < 2:
        result["status"] = "error"
        result["message"] = "Invalid key format"
        return result

    provider_name = parts[0]
    result["provider"] = provider_name
    msg_id_part = parts[1]

    if ":" in msg_id_part:
        msg_id = msg_id_part.split(":", 1)[1]
    else:
        msg_id = msg_id_part

    if provider_name not in _providers_map:
        result["status"] = "error"
        result["message"] = f"Provider not available: {provider_name}"
        return result

    provider = _providers_map[provider_name]

    existing = get_message(key)
    if existing and existing.get("status") in ("sent", "deleted"):
        result["message"] = f"Already {existing['status']}"
        return result

    try:
        if action == "send":
            send_body = body
            if not send_body:
                from db import get_draft as _get_draft
                send_body = _get_draft(key)
            if not send_body:
                result["status"] = "error"
                result["message"] = "No body or draft available"
                return result

            print(f"  SEND: key={key}, provider={provider_name}, msg_id={msg_id}, body_len={len(send_body)}", flush=True)
            provider.send_reply(msg_id, send_body)
            mark_status(key, "sent")
            log_action(key, provider_name, msg_id, "send", "success", "Email sent")
            result["message"] = "Email enviado com sucesso"
            print(f"  SEND OK: {key} reply sent via {provider_name}", flush=True)

        elif action == "mark_read":
            provider.mark_read(msg_id)
            mark_status(key, "read")
            log_action(key, provider_name, msg_id, "mark_read", "success", "Marked as read")
            result["message"] = "Marked as read"

        elif action == "delete":
            existing_msg = get_message(key)
            current_status = existing_msg.get("status") if existing_msg else None
            print(f"  DELETE: key={key}, current_status={current_status}, confirm_delete={confirm_delete}", flush=True)
            if current_status == "pending_delete" or confirm_delete:
                provider.delete(msg_id)
                mark_status(key, "deleted")
                log_action(key, provider_name, msg_id, "delete", "success", "Email deleted")
                result["message"] = "Email deleted"
                print(f"  DELETE OK: {key} deleted via {provider_name}", flush=True)
            else:
                mark_status(key, "pending_delete")
                log_action(key, provider_name, msg_id, "delete", "pending", "Marked for deletion (two-step)")
                result["message"] = "Marked for deletion (two-step)"
                print(f"  DELETE PENDING: {key} marked pending_delete (two-step)", flush=True)

        elif action == "skip":
            mark_status(key, "skipped")
            log_action(key, provider_name, msg_id, "skip", "success", "Skipped")
            result["message"] = "Skipped"

    except Exception as e:
        result["status"] = "error"
        result["message"] = str(e)
        log_action(key, provider_name, msg_id, action, "error", str(e))

    return result


LLM_RATE_LIMIT_RPM = int(os.getenv("LLM_RATE_LIMIT_PER_MINUTE", "20"))
LLM_MIN_INTERVAL = int(os.getenv("LLM_MIN_SECONDS_BETWEEN_CALLS", "3"))


class JobCreateRequest(BaseModel):
    session_id: str = ""
    user_id: str = "default"
    job_type: str
    payload: dict = {}


@router.post("/job")
def create_job(req: JobCreateRequest, _: bool = Depends(_check_api_key)):
    init_db()
    if req.job_type not in ("suggest_reply", "triage", "chat"):
        raise HTTPException(400, f"Invalid job_type: {req.job_type}")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    result = job_create(job_id, req.user_id, req.session_id, req.job_type, req.payload)
    return {"ok": True, **result}


@router.get("/job/{job_id}")
def get_job_status(job_id: str, _: bool = Depends(_check_api_key)):
    init_db()
    job = job_get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    resp = {
        "ok": True,
        "job_id": job["job_id"],
        "status": job["status"],
        "job_type": job.get("job_type", ""),
        "attempts": job.get("attempts", 0),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }
    if job["status"] == "done" and job.get("result"):
        resp["result"] = job["result"]
    return resp


@router.get("/debug/status")
def debug_status(_: bool = Depends(_check_api_key)):
    init_db()
    from llm_worker import get_worker_status
    stats = job_queue_stats()
    rl = rate_limit_status("default")
    worker = get_worker_status()
    return {
        "ok": True,
        "worker": worker,
        "queue": stats,
        "rate_limit": rl,
    }
