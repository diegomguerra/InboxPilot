import os
import io
import uuid
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Header, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from db import init_db, get_message, mark_status, log_action, get_draft, _get_conn
from time_filters import get_date_range_info

router = APIRouter()

INBOXPILOT_API_KEY = os.getenv("INBOXPILOT_API_KEY")
BASE_URL = os.getenv("BASE_URL", f"https://{os.getenv('REPLIT_DEV_DOMAIN', 'localhost:5000')}")

_providers_map = {}


def set_providers(providers: Dict):
    global _providers_map
    _providers_map = providers


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if INBOXPILOT_API_KEY:
        if not x_api_key or x_api_key != INBOXPILOT_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    return True


def _get_date_range(range_filter: str, from_date: str = None, to_date: str = None):
    """
    Get date range using unified time_filters (America/Sao_Paulo).
    Returns (start_utc, end_utc) tuple.
    """
    info = get_date_range_info(range_filter, 7, from_date, to_date)
    return (info["start_utc"], info["end_utc"])


def _get_emails_for_export(
    providers: List[str],
    folders: List[str],
    range_filter: str,
    from_date: str = None,
    to_date: str = None,
    limit_per_provider: int = 30
) -> List[Dict]:
    init_db()
    conn = _get_conn()
    cursor = conn.cursor()
    
    date_start, date_end = _get_date_range(range_filter, from_date, to_date)
    date_start_str = date_start.isoformat()
    date_end_str = date_end.isoformat()
    
    emails = []
    
    for prov in providers:
        if prov not in _providers_map:
            continue
        
        folder_placeholders = ','.join(['?'] * len(folders))
        
        cursor.execute(f"""
            SELECT * FROM messages 
            WHERE provider = ? 
            AND folder IN ({folder_placeholders})
            AND status NOT IN ('deleted', 'sent')
            AND date >= ?
            AND date <= ?
            ORDER BY date DESC
            LIMIT ?
        """, [prov] + folders + [date_start_str, date_end_str, limit_per_provider])
        
        rows = cursor.fetchall()
        for row in rows:
            emails.append(dict(row))
    
    conn.close()
    return emails


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    text = text.replace('&', '&amp;')
    return text[:5000]


