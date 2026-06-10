import json
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

CRITICAL: The post MUST be grounded entirely in the specific stories, facts, numbers, and experiences from the transcript above. Do NOT invent new anecdotes or generic content. Every concrete detail in the draft must come directly from the transcript. If the transcript mentions Intel, Dyson, Cambridge Consultants, 15 applications, a bad CV — use those exact details. That specificity is what makes the post authentic.

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

# In-memory store: chat_id -> {"draft": ..., "transcript": ...}
_sessions: dict[int, dict] = {}


def get_groq() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    return Groq(api_key=api_key)


def parse_draft(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def transcribe_audio(audio_bytes: bytes) -> str:
    client = get_groq()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=("voice.ogg", f, "audio/ogg"),
                model="whisper-large-v3",
                response_format="text",
            )
        return transcription.strip()
    finally:
        os.unlink(tmp_path)


def generate_from_transcript(transcript: str, feedback: str = "") -> dict:
    style_guide = STYLE_GUIDE.read_text()
    feedback_section = f"\n\n## Feedback to incorporate\n{feedback}" if feedback else ""
    prompt = USER_PROMPT_TEMPLATE.format(
        style_guide=style_guide,
        transcript=transcript,
        feedback_section=feedback_section,
    )
    client = get_groq()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=4096,
    )
    return parse_draft(response.choices[0].message.content)


def tg_send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=30,
    )
    logger.info(f"tg_send status={r.status_code}")


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
    # Preserve paragraph breaks before stripping tags
    body = draft["body_html"]
    body = re.sub(r"</p>\s*<p>", "\n\n", body)
    body = re.sub(r"<p>|</p>", "", body)
    body = re.sub(r"<[^>]+>", "", body).strip()
    max_body = 3000
    if len(body) > max_body:
        body = body[:max_body] + "\n\n[truncated — send /push to get full draft in Kit.com]"
    return (
        f"<b>Subject line options:</b>\n{subjects}\n\n"
        f"<b>Preview text:</b>\n{draft['preview_text']}\n\n"
        f"<b>Draft:</b>\n\n{body}\n\n"
        f"───────────────\n"
        f"Reply with feedback to regenerate, or /push to create a Kit.com draft."
    )


async def handle_telegram_update(update: dict) -> None:
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = message.get("text", "")
    voice = message.get("voice") or message.get("audio")

    logger.info(f"update chat_id={chat_id} text={text!r} voice={bool(voice)}")

    if text == "/start":
        tg_send(chat_id, (
            "Welcome to Newsletter Factory!\n\n"
            "Send me a voice message and I'll turn it into a newsletter draft in your style.\n\n"
            "Commands:\n"
            "/push — create a draft in Kit.com\n"
            "/start — show this message"
        ))
        return

    if text == "/push":
        session = _sessions.get(chat_id)
        if not session or "draft" not in session:
            tg_send(chat_id, "No draft yet. Send a voice message first.")
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
        logger.info(f"kit status={r.status_code} body={r.text[:200]}")
        if r.status_code in (200, 201):
            broadcast_id = r.json().get("broadcast", {}).get("id")
            tg_send(chat_id, f"Draft created in Kit.com!\n\nhttps://app.kit.com/broadcasts/{broadcast_id}/edit")
        else:
            tg_send(chat_id, f"Kit.com error {r.status_code}: {r.text[:200]}")
        return

    if voice:
        tg_send(chat_id, "Got it! Transcribing and generating your draft... (20-40 seconds)")
        try:
            file_url = tg_get_file_url(voice["file_id"])
            audio_bytes = httpx.get(file_url, timeout=60).content
            logger.info(f"Downloaded audio: {len(audio_bytes)} bytes")
            transcript = transcribe_audio(audio_bytes)
            logger.info(f"Transcript: {transcript[:200]}")
            draft = generate_from_transcript(transcript)
            _sessions[chat_id] = {"draft": draft, "transcript": transcript}
            tg_send(chat_id, format_draft_message(draft))
        except Exception as e:
            logger.exception("Error processing voice message")
            tg_send(chat_id, f"Something went wrong: {str(e)[:300]}")
        return

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
            logger.exception("Error regenerating draft")
            tg_send(chat_id, f"Something went wrong: {str(e)[:300]}")
        return


# ── REST API ─────────────────────────────────────────────────────────────────

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
        logger.exception("Error in /generate")
        raise HTTPException(status_code=500, detail=str(e))
    return GenerateResponse(**data)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"webhook update: {json.dumps(update)[:200]}")
    try:
        await handle_telegram_update(update)
    except Exception as e:
        logger.exception("Unhandled error in webhook")
    return {"ok": True}
