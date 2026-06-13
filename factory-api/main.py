import base64
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _is_volume_mounted(path: Path) -> bool:
    try:
        mounts = Path("/proc/mounts").read_text()
        return any(str(path) in line for line in mounts.splitlines())
    except Exception:
        return False


def _pick_upload_dir() -> Path:
    """Where user-uploaded images live. Prefer the persistent Railway volume at
    /data (survives restarts/redeploys); fall back to a local dir (ephemeral —
    fine for testing, but images may break after a redeploy until the volume
    exists). Returns the first writable option."""
    candidates = [Path("/data") / "uploads", STATIC_DIR / "uploads"]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            if str(d).startswith("/data"):
                label = "persistent volume" if _is_volume_mounted(Path("/data")) else "EPHEMERAL /data (no volume mounted)"
            else:
                label = "EPHEMERAL local"
            logger.info(f"upload dir = {d} ({label})")
            return d
        except Exception:
            continue
    fallback = Path(tempfile.gettempdir()) / "uploads"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


UPLOAD_DIR = _pick_upload_dir()
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Public base URL of this service — used to build absolute <img> URLs that Kit
# (and email clients) can fetch. Override via env if the Railway domain changes.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://newsletter-api-production-fa3d.up.railway.app"
).rstrip("/")

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
- body_html: full post body wrapped in <p> tags, 600-900 words. Use SHORT paragraphs (1-2 sentences, often a single sentence), each in its own <p>, with lots of breathing room. End with the close paragraph — do NOT add any CTA, sign-off, P.S., or links; the system appends Aleks's footer automatically.
"""

# In-memory store: chat_id -> {"draft": ..., "transcript": ...}
_sessions: dict[int, dict] = {}

# Safety cap: /send_draft refuses to send if the preview tag has more subscribers
# than this, so a misconfigured tag can never blast the whole list.
PREVIEW_TAG_MAX_SUBSCRIBERS = 3

# The email /send_draft sends previews to. The bot auto-creates this subscriber
# and the preview tag if they don't exist. Must be an ACTIVE subscriber for Kit
# to actually deliver (unsubscribed/cancelled addresses won't receive broadcasts).
KIT_PREVIEW_EMAIL = os.environ.get("KIT_PREVIEW_EMAIL", "aleksandergornik@gmail.com")


def get_groq() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    return Groq(api_key=api_key)


# ── LLM provider: Gemini primary, Groq (Llama / Whisper) fallback ──────────────
# Gemini handles text better, so it's the default for generation, editing and
# transcription. If GEMINI_API_KEY is unset or any Gemini call fails, we fall
# back to Groq automatically so the bot keeps working.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"


def _gemini_request(parts: list, system: str | None, temperature: float, max_tokens: int) -> str:
    """Single-turn call to Gemini generateContent. Returns the concatenated text
    of the first candidate. Raises on HTTP error or no candidate so the caller
    can fall back to Groq."""
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            # Disable "thinking" — these are direct generation/transcription tasks,
            # and on 2.5 flash an unbounded thinking budget can silently consume
            # the whole output allowance and return an empty candidate.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    # Gemini intermittently returns transient 429/500/503s under load. Retry a few
    # times with backoff before letting the caller fall back to Groq, so we keep
    # generations on Gemini (better text quality) whenever possible.
    last_exc: Exception | None = None
    for attempt in range(3):
        r = httpx.post(
            f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json=body,
            timeout=120,
        )
        if r.status_code in (429, 500, 502, 503, 504):
            last_exc = httpx.HTTPStatusError(
                f"Gemini transient {r.status_code}", request=r.request, response=r
            )
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        cand = r.json()["candidates"][0]
        return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
    raise last_exc  # exhausted retries on transient errors


def llm_complete(system: str, user: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
    """Text completion. Prefer Gemini; fall back to Groq Llama on any failure."""
    if GEMINI_API_KEY:
        try:
            out = _gemini_request([{"text": user}], system, temperature, max_tokens)
            if out:
                return out
            logger.warning("Gemini returned empty text; falling back to Groq")
        except Exception:
            logger.exception("Gemini text generation failed; falling back to Groq")
    client = get_groq()
    resp = client.chat.completions.create(
        model=GROQ_TEXT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def parse_draft(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Strip ALL control characters the LLM sometimes embeds inside JSON string
    # values (including literal \n, \r, \t which are valid JSON whitespace at
    # the structural level but illegal inside strings). Replacing with space is
    # safe — JSON is whitespace-insensitive between tokens.
    raw = re.sub(r'[\x00-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


TRANSCRIBE_PROMPT = (
    "Transcribe this voice note verbatim in English. Return ONLY the transcript "
    "text — no preamble, no quotation marks, no commentary."
)


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe a voice note. Prefer Gemini (handles ogg/opus inline); fall back
    to Groq Whisper large-v3 on any failure."""
    if GEMINI_API_KEY:
        try:
            b64 = base64.b64encode(audio_bytes).decode()
            parts = [
                {"text": TRANSCRIBE_PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": b64}},
            ]
            out = _gemini_request(parts, None, 0, 8192)
            if out:
                return out
            logger.warning("Gemini transcription empty; falling back to Groq Whisper")
        except Exception:
            logger.exception("Gemini transcription failed; falling back to Groq Whisper")
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


EDITOR_PROMPT = """You are a professional editor reviewing a newsletter draft. Fix the following issues — nothing else:

1. Grammar and spelling errors.
2. Remove filler phrases: "I mean", "right?", "you know", "sort of", "kind of", "basically", "I'm not saying X, I'm saying Y" constructions unless they add true value to the message.
3. Break up any sequence of 3+ long sentences (20+ words each) — insert a short punchy sentence.
4. Remove any sentence that sounds like AI or a life coach: "we've all been there", "you're not alone", "it's worth it", "believe in yourself".
5. Preserve all HTML <p> tags exactly. Do not add or remove any tags.
6. Do not change the meaning, stories, structure, or sign-off. Only fix the issues above.

Return the corrected body_html only — no JSON wrapper, no explanation, just the HTML."""


def editor_pass(draft: dict) -> dict:
    corrected_html = llm_complete(EDITOR_PROMPT, draft["body_html"], temperature=0.2, max_tokens=8192)
    # Strip any accidental markdown fences
    if corrected_html.startswith("```"):
        corrected_html = corrected_html.split("```")[1]
        if corrected_html.startswith("html"):
            corrected_html = corrected_html[4:]
        corrected_html = corrected_html.strip()
    return {**draft, "body_html": corrected_html}


def generate_from_transcript(transcript: str, feedback: str = "") -> dict:
    style_guide = STYLE_GUIDE.read_text()
    feedback_section = f"\n\n## Feedback to incorporate\n{feedback}" if feedback else ""
    prompt = USER_PROMPT_TEMPLATE.format(
        style_guide=style_guide,
        transcript=transcript,
        feedback_section=feedback_section,
    )
    raw = llm_complete(SYSTEM_PROMPT, prompt, temperature=0.7, max_tokens=8192)
    draft = parse_draft(raw)
    return editor_pass(draft)


IMG_PLACE_PROMPT = """You decide where to place ONE image inside a newsletter draft.

You get the draft's paragraphs (numbered) and a short caption describing the image.
Pick the single best spot so the image sits right next to the text it relates to.

Reply with ONLY a number — nothing else:
- 0  => place the image at the very top, before paragraph 1
- N  => place the image immediately AFTER paragraph N
"""


def image_block(url: str, caption: str) -> str:
    alt = (caption or "image").replace('"', "'")
    caption_html = (
        f'<p style="text-align:center;font-size:14px;color:#666;margin-top:4px;">{caption}</p>'
        if caption else ""
    )
    return (
        f'<p style="text-align:center;">'
        f'<img src="{url}" alt="{alt}" style="display:inline-block;max-width:55%;width:320px;height:auto;" />'
        f"</p>"
        f"{caption_html}"
    )


def choose_image_position(paragraphs: list[str], caption: str) -> int:
    """Ask the model which paragraph the image belongs after. Returns 0..len(paragraphs)
    (0 = top, N = after paragraph N). Falls back to end on any parsing trouble."""
    numbered = "\n".join(
        f"{i + 1}. {re.sub(r'<[^>]+>', '', p).strip()}" for i, p in enumerate(paragraphs)
    )
    user = f"Caption: {caption or '(no caption given)'}\n\nParagraphs:\n{numbered}"
    try:
        out = llm_complete(IMG_PLACE_PROMPT, user, temperature=0, max_tokens=16)
        m = re.search(r"\d+", out or "")
        n = int(m.group()) if m else len(paragraphs)
    except Exception:
        logger.exception("image position selection failed; appending at end")
        n = len(paragraphs)
    return max(0, min(n, len(paragraphs)))


def insert_image_into_draft(body_html: str, url: str, caption: str) -> str:
    """Insert an image block at the most contextually relevant spot in the body."""
    paragraphs = re.findall(r"<p\b[^>]*>.*?</p>", body_html, flags=re.DOTALL | re.IGNORECASE)
    block = image_block(url, caption)
    if not paragraphs:
        return block + body_html
    pos = choose_image_position(paragraphs, caption)
    if pos == 0:
        return block + body_html
    anchor = paragraphs[pos - 1]
    idx = body_html.find(anchor) + len(anchor)
    return body_html[:idx] + block + body_html[idx:]


def apply_images(body_html: str, images: list[dict]) -> str:
    """Re-insert every image the user has added this session into a (freshly
    regenerated) draft body, so feedback edits don't wipe previously added
    images. Each image is {"url": ..., "caption": ...}."""
    for img in images:
        body_html = insert_image_into_draft(body_html, img["url"], img.get("caption", ""))
    return body_html


# Aleks always ends every newsletter with two Kit "reusable snippets":
#   1. "P.S. Coaching" — a P.S. offering 1:1 coaching with a Calendly link
#   2. "pic + Channel" — circular headshot from Kit CDN linking to YouTube
# Replicated from the Kit snippet editor HTML (exact styles preserved).
YOUTUBE_URL = os.environ.get("YOUTUBE_URL", "https://www.youtube.com/@aleksgornik")
COACHING_CALENDLY_URL = "https://calendly.com/aleksgornikmedia/strategy-call-with-aleks"
KIT_PROFILE_IMG = "https://embed.filekitcdn.com/e/ryjdbMCD8h44uP8HMNqBwC/4QqXGRzzAXGnbj3Uf5mUSx/email"


def signature_footer() -> str:
    coaching_link = f'<a href="{COACHING_CALENDLY_URL}">{COACHING_CALENDLY_URL}</a>'
    channel_link = f'<a href="{YOUTUBE_URL}" style="color:rgb(0,0,255);">@aleksgornik</a>'
    return (
        "<p>P.S.</p>"
        "<p>I'm planning to run 1-on-1 coaching with a select few students to help you "
        "navigate university and land those internship / grad roles.</p>"
        f"<p>If you're interested, book a call with me here at {coaching_link}.</p>"
        f'<p style="text-align:center;margin:20px 0 4px;">'
        f'<a href="{YOUTUBE_URL}" style="text-decoration:none;">'
        f'<img src="{KIT_PROFILE_IMG}" width="181" height="181"'
        f' style="border-radius:9999px;width:181px;height:auto;display:inline-block;object-fit:contain;"'
        f' alt="Aleks Gornik" /></a></p>'
        f'<p style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:18px;'
        f'color:rgb(53,53,53);font-weight:400;line-height:1.5;text-align:center;margin:4px 0 20px;">'
        f"My youtube channel : {channel_link}</p>"
    )


def build_email_html(draft: dict) -> str:
    """Assemble the full email body: the generated post + the standing signature footer."""
    return draft["body_html"].rstrip() + signature_footer()


def create_kit_broadcast(kit_key: str, draft: dict) -> int | None:
    """Create a draft broadcast in Kit.com (v4). Returns the broadcast id, or None on error.

    Kit v4 expects the broadcast fields at the TOP LEVEL of the JSON body — NOT
    nested under a "broadcast" key. Nesting them makes Kit ignore every field and
    silently create a blank broadcast (which then 404s in the web editor).
    """
    subject = draft["subject_lines"][0]
    r = httpx.post(
        "https://api.kit.com/v4/broadcasts",
        headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
        json={
            "subject": subject,
            "description": subject,  # internal name shown in the Kit broadcast list
            "preview_text": draft["preview_text"],
            "content": build_email_html(draft),
            "public": False,         # keep it a private draft, don't publish to the web feed
            "send_at": None,         # null => save as draft (do not schedule/send)
        },
        timeout=30,
    )
    logger.info(f"kit create status={r.status_code} body={r.text[:300]}")
    if r.status_code in (200, 201):
        return r.json().get("broadcast", {}).get("id")
    return None


def find_kit_tag(kit_key: str, name: str) -> dict | None:
    """Look up a Kit tag by (case-insensitive) name. Returns the tag dict or None."""
    r = httpx.get(
        "https://api.kit.com/v4/tags",
        headers={"X-Kit-Api-Key": kit_key},
        params={"per_page": 1000},
        timeout=30,
    )
    logger.info(f"kit list tags status={r.status_code}")
    if r.status_code != 200:
        return None
    for tag in r.json().get("tags", []):
        if tag.get("name", "").strip().lower() == name.strip().lower():
            return tag
    return None


def find_or_create_kit_tag(kit_key: str, name: str) -> dict | None:
    """Return the Kit tag with this name, creating it if it doesn't exist."""
    tag = find_kit_tag(kit_key, name)
    if tag:
        return tag
    r = httpx.post(
        "https://api.kit.com/v4/tags",
        headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
        json={"name": name},
        timeout=30,
    )
    logger.info(f"kit create tag status={r.status_code} body={r.text[:200]}")
    if r.status_code in (200, 201):
        return r.json().get("tag")
    return None


def find_or_create_kit_subscriber(kit_key: str, email: str) -> dict | None:
    """Return the Kit subscriber for this email, creating it if needed. The returned
    dict includes "state" — broadcasts only deliver to an "active" subscriber."""
    r = httpx.get(
        "https://api.kit.com/v4/subscribers",
        headers={"X-Kit-Api-Key": kit_key},
        params={"email_address": email},
        timeout=30,
    )
    if r.status_code == 200:
        subs = r.json().get("subscribers", [])
        if subs:
            return subs[0]
    r = httpx.post(
        "https://api.kit.com/v4/subscribers",
        headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
        json={"email_address": email},
        timeout=30,
    )
    logger.info(f"kit create subscriber status={r.status_code} body={r.text[:200]}")
    if r.status_code in (200, 201):
        return r.json().get("subscriber")
    return None


def tag_kit_subscriber(kit_key: str, tag_id: int, subscriber_id: int) -> bool:
    """Add a subscriber to a tag. Idempotent on Kit's side."""
    r = httpx.post(
        f"https://api.kit.com/v4/tags/{tag_id}/subscribers/{subscriber_id}",
        headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
        timeout=30,
    )
    logger.info(f"kit tag subscriber status={r.status_code}")
    return r.status_code in (200, 201)


def send_kit_preview(kit_key: str, draft: dict, tag_id: int) -> int | None:
    """Send the draft as a real broadcast to ONLY the given tag (a one-person preview
    audience), scheduled for now. Returns the broadcast id, or None on error.

    Same top-level-fields rule as create_kit_broadcast. The subscriber_filter restricts
    the audience to the tag; send_at set to now triggers the actual send.
    """
    subject = draft["subject_lines"][0]
    now_iso = datetime.now(timezone.utc).isoformat()
    r = httpx.post(
        "https://api.kit.com/v4/broadcasts",
        headers={"X-Kit-Api-Key": kit_key, "Content-Type": "application/json"},
        json={
            "subject": f"[PREVIEW] {subject}",
            "description": f"[PREVIEW] {subject}",
            "preview_text": draft["preview_text"],
            "content": build_email_html(draft),
            "public": False,
            "send_at": now_iso,
            "subscriber_filter": [
                {"all": [{"type": "tag", "ids": [tag_id]}], "any": None, "none": None}
            ],
        },
        timeout=30,
    )
    logger.info(f"kit preview send status={r.status_code} body={r.text[:300]}")
    if r.status_code in (200, 201):
        return r.json().get("broadcast", {}).get("id")
    return None


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
    body = draft["body_html"]
    # Show image placeholders so the user can see where images landed
    body = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"🖼 [image: \1]", body, flags=re.IGNORECASE)
    body = re.sub(r"<img[^>]*>", "🖼 [image]", body, flags=re.IGNORECASE)
    # Preserve paragraph breaks (handles <p> with attributes) before stripping tags
    body = re.sub(r"</p>\s*<p\b[^>]*>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", "", body).strip()
    max_body = 3000
    if len(body) > max_body:
        body = body[:max_body] + "\n\n[truncated — send /push to get full draft in Kit.com]"
    return (
        f"<b>Subject line options:</b>\n{subjects}\n\n"
        f"<b>Preview text:</b>\n{draft['preview_text']}\n\n"
        f"<b>Draft:</b>\n\n{body}\n\n"
        f"───────────────\n"
        f"Reply with feedback to regenerate, send a photo (with a caption) to add an image, "
        f"/send_draft to preview it in your inbox, or /push to create a Kit.com draft."
    )