def _generate_pdf(emails: List[Dict], export_id: str, filters: Dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, 
                           leftMargin=0.75*inch, rightMargin=0.75*inch,
                           topMargin=0.75*inch, bottomMargin=0.75*inch)
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=16, spaceAfter=12)
    header_style = ParagraphStyle('Header', parent=styles['Normal'], fontSize=10, spaceAfter=6)
    label_style = ParagraphStyle('Label', parent=styles['Normal'], fontSize=9, textColor='#666666')
    content_style = ParagraphStyle('Content', parent=styles['Normal'], fontSize=10, spaceAfter=4)
    body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=9, leftIndent=20)
    
    story = []
    
    story.append(Paragraph("INBOXPILOT - EXPORT PDF", title_style))
    story.append(Spacer(1, 12))
    
    session_id = filters.get('session_id', export_id)
    header_lines = [
        f"<b>InboxPilot Export</b>",
        f"<b>Session ID:</b> {session_id}",
        f"<b>Filtros aplicados:</b> providers={filters.get('providers')}, folders={filters.get('folders')}, date_mode={filters.get('date_mode')}, from={filters.get('from_date')}, to={filters.get('to_date')}",
        f"<b>Total de emails:</b> {len(emails)}",
        f"<b>Gerado em:</b> {datetime.now(timezone.utc).isoformat()}",
    ]
    
    for line in header_lines:
        story.append(Paragraph(line, header_style))
    
    story.append(Spacer(1, 12))
    
    instruction_style = ParagraphStyle('Instruction', parent=styles['Normal'], fontSize=10, spaceAfter=4, leftIndent=10)
    
    story.append(Paragraph("<b>INSTRUCOES PARA O CHATGPT:</b>", title_style))
    story.append(Spacer(1, 6))
    
    intro_instructions = [
        "<b>IMPORTANTE: NAO DECIDA SOZINHO!</b>",
        "Voce deve CONVERSAR com o usuario sobre CADA email, um por um.",
        "Apresente um resumo do email e PERGUNTE ao usuario qual acao ele deseja tomar.",
        "Aguarde a resposta do usuario antes de passar para o proximo email.",
        "Somente ao final, quando o usuario confirmar, gere o JSON de despacho.",
        "",
        "<b>FLUXO CORRETO:</b>",
        "1. Apresente o EMAIL 1 com resumo (remetente, assunto, contexto)",
        "2. Pergunte: 'O que deseja fazer com este email?'",
        "3. Aguarde a decisao do usuario",
        "4. Repita para cada email",
        "5. Ao final, pergunte: 'Posso gerar o JSON de despacho?'",
        "6. Gere o JSON somente apos confirmacao",
    ]
    
    for line in intro_instructions:
        story.append(Paragraph(line, instruction_style))
    
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>ACOES DISPONIVEIS (explique ao usuario):</b>", header_style))
    story.append(Spacer(1, 6))
    
    actions_list = [
        "<b>delete</b> - Apagar o email permanentemente",
        "<b>mark_read</b> - Apenas marcar como lido (manter na caixa)",
        "<b>mark_unread</b> - Marcar como nao lido",
        "<b>skip</b> - Ignorar por enquanto (nenhuma acao)",
        "<b>suggest_reply</b> - Voce sugere uma resposta para o usuario revisar",
        "<b>send</b> - Enviar resposta (usuario deve fornecer ou aprovar o texto)",
        "<b>send_with_edits</b> - Salvar rascunho para revisao posterior",
    ]
    
    for action in actions_list:
        story.append(Paragraph(f"• {action}", instruction_style))
    
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>LEMBRE-SE:</b> Nunca decida por conta propria. Sempre pergunte ao usuario!", header_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Ao final, retorne um JSON no formato do modelo ao final deste documento.", header_style))
    
    story.append(Spacer(1, 24))
    story.append(Paragraph("=" * 80, content_style))
    story.append(Spacer(1, 12))
    
    for idx, email in enumerate(emails, 1):
        story.append(Paragraph(f"<b>EMAIL {idx}</b>", title_style))
        story.append(Spacer(1, 6))
        
        key = email.get('key', '')
        provider = email.get('provider', '')
        folder = email.get('folder', '')
        date_str = email.get('date', '')
        from_addr = email.get('from_addr', '')
        subject = _clean_text(email.get('subject', ''))
        classification = email.get('category', 'unknown')
        body_text = _clean_text(email.get('body_text', ''))
        snippet = body_text[:200] + "..." if len(body_text) > 200 else body_text
        
        fields = [
            ("[#] Indice", str(idx)),
            ("Provider", provider),
            ("Key", key),
            ("Classification", classification),
            ("From", from_addr),
            ("Subject", subject),
            ("Date", date_str),
            ("Unread", "true"),
            ("Preview", snippet),
        ]
        
        for label, value in fields:
            story.append(Paragraph(f"<b>{label}:</b> {value}", content_style))
        
        story.append(Spacer(1, 6))
        story.append(Paragraph("<b>body:</b>", label_style))
        
        body_paragraphs = body_text.split('\n')[:30]
        for para in body_paragraphs:
            if para.strip():
                story.append(Paragraph(para[:500], body_style))
        
        story.append(Spacer(1, 6))
        story.append(Paragraph("<b>suggested_action:</b> (preencher)", label_style))
        story.append(Paragraph("<b>suggested_reply:</b> (preencher se action=send)", label_style))
        
        story.append(Spacer(1, 12))
        story.append(Paragraph("-" * 80, content_style))
        story.append(Spacer(1, 12))
    
    story.append(Spacer(1, 24))
    story.append(Paragraph("<b>MODELO DE DESPACHO FINAL (JSON):</b>", title_style))
    story.append(Spacer(1, 12))
    
    example_json = {
        "session_id": session_id,
        "dry_run": False,
        "actions": [
            {"key": "apple:12345", "decision": "delete"},
            {"key": "apple:12346", "decision": "mark_read"},
            {"key": "apple:12347", "decision": "skip"},
            {"key": "gmail:abc123", "decision": "suggest_reply", "suggested_text": "Sugestao de resposta aqui..."},
            {"key": "gmail:xyz789", "decision": "send", "reply": {"body": "Texto aprovado para enviar"}}
        ]
    }
    
    json_str = json.dumps(example_json, indent=2, ensure_ascii=False)
    for line in json_str.split('\n'):
        story.append(Paragraph(line.replace(' ', '&nbsp;'), body_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def _get_session_emails_for_export(session_id: str) -> List[Dict]:
    from db import get_session_items
    items = get_session_items(session_id, limit=500)
    
    emails = []
    for item in items:
        emails.append({
            "key": item.get("key"),
            "provider": item.get("provider"),
            "folder": item.get("folder", "inbox"),
            "date": item.get("date"),
            "from_addr": item.get("sender"),
            "subject": item.get("subject"),
            "category": item.get("classification"),
            "body_text": item.get("body_text", "") or ""
        })
    
    return emails


@router.get("/export/pdf")
def export_pdf(
    session_id: Optional[str] = Query(None, description="Session ID to export (uses session items)"),
    providers: str = Query("apple,gmail", description="Comma-separated providers"),
    folders: str = Query("inbox", description="Comma-separated folders"),
    range: str = Query("today", description="today|week|month|custom"),
    from_date: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD for custom range"),
    to_date: Optional[str] = Query(None, alias="to", description="YYYY-MM-DD for custom range"),
    limit_per_provider: int = Query(30, description="Max emails per provider (max 200)"),
    _: bool = Depends(check_api_key)
):
    init_db()
    
    provider_list = [p.strip() for p in providers.split(',') if p.strip()]
    folder_list = [f.strip() for f in folders.split(',') if f.strip()]
    limit = min(limit_per_provider, 200)
    
    export_id = str(uuid.uuid4())
    
    date_mode = range
    
    if session_id:
        from db import get_session
        import json as json_mod
        session = get_session(session_id)
        if session:
            provider_list = json_mod.loads(session.get("providers", "[]"))
            folder_list = json_mod.loads(session.get("folders", "[]"))
            date_mode = session.get("date_mode") or session.get("range_filter", "today")
            from_date = session.get("from_date")
            to_date = session.get("to_date")
        
        emails = _get_session_emails_for_export(session_id)
    else:
        emails = _get_emails_for_export(
            providers=provider_list,
            folders=folder_list,
            range_filter=range,
            from_date=from_date,
            to_date=to_date,
            limit_per_provider=limit
        )
    
    filters = {
        "session_id": session_id or export_id,
        "providers": provider_list,
        "folders": folder_list,
        "date_mode": date_mode,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit
    }
    
    pdf_bytes = _generate_pdf(emails, export_id, filters)
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"InboxPilot_{range}_{today_str}.pdf"
    
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-ID": export_id,
            "X-Email-Count": str(len(emails))
        }
    )


