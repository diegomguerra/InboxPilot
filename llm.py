import os
import requests

def draft_reply(email_from: str, subject: str, body_text: str) -> dict:
    prompt = f"""
Assunto: {subject}
De: {email_from}
Corpo:
{body_text}

Responda em JSON com:
summary (máx 5 linhas),
priority (High/Medium/Low),
suggested_reply (texto pronto),
questions_to_answer (lista).

Não invente fatos. Se faltar informação, pergunte.
""".strip()

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": "Você é um assistente executivo. Seja formal, direto e preciso."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=45,
    )
    r.raise_for_status()
    return {"raw": r.json()["choices"][0]["message"]["content"]}