async def handle_telegram_update(update: dict) -> None:
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = message.get("text", "")
    voice = message.get("voice") or message.get("audio")
    photo = message.get("photo")

    logger.info(f"update chat_id={chat_id} text={text!r} voice={bool(voice)} photo={bool(photo)}")

    if text == "/start":
        tg_send(chat_id, (
            "Welcome to Newsletter Factory!\n\n"
            "Send me a voice message and I'll turn it into a newsletter draft in your style.\n"
            "Reply with text to refine it. Send a photo (with a caption describing it) "
            "and I'll drop it into the draft at the most relevant spot.\n\n"
            "Commands:\n"
            "/send_draft — email yourself a preview of the current draft\n"
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
        broadcast_id = create_kit_broadcast(kit_key, draft)
        if broadcast_id:
            tg_send(chat_id, f"Draft created in Kit.com!\n\nhttps://app.kit.com/campaigns/{broadcast_id}/draft")
        else:
            tg_send(chat_id, "Kit.com error creating the draft. Check the logs.")
        return

    if text == "/send_draft":
        session = _sessions.get(chat_id)
        if not session or "draft" not in session:
            tg_send(chat_id, "No draft yet. Send a voice message first.")
            return
        draft = session["draft"]
        kit_key = os.environ.get("KIT_API_KEY")
        if not kit_key:
            tg_send(chat_id, "KIT_API_KEY not configured.")
            return
        tag_name = os.environ.get("KIT_PREVIEW_TAG", "Preview")
        # Auto-provision: find-or-create the preview tag, ensure the preview email
        # exists as a subscriber and is tagged. This is what makes /send_draft work
        # out of the box instead of failing on a missing tag.
        tag = find_or_create_kit_tag(kit_key, tag_name)
        if not tag:
            tg_send(chat_id, f"Couldn't create or find the \"{tag_name}\" tag in Kit. Check the logs.")
            return
        # Safety stop only when the tag ALREADY has many subscribers (a misconfigured
        # tag pointing at the whole list). A freshly created tag reports no count.
        count = tag.get("subscriber_count") or 0
        if count > PREVIEW_TAG_MAX_SUBSCRIBERS:
            tg_send(chat_id, (
                f"Safety stop: the \"{tag_name}\" tag has {count} subscribers. "
                f"A preview should go to just you. Trim that tag to {PREVIEW_TAG_MAX_SUBSCRIBERS} "
                f"or fewer before using /send_draft, so this never blasts your whole list."
            ))
            return
        subscriber = find_or_create_kit_subscriber(kit_key, KIT_PREVIEW_EMAIL)
        if not subscriber:
            tg_send(chat_id, f"Couldn't set up the preview subscriber ({KIT_PREVIEW_EMAIL}) in Kit. Check the logs.")
            return
        tag_kit_subscriber(kit_key, tag["id"], subscriber["id"])
        # Kit only delivers broadcasts to ACTIVE subscribers. If the preview email is
        # unsubscribed/cancelled, the send "succeeds" but never lands — warn instead.
        state = (subscriber.get("state") or "").lower()
        if state and state != "active":
            tg_send(chat_id, (
                f"Your preview email ({KIT_PREVIEW_EMAIL}) is \"{state}\" in Kit, so Kit won't "
                f"deliver previews to it. Reactivate it in Kit (Subscribers → set to active / resubscribe), "
                f"or set KIT_PREVIEW_EMAIL to an active address, then try /send_draft again."
            ))
            return
        tg_send(chat_id, f"Sending a preview to {KIT_PREVIEW_EMAIL}... check your inbox shortly.")
        broadcast_id = send_kit_preview(kit_key, draft, tag["id"])
        if broadcast_id:
            tg_send(chat_id, "Preview scheduled to send now. It should land in your inbox within a minute or two.")
        else:
            tg_send(chat_id, "Kit.com error sending the preview. Check the logs.")
        return

    if photo:
        session = _sessions.get(chat_id)
        if not session or "draft" not in session:
            tg_send(chat_id, "Send a voice note first to create a draft, then send a photo (with a caption describing it) to add it.")
            return
        caption = message.get("caption", "")
        tg_send(chat_id, "Adding your image to the draft...")
        try:
            file_id = photo[-1]["file_id"]  # last entry is the largest size
            file_url = tg_get_file_url(file_id)
            img_bytes = httpx.get(file_url, timeout=60).content
            fname = f"{uuid.uuid4().hex}.jpg"
            (UPLOAD_DIR / fname).write_bytes(img_bytes)
            public_url = f"{PUBLIC_BASE_URL}/uploads/{fname}"
            logger.info(f"saved image {fname} ({len(img_bytes)} bytes) caption={caption!r}")
            draft = session["draft"]
            draft["body_html"] = insert_image_into_draft(draft["body_html"], public_url, caption)
            # Remember the image so a later text-feedback regeneration re-applies it
            # instead of wiping it.
            session.setdefault("images", []).append({"url": public_url, "caption": caption})
            _sessions[chat_id]["draft"] = draft
            tg_send(chat_id, format_draft_message(draft))
        except Exception as e:
            logger.exception("Error adding image")
            tg_send(chat_id, f"Couldn't add the image: {str(e)[:300]}")
        return

    if voice:
        tg_send(chat_id, "Got it! Transcribing and generating your draft... (20-40 seconds)")
        try:
            file_url = tg_get_file_url(voice["file_id"])
            audio_bytes = httpx.get(file_url, timeout=60).content
            mime_type = voice.get("mime_type", "audio/ogg")
            logger.info(f"Downloaded audio: {len(audio_bytes)} bytes ({mime_type})")
            transcript = transcribe_audio(audio_bytes, mime_type)
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
            # Re-apply any images added this session so feedback doesn't wipe them.
            draft["body_html"] = apply_images(draft["body_html"], session.get("images", []))
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
