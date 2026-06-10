import json
import os
import tempfile
from pathlib import Path

import google.generativeai as genai
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Newsletter Factory API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STYLE_GUIDE = Path(__file__).parent / "style_guide" / "ALEKS_STYLE.md"

SYSTEM_PROMPT = """You are a ghostwriter for Aleks Gornik's email newsletter.
Your job is to turn a voice note transcript into a polished newsletter post
that sounds exactly like Aleks — not like AI, not like a writing coach,
not like a LinkedIn post. Study the style guide carefully before writing.
Return only valid JSON with no markdown fences."""

USER_PROMPT_TEMPLATE = """
## Style Guide
{style_guide}

## Voice Note Transcript
{transcript}
{feedback_section}

## Task
Generate a newsletter post in Aleks's exact style.

Return valid JSON only (no markdown fences, no extra text):
{{
  "subject_lines": ["...", "...", "..."],
  "preview_text": "...",
  "body_html": "..."
}}

- subject_lines: exactly 3 options following the formula in the style guide
- preview_text: 1 sentence shown in email inbox previews
- body_html: full post body wrapped in <p> tags, 600-900 words, CTA stack at end
"""

# In-memory store: chat_id -> last draft (for /push) and transcript (for feedback regeneration)
_sessions: dict[int, dict] = {}


def get_gemini_model() -> genai.GenerativeModel:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
    )


def parse_draft(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def generate_from_transcript(transcript: str, feedback: str = "") -> dict:
    style_guide = STYLE_GUIDE.read_text()
    feedback_section = f"\n\n## Feedback to incorporate\n{feedback}" if feedback else ""
    prompt = USER_PROMPT_TEMPLATE.format(
        style_guide=style_guide,
        transcript=transcript,
        feedback_section=feedback_section,
    )
    model = get_gemini_model()
    response = model.generate_content(prompt)
    return parse_draft(response.text)


def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Upload audio to Gemini and get back a clean transcript."""
    api_key = os.environ.get("GEMINI_API_KEY")
    genai.configure(api_key=api_key)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    uploaded = genai.upload_file(tmp_path, mime_type=mime_type)
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content([
        uploaded,
        "Transcribe this audio accurately. Return only the transcription text, nothing else."
    ])
    os.unlink(tmp_path)
    return response.text.strip()


def tg_send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=30,
    )


def tg_get_file_url(file_id: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.get(
        f"https://api.telegram.org/bot{token}/getFile",
        params={"file_id": file_id},
        timeout=10,
    )
    file_path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"


def format_draft_message(draft: dict) -> str:
    subjects = "\n".join(f"{i+1}. {s}" for i, s in enumerate(draft["subject_lines"]))
    # Strip HTML tags for Telegram display
    import re
    body_plain = re.sub(r"<[^>]+>", "", draft["body_html"]).strip()
    return (
        f"<b>Subject line options:</b>\n{subjects}\n\n"
        f"<b>Preview text:</b>\n{draft['preview_text']}\n\n"
        f"<b>Draft:</b>\n\n{body_plain}\n\n"
        f"---\n"
        f"Reply with feedback to regenerate, or send /push to create a Kit.com draft."
    )


async def handle_telegram_update(update: dict) -> None:
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = message.get("text", "")
    voice = message.get("voice") or message.get("audio")

    # /start
    if text == "/start":
        tg_send(chat_id, (
            "👋 Welcome to Newsletter Factory!\n\n"
            "Send me a voice message and I'll turn it into a newsletter draft in your style.\n\n"
            "Commands:\n"
            "/push — create a draft in Kit.com from the last generation\n"
            "/start — show this message"
        ))
        return

    # /push — create Kit.com draft from last generation
    if text == "/push":
        session = _sessions.get(chat_id)
        if not session or "draft" not in session:
            tg_send(chat_id, "No draft to push yet. Send a voice message first.")
            return
        draft = session["draft"]
        kit_key = os.environ.get("KIT_API_KEY")
        if not kit_key:
            tg_send(chat_id, "KIT_API_KEY not configured.")
            return
        r = httpx.post(
            "https://api.kit.com/v4/broadcasts",
            headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
            json={"broadcast": {
                "subject": draft["subject_lines"][0],
                "preview_text": draft["preview_text"],
                "content": draft["body_html"],
                "send_at": None,
            }},
            timeout=30,
        )
        if r.status_code in (200, 201):
            broadcast_id = r.json().get("broadcast", {}).get("id")
            tg_send(chat_id, f"✅ Draft created in Kit.com!\n\nhttps://app.kit.com/broadcasts/{broadcast_id}/edit")
        else:
            tg_send(chat_id, f"Kit.com error: {r.status_code} — {r.text[:200]}")
        return

    # Voice message — transcribe + generate
    if voice:
        tg_send(chat_id, "Got it! Transcribing and generating your draft... (30–60 seconds)")
        file_url = tg_get_file_url(voice["file_id"])
        audio_bytes = httpx.get(file_url, timeout=60).content
        try:
            transcript = transcribe_audio(audio_bytes, "audio/ogg")
            draft = generate_from_transcript(transcript)
            _sessions[chat_id] = {"draft": draft, "transcript": transcript}
            tg_send(chat_id, format_draft_message(draft))
        except Exception as e:
            tg_send(chat_id, f"Something went wrong: {str(e)[:200]}")
        return

    # Text feedback — regenerate from last transcript
    if text and not text.startswith("/"):
        session = _sessions.get(chat_id)
        if not session or "transcript" not in session:
            tg_send(chat_id, "Send a voice message first, then reply with feedback to refine it.")
            return
        tg_send(chat_id, "Regenerating with your feedback...")
        try:
            draft = generate_from_transcript(session["transcript"], feedback=text)
            _sessions[chat_id]["draft"] = draft
            tg_send(chat_id, format_draft_message(draft))
        except Exception as e:
            tg_send(chat_id, f"Something went wrong: {str(e)[:200]}")
        return


# ── REST API (kept for testing) ──────────────────────────────────────────────

class GenerateRequest(BaseModel):
    transcript: str
    feedback: str = ""


class GenerateResponse(BaseModel):
    subject_lines: list[str]
    preview_text: str
    body_html: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    try:
        data = generate_from_transcript(req.transcript, req.feedback)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return GenerateResponse(**data)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await handle_telegram_update(update)
    return {"ok": True}