class DispatchReply(BaseModel):
    subject: Optional[str] = None
    body: str


class DispatchAction(BaseModel):
    key: str
    decision: str
    reply: Optional[DispatchReply] = None
    suggested_text: Optional[str] = None


class ComposeEmail(BaseModel):
    provider: str
    to: str
    subject: str
    body: str


class DispatchImportRequest(BaseModel):
    export_id: Optional[str] = None
    session_id: Optional[str] = None
    dry_run: bool = True
    force: bool = False
    actions: List[DispatchAction] = []
    compose_emails: List[ComposeEmail] = []


@router.post("/dispatch/import")
def dispatch_import(request: DispatchImportRequest, _: bool = Depends(check_api_key)):
    init_db()
    
    valid_decisions = {"skip", "mark_read", "mark_unread", "delete", "send", "send_with_edits", "suggest_reply"}
    
    priority_order = {"send": 0, "send_with_edits": 1, "suggest_reply": 2, "mark_read": 3, "mark_unread": 4, "skip": 5, "delete": 6}
    sorted_actions = sorted(request.actions, key=lambda a: priority_order.get(a.decision, 99))
    
    counts = {"send": 0, "mark_read": 0, "mark_unread": 0, "delete": 0, "skip": 0, "suggest_reply": 0, "ignored": 0, "errors": 0, "reset": 0}
    results = []
    
    for action in sorted_actions:
        key = action.key
        decision = action.decision
        
        result = {
            "key": key,
            "decision": decision,
            "status": "planned" if request.dry_run else "pending",
            "detail": ""
        }
        
        if decision not in valid_decisions:
            result["status"] = "error"
            result["detail"] = f"Invalid decision: {decision}"
            counts["errors"] += 1
            results.append(result)
            continue
        
        if decision == "send" and not action.reply:
            result["status"] = "error"
            result["detail"] = "reply required for send decision"
            counts["errors"] += 1
            results.append(result)
            continue
        
        msg = get_message(key)
        msg_in_db = msg is not None
        
        current_status = msg.get("status", "") if msg else ""
        if current_status in ("sent", "deleted"):
            if request.force:
                mark_status(key, "classified")
                counts["reset"] += 1
                result["detail"] = f"Reset from {current_status}"
                current_status = "classified"
            else:
                result["status"] = "ignored"
                result["detail"] = f"Already processed: {current_status}. Use force=true to reset."
                counts["ignored"] += 1
                results.append(result)
                continue
        
        if request.dry_run:
            result["status"] = "planned"
            result["detail"] = f"Would execute: {decision}"
            counts[decision] += 1
            results.append(result)
            continue
        
        parts = key.split(":", 1)
        provider_name = parts[0] if len(parts) > 1 else ""
        msg_id = parts[1] if len(parts) > 1 else key
        
        try:
            if provider_name not in _providers_map:
                raise Exception(f"Provider not available: {provider_name}")
            
            provider = _providers_map[provider_name]
            
            if decision == "skip":
                if msg_in_db:
                    mark_status(key, "skipped")
                log_action(key, provider_name, msg_id, "skip", "success", "Skipped via dispatch")
                result["status"] = "done"
                result["detail"] = "Skipped"
                counts["skip"] += 1
            
            elif decision == "mark_read":
                provider.mark_read(msg_id)
                if msg_in_db:
                    mark_status(key, "read")
                log_action(key, provider_name, msg_id, "mark_read", "success", "Marked as read via dispatch")
                result["status"] = "done"
                result["detail"] = "Marked as read"
                counts["mark_read"] += 1
            
            elif decision == "mark_unread":
                if hasattr(provider, 'mark_unread'):
                    provider.mark_unread(msg_id)
                if msg_in_db:
                    mark_status(key, "unread")
                log_action(key, provider_name, msg_id, "mark_unread", "success", "Marked as unread via dispatch")
                result["status"] = "done"
                result["detail"] = "Marked as unread"
                counts["mark_unread"] += 1
            
            elif decision == "suggest_reply":
                suggested = action.suggested_text or "Sugestao pendente"
                if msg_in_db:
                    mark_status(key, "suggestion_pending")
                log_action(key, provider_name, msg_id, "suggest_reply", "success", f"Suggestion: {suggested[:100]}")
                result["status"] = "done"
                result["detail"] = f"Sugestao salva: {suggested[:50]}..."
                result["suggested_text"] = suggested
                counts["suggest_reply"] += 1
            
            elif decision == "delete":
                provider.delete(msg_id)
                if msg_in_db:
                    mark_status(key, "deleted")
                log_action(key, provider_name, msg_id, "delete", "success", "Deleted via dispatch")
                result["status"] = "done"
                result["detail"] = "Deleted"
                counts["delete"] += 1
            
            elif decision == "send":
                body = action.reply.body
                provider.send_reply(msg_id, body)
                if msg_in_db:
                    mark_status(key, "sent")
                log_action(key, provider_name, msg_id, "send", "success", "Sent via dispatch")
                result["status"] = "done"
                result["detail"] = "Email sent"
                counts["send"] += 1
            
            elif decision == "send_with_edits":
                if action.reply:
                    body = action.reply.body
                    if msg_in_db:
                        mark_status(key, "pending_review")
                    log_action(key, provider_name, msg_id, "send_with_edits", "pending", f"Needs review: {body[:100]}")
                    result["status"] = "pending"
                    result["detail"] = "Resposta salva para revisao (aprovar antes de enviar)"
                    result["draft"] = body
                else:
                    result["status"] = "error"
                    result["detail"] = "reply required for send_with_edits"
                    counts["errors"] += 1
                counts["send"] += 1
        
        except Exception as e:
            result["status"] = "error"
            result["detail"] = str(e)
            counts["errors"] += 1
            log_action(key, provider_name, msg_id, decision, "error", str(e))
        
        results.append(result)
    
    compose_results = []
    compose_count = {"sent": 0, "errors": 0}
    
    for compose in request.compose_emails:
        comp_result = {
            "provider": compose.provider,
            "to": compose.to,
            "subject": compose.subject,
            "status": "planned" if request.dry_run else "pending"
        }
        
        if compose.provider not in _providers_map:
            comp_result["status"] = "error"
            comp_result["detail"] = f"Provider not available: {compose.provider}"
            compose_count["errors"] += 1
            compose_results.append(comp_result)
            continue
        
        if request.dry_run:
            comp_result["status"] = "planned"
            comp_result["detail"] = f"Would send to: {compose.to}"
            compose_results.append(comp_result)
            continue
        
        try:
            provider = _providers_map[compose.provider]
            if not hasattr(provider, 'compose_email'):
                raise Exception(f"Provider {compose.provider} does not support compose")
            
            result_send = provider.compose_email(compose.to, compose.subject, compose.body)
            comp_result["status"] = "sent"
            comp_result["detail"] = f"Email sent to {compose.to}"
            comp_result["message_id"] = result_send.get("message_id", "")
            compose_count["sent"] += 1
            log_action(f"compose:{compose.provider}", compose.provider, compose.to, "compose", "success", f"Sent: {compose.subject}")
        except Exception as e:
            comp_result["status"] = "error"
            comp_result["detail"] = str(e)
            compose_count["errors"] += 1
            log_action(f"compose:{compose.provider}", compose.provider, compose.to, "compose", "error", str(e))
        
        compose_results.append(comp_result)
    
    return {
        "ok": True,
        "dry_run": request.dry_run,
        "export_id": request.export_id,
        "counts": counts,
        "compose_counts": compose_count,
        "results": results,
        "compose_results": compose_results
    }


