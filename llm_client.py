import os
import json
import hashlib
import time
import requests
from typing import Optional, Dict, Any
from db import llm_cache_get, llm_cache_set, llm_log_insert

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
LLM_TRIAGE_MODEL = os.getenv("LLM_TRIAGE_MODEL", "") or LLM_MODEL
LLM_REPLY_MODEL = os.getenv("LLM_REPLY_MODEL", "") or LLM_MODEL
LLM_CHAT_MODEL = os.getenv("LLM_CHAT_MODEL", "") or LLM_MODEL
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "350"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL_SECONDS", "604800"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
LLM_MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "12000"))

def get_model_for_action(action: str) -> str:
    if action in ("suggest_reply", "reply"):
        return LLM_REPLY_MODEL
    if action == "triage":
        return LLM_TRIAGE_MODEL
    if action == "chat":
        return LLM_CHAT_MODEL
    return LLM_MODEL


def _make_prompt_hash(system: str, user: str, model: str, temperature: float, max_tokens: int) -> str:
    content = f"{system}|{user}|{model}|{temperature}|{max_tokens}"
    return hashlib.sha256(content.encode()).hexdigest()


def _make_cache_key(action: str, email_key: str, prompt_hash: str) -> str:
    content = f"{action}|{email_key}|{prompt_hash}"
    return hashlib.sha256(content.encode()).hexdigest()


def call_llm(
    system: str,
    user: str,
    max_tokens: int = None,
    temperature: float = None,
    action: str = "generic",
    email_key: str = "",
    session_id: str = "",
    use_cache: bool = True,
    json_mode: bool = False,
    model: str = None,
) -> Dict[str, Any]:
    if max_tokens is None:
        max_tokens = LLM_MAX_OUTPUT_TOKENS
    if temperature is None:
        temperature = LLM_TEMPERATURE
    if model is None:
        model = get_model_for_action(action)

    prompt_hash = _make_prompt_hash(system, user, model, temperature, max_tokens)
    cache_key = _make_cache_key(action, email_key, prompt_hash)

    if use_cache:
        cached = llm_cache_get(cache_key)
        if cached:
            llm_log_insert(session_id, action, email_key, len(user), 0, 1)
            return {"ok": True, "text": cached["response_json"], "cached": True, "usage_tokens": 0, "model": model}

    api_key = LLM_API_KEY
    if not api_key:
        return {"ok": False, "reason": "LLM API key not configured", "text": "", "cached": False, "usage_tokens": 0}

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        if r.status_code == 429:
            last_error = "Limite de requisições atingido (429). Aguarde alguns segundos."
            llm_log_insert(session_id, action, email_key, len(user), 0, 0)
            return {"ok": False, "reason": last_error, "text": "", "cached": False, "usage_tokens": 0, "error_code": "rate_limited"}
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        output_tokens = usage.get("completion_tokens", 0)

        llm_cache_set(cache_key, LLM_PROVIDER, model, action, email_key, prompt_hash, text, LLM_CACHE_TTL)
        llm_log_insert(session_id, action, email_key, len(user), output_tokens, 0)

        return {"ok": True, "text": text, "cached": False, "usage_tokens": output_tokens, "model": model}
    except requests.exceptions.Timeout:
        last_error = "Tempo esgotado aguardando resposta da IA."
    except Exception as e:
        last_error = str(e)

    llm_log_insert(session_id, action, email_key, len(user), 0, 0)
    return {"ok": False, "reason": last_error, "text": "", "cached": False, "usage_tokens": 0}


def call_llm_multi(
    messages: list,
    max_tokens: int = None,
    temperature: float = None,
    action: str = "chat",
    session_id: str = "",
    json_mode: bool = False,
    model: str = None,
) -> Dict[str, Any]:
    if max_tokens is None:
        max_tokens = LLM_MAX_OUTPUT_TOKENS
    if temperature is None:
        temperature = LLM_TEMPERATURE
    if model is None:
        model = get_model_for_action(action)

    api_key = LLM_API_KEY
    if not api_key:
        return {"ok": False, "reason": "LLM API key not configured", "text": "", "cached": False, "usage_tokens": 0}

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    input_chars = sum(len(m.get("content", "")) for m in messages)
    last_error = None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        if r.status_code == 429:
            last_error = "Limite de requisições atingido (429). Aguarde alguns segundos."
            llm_log_insert(session_id, action, "", input_chars, 0, 0)
            return {"ok": False, "reason": last_error, "text": "", "cached": False, "usage_tokens": 0, "error_code": "rate_limited"}
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        output_tokens = usage.get("completion_tokens", 0)

        llm_log_insert(session_id, action, "", input_chars, output_tokens, 0)
        return {"ok": True, "text": text, "cached": False, "usage_tokens": output_tokens}
    except requests.exceptions.Timeout:
        last_error = "Tempo esgotado aguardando resposta da IA."
    except requests.exceptions.HTTPError as e:
        last_error = f"Erro na API OpenAI ({r.status_code}): {r.text[:200] if r.text else str(e)}"
    except Exception as e:
        last_error = str(e)

    llm_log_insert(session_id, action, "", input_chars, 0, 0)
    return {"ok": False, "reason": last_error, "text": "", "cached": False, "usage_tokens": 0}


def parse_json_response(text: str) -> Optional[Dict]:
    if isinstance(text, dict):
        return text
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    import re
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            parsed_list = json.loads(match.group(0))
            if isinstance(parsed_list, list):
                return parsed_list
        except json.JSONDecodeError:
            pass
    return None
