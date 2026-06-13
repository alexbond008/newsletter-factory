# Newsletter Factory

A Telegram bot that turns voice notes into publish-ready newsletter drafts — written in Aleks Gornik's style, formatted for Kit.com, and delivered to my inbox in seconds.

## How it works

1. Send a voice note to [@newsletter_copywriter_bot](https://t.me/newsletter_copywriter_bot)
2. Gemini transcribes the audio and generates a full newsletter draft
3. The bot replies with the draft + 3 subject line options
4. Use `/push` to create a draft broadcast in Kit.com
5. Use `/send_draft` to receive a pixel-perfect inbox preview via email

```
Voice note → Transcription → Draft generation → Kit.com broadcast
```

## Features

- **Voice-to-newsletter** — send a voice note, get a full draft back in seconds
- **Style-faithful output** — generation is guided by a custom style guide (`factory-api/style_guide/ALEKS_STYLE.md`) that is read on every call, so quality tuning requires no redeployment
- **Feedback loop** — reply with plain text to regenerate with edits applied
- **Photo support** — send a photo with a caption; the model inserts it after the most contextually relevant paragraph
- **One-click publishing** — `/push` creates a Kit.com broadcast draft with subject lines pre-filled
- **Inbox preview** — `/send_draft` sends the draft as a real broadcast to a private preview tag for a true email-client render
- **Branded footer** — Aleks's P.S. coaching block and circular headshot are appended automatically on every draft

## Tech stack

| Layer | Technology |
|---|---|
| API server | FastAPI + Uvicorn |
| Primary LLM | Gemini 2.5 Flash (transcription + generation + editor pass) |
| Fallback LLM | Groq — Llama 3.3 70B (text) + Whisper large-v3 (audio) |
| Messaging | Telegram Bot API |
| Email platform | Kit.com API v4 |
| Deployment | Railway |
| Image storage | Railway persistent volume (`/data/uploads`) |

## Project structure

```
factory-api/
├── main.py                    # FastAPI app — entire pipeline lives here
├── requirements.txt
├── Dockerfile
├── style_guide/
│   └── ALEKS_STYLE.md         # Writing style guide — edit this to tune output quality
└── static/
    └── channel.png            # Static assets
```

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Transcription + generation (primary) |
| `GROQ_API_KEY` | Yes | Transcription + generation (fallback) |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot identity |
| `KIT_API_KEY` | Yes | Kit.com broadcast creation + preview send |
| `GEMINI_MODEL` | No | Override Gemini model ID (default: `gemini-2.5-flash`) |
| `KIT_PREVIEW_TAG` | No | Kit tag name for `/send_draft` (default: `Preview`) |
| `KIT_PREVIEW_EMAIL` | No | Email that receives previews (default: `aleksandergornik@gmail.com`) |
| `PUBLIC_BASE_URL` | No | Service's public URL for image hosting (defaults to Railway domain) |
| `YOUTUBE_URL` | No | Channel link in the sign-off footer |

## Local development

```bash
cd factory-api
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Env vars are loaded from `../.env` at the repo root.

Test generation without Telegram:

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"transcript": "your transcript here"}'
```

## Deployment

The app is deployed to Railway. After any code change:

```bash
# Verify no syntax errors before deploying
python3 -c "compile(open('factory-api/main.py').read(), 'main.py', 'exec')"

cd factory-api
railway up --detach
railway logs
```

If the Railway domain changes, re-register the Telegram webhook:

```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://<your-railway-domain>/webhook"
```

## Bot commands

| Command | Description |
|---|---|
| Send a voice note | Transcribe + generate a full draft |
| Send plain text | Regenerate with your feedback applied |
| Send a photo + caption | Host the image and insert it into the draft |
| `/push` | Create a draft broadcast in Kit.com |
| `/send_draft` | Send a real preview broadcast to your private preview tag |
| `/start` | Welcome message |

## Tuning output quality

Edit `factory-api/style_guide/ALEKS_STYLE.md`. Changes take effect immediately — no redeployment needed.