@router.get("/dispatch/example")
def dispatch_example():
    return {
        "session_id": "sess_YYYYMMDD_HHMMSS",
        "dry_run": False,
        "force": False,
        "actions": [
            {"key": "apple:12345", "decision": "delete"},
            {"key": "apple:12346", "decision": "mark_read"},
            {"key": "apple:12347", "decision": "mark_unread"},
            {"key": "apple:12348", "decision": "skip"},
            {"key": "gmail:abc123", "decision": "suggest_reply", "suggested_text": "Sugestao de resposta..."},
            {"key": "gmail:xyz789", "decision": "send", "reply": {"body": "Texto aprovado"}}
        ],
        "compose_emails": [
            {"provider": "gmail", "to": "cliente@empresa.com", "subject": "Assunto", "body": "Corpo do email"},
            {"provider": "apple", "to": "fornecedor@empresa.com", "subject": "Cotacao", "body": "Solicito cotacao..."}
        ],
        "_help": {
            "session_id": "ID da sessao do PDF exportado",
            "dry_run": "false=executar, true=simulacao",
            "force": "true=reprocessar emails ja processados",
            "decisions": "delete, mark_read, mark_unread, skip, suggest_reply, send, send_with_edits",
            "compose_emails": "Lista de novos emails para enviar (provider: gmail ou apple)"
        }
    }
