# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Telegram bot that converts voice notes into publish-ready newsletter drafts in Aleks Gornik's writing style. The bot is deployed on Railway.

**Flow:** User sends voice message to Telegram bot → Groq Whisper transcribes audio → Llama 3.3 70B generates newsletter draft using the style guide → bot replies with draft + 3 subject lines → `/push` creates a Kit.com broadcast draft.

## Deployment

The entire app lives in `factory-api/` and is deployed to Railway via the Railway CLI:

```bash
cd factory-api
railway up --detach   # deploy (non-blocking)
railway logs          # tail live logs
```

The service is linked to Railway project `delightful-luck`, service `newsletter-api`. After any deploy that changes env vars, Railway auto-redeploys — no manual trigger needed.

After deploying, re-register the Telegram webhook if the domain changes:
```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://newsletter-api-production-fa3d.up.railway.app/webhook"
```

## Local development

```bash
cd factory-api
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Env vars are loaded from `../.env` (root of repo) — copy them into your shell or use `python-dotenv`. The `/health` endpoint confirms the server is up.

To test generation without Telegram:
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"transcript": "your transcript here"}'
```

## Architecture

Single file: `factory-api/main.py`. No database — session state (last draft + transcript per chat ID) is held in the `_sessions` dict in memory. A Railway restart clears sessions, which is fine since users just re-send a voice note.

**Key functions:**
- `transcribe_audio()` — Groq Whisper large-v3, saves audio to a tempfile, returns plain text
- `generate_from_transcript()` — injects style guide + transcript into Llama 3.3 70B, returns parsed JSON `{subject_lines, preview_text, body_html}`
- `handle_telegram_update()` — routes incoming updates: voice → transcribe+generate, text → feedback regeneration, `/push` → Kit.com API
- `format_draft_message()` — strips HTML tags with paragraph breaks preserved for readable Telegram display

## The style guide

`factory-api/style_guide/ALEKS_STYLE.md` is the most important file for output quality. It is read from disk on every generation call (no caching), so edits take effect immediately without redeployment. When output quality drifts, edit this file first before touching the prompt.

The generation prompt enforces that all concrete details (names, numbers, stories) must come from the transcript — do not remove this constraint.

## Environment variables

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Transcription (Whisper) + generation (Llama) |
| `TELEGRAM_BOT_TOKEN` | Bot: `@newsletter_copywriter_bot` |
| `KIT_API_KEY` | Kit.com API v4 for `/push` |
| `GEMINI_API_KEY` | Not currently used (was original LLM, switched to Groq) |

## Kit.com API

Uses v4: `POST https://api.kit.com/v4/broadcasts` with `X-Kit-Api-Key` header. Setting `send_at: null` creates a draft. Broadcast edit URL: `https://app.kit.com/broadcasts/{id}/edit`.


