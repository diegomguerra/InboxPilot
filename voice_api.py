import os
import io
import logging
import httpx
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, Header, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/voice", tags=["voice"])

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
INBOXPILOT_API_KEY = os.getenv("INBOXPILOT_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "gpt-4o-mini-transcribe")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "nova")

TTS_INSTRUCTIONS = os.getenv("TTS_INSTRUCTIONS", (
    "Speak in a calm, clear, and natural tone like a professional assistant. "
    "Maintain the same pace and energy whether speaking Portuguese or English. "
    "Do not speed up or change intonation when switching languages. "
    "Keep a steady, warm rhythm throughout. "
    "Pronounce Portuguese words with a Brazilian accent. "
    "Be concise and articulate."
))


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if INBOXPILOT_API_KEY:
        if not x_api_key or x_api_key != INBOXPILOT_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
    return True


@router.post("/transcribe")
async def transcribe_audio(
    audio: UploadFile = File(...),
    language: str = Form("pt"),
    _: bool = Depends(check_api_key),
):
    audio_bytes = await audio.read()
    if len(audio_bytes) < 1000:
        return {"ok": False, "text": "", "message": "Áudio muito curto"}

    model = WHISPER_MODEL

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            data_fields = {"model": model, "language": language, "response_format": "json"}

            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                files={"file": (audio.filename or "audio.webm", audio_bytes, audio.content_type or "audio/webm")},
                data=data_fields,
            )

        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "").strip()
            logging.info(f"[STT:{model}] Transcribed {len(audio_bytes)} bytes -> '{text[:80]}'")
            return {"ok": True, "text": text, "model": model}
        else:
            error_text = resp.text[:200]
            logging.error(f"[STT:{model}] Error {resp.status_code}: {error_text}")
            if model != "whisper-1" and resp.status_code in (400, 401, 403, 404, 422):
                logging.info(f"[STT] Falling back to whisper-1 (status {resp.status_code})")
                return await _transcribe_fallback(audio_bytes, audio.filename, audio.content_type, language)
            return {"ok": False, "text": "", "message": f"Erro na transcrição ({resp.status_code})"}

    except Exception as e:
        logging.error(f"[STT:{model}] Exception: {e}")
        return {"ok": False, "text": "", "message": str(e)}


async def _transcribe_fallback(audio_bytes, filename, content_type, language):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                files={"file": (filename or "audio.webm", audio_bytes, content_type or "audio/webm")},
                data={"model": "whisper-1", "language": language, "response_format": "json"},
            )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "").strip()
            logging.info(f"[STT:whisper-1-fallback] Transcribed {len(audio_bytes)} bytes -> '{text[:80]}'")
            return {"ok": True, "text": text, "model": "whisper-1"}
        else:
            logging.error(f"[STT:whisper-1-fallback] Error {resp.status_code}: {resp.text[:200]}")
            return {"ok": False, "text": "", "message": f"Erro na transcrição ({resp.status_code})"}
    except Exception as e:
        logging.error(f"[STT:whisper-1-fallback] Exception: {e}")
        return {"ok": False, "text": "", "message": str(e)}


class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    speed: float = 1.0
    instructions: str = ""


@router.post("/tts")
async def text_to_speech(req: TTSRequest, _: bool = Depends(check_api_key)):
    if not req.text or not req.text.strip():
        raise HTTPException(400, "Texto vazio")

    text = req.text.strip()[:4096]
    voice = req.voice or TTS_VOICE
    model = TTS_MODEL
    instructions = req.instructions.strip() if req.instructions else TTS_INSTRUCTIONS

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        "speed": max(0.25, min(4.0, req.speed)),
    }

    if model.startswith("gpt-4o") and instructions and instructions != "__none__":
        payload["instructions"] = instructions

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if resp.status_code == 200:
            audio_data = resp.content
            logging.info(f"[TTS:{model}] Generated {len(audio_data)} bytes for '{text[:50]}'")
            return StreamingResponse(
                io.BytesIO(audio_data),
                media_type="audio/mpeg",
                headers={"Content-Length": str(len(audio_data))},
            )
        else:
            error_text = resp.text[:200]
            logging.error(f"[TTS:{model}] Error {resp.status_code}: {error_text}")
            if model != "tts-1-hd" and resp.status_code in (400, 401, 403, 404, 422):
                logging.info(f"[TTS] Falling back to tts-1-hd (status {resp.status_code})")
                return await _tts_fallback(text, voice, req.speed)
            raise HTTPException(resp.status_code, f"Erro no TTS: {error_text}")

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"[TTS:{model}] Exception: {e}")
        raise HTTPException(500, str(e))


async def _tts_fallback(text, voice, speed):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "tts-1-hd",
                    "input": text,
                    "voice": voice,
                    "response_format": "mp3",
                    "speed": max(0.5, min(2.0, speed)),
                },
            )
        if resp.status_code == 200:
            audio_data = resp.content
            logging.info(f"[TTS:tts-1-hd-fallback] Generated {len(audio_data)} bytes")
            return StreamingResponse(
                io.BytesIO(audio_data),
                media_type="audio/mpeg",
                headers={"Content-Length": str(len(audio_data))},
            )
        else:
            raise HTTPException(resp.status_code, f"Erro no TTS fallback: {resp.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
