# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Telegram bot that converts voice notes into publish-ready newsletter drafts in Aleks Gornik's writing style. The bot is deployed on Railway.

**Flow:** User sends voice message to Telegram bot → Gemini transcribes audio → Gemini generates newsletter draft using the style guide → bot replies with draft + 3 subject lines → `/push` creates a Kit.com broadcast draft.

**LLM provider:** Gemini (`gemini-2.5-flash` by default) is primary for transcription, generation, the editor pass, and image placement — it handles text better. Groq (Llama 3.3 70B / Whisper large-v3) is the automatic fallback if `GEMINI_API_KEY` is unset or any Gemini call fails (incl. transient 429/5xx, which `_gemini_request` retries 3× before giving up). All routing goes through `llm_complete()` and `transcribe_audio()`.

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
| `GEMINI_API_KEY` | PRIMARY: transcription + generation + editor + image placement (Gemini) |
| `GEMINI_MODEL` | (optional) Gemini model id. Default `gemini-2.5-flash` |
| `GROQ_API_KEY` | FALLBACK: transcription (Whisper) + generation (Llama) when Gemini is unset/fails |
| `TELEGRAM_BOT_TOKEN` | Bot: `@newsletter_copywriter_bot` |
| `KIT_API_KEY` | Kit.com API v4 for `/push` and `/send_draft` |
| `KIT_PREVIEW_TAG` | (optional) Kit tag name `/send_draft` sends previews to. Default `Preview` |
| `KIT_PREVIEW_EMAIL` | (optional) Email `/send_draft` previews go to; auto-created + tagged. Default `aleksandergornik@gmail.com` (active subscriber) |
| `PUBLIC_BASE_URL` | (optional) This service's public URL, used to build absolute `<img>` URLs. Defaults to the Railway domain |
| `YOUTUBE_URL` | (optional) Channel link for the auto-appended sign-off. Default `https://www.youtube.com/@aleksgornik` |

## Kit.com API

Uses v4: `POST https://api.kit.com/v4/broadcasts` with `X-Kit-Api-Key` header. **Broadcast fields must be TOP-LEVEL in the JSON body — NOT nested under a `broadcast` key.** Nesting makes Kit ignore every field and silently create a blank broadcast. Setting `send_at: null` creates a draft. The working web URL is `https://app.kit.com/campaigns/{id}/draft` (NOT `/broadcasts/{id}/edit`, which 404s).

## Session 2026-06-12 (part 2)

**Status**: `/push` fixed + 3 new features shipped & deployed. One infra step (volume) blocked pending user authorization.

**Work**: (1) Fixed the empty-broadcast `/push` bug — Kit v4 wants broadcast fields top-level, not wrapped in `{"broadcast": {...}}`; the wrapper made Kit ignore everything and create a blank draft. Also fixed the web link to `/campaigns/{id}/draft`. (2) Added `/send_draft` — sends the draft as a real broadcast to a one-person Kit tag (default `Preview`, looked up by name) for a pixel-perfect inbox preview; refuses if that tag has > `PREVIEW_TAG_MAX_SUBSCRIBERS` (3) subscribers as an anti-blast guard. (3) Made paragraphs airier (style guide + prompts now demand 1–2 sentence paragraphs; LLM no longer writes a CTA). (4) Auto-append Aleks's sign-off (the `pic + Channel` + `P.S. Coaching` Kit snippets, baked as static HTML since the API can't reference snippets; channel image served from `/static/channel.png`). (5) Photo support — send a photo with a caption; image is hosted and the LLM inserts it after the most relevant paragraph (`insert_image_into_draft`). New `/static` + `/uploads` StaticFiles mounts.

**Key decisions**: Kit has NO test-email API, so "preview" = a real send to a self-only tag (user chose this over Resend/SMTP for truest fidelity). Snippets baked as static HTML (only way via API). Image placement = caption-driven auto-placement (model picks the paragraph). Images stored under `/data/uploads` (volume path) with fallback to local `static/uploads`.

**Remaining**: (1) **Create the Railway volume**: `railway volume add --service newsletter-api --mount-path /data` — blocked by the prod-infra safety classifier; needs user OK. Until then `/data` is EPHEMERAL (no volume exists) and images break on redeploy. (2) Confirm with user: YouTube URL `@aleksgornik` and sign-off order. (3) Cleanup for next deploy: `_pick_upload_dir` logs "persistent volume" for any `/data` path even with no volume attached — make the label honest (e.g. check `/proc/mounts`).

**Gotchas**: Starlette `StaticFiles` 404s route through the app's JSON exception handler, so a missing `/uploads/x` returns `application/json` `{"detail":"Not Found"}` — you CANNOT distinguish a live StaticFiles mount from an unmounted route via content-type (a deploy-detection probe failed because of this; use `railway logs` instead). Adding an image then sending TEXT feedback regenerates the draft and WIPES inserted images — add photos last. Sessions are in-memory (cleared on redeploy).

## Session 2026-06-13

**Status**: All features from prior session shipped and stable. Several bugs fixed, footer redesigned.

**Work**: (1) **Railway volume created** — `newsletter-api-volume` mounted at `/data`; images now persist across redeploys. `_pick_upload_dir` now checks `/proc/mounts` to log honestly whether a real volume is attached. (2) **JSON control character fix** — Llama sometimes embeds literal control chars (including `\x0a` newline) inside JSON string values; `parse_draft()` now strips the full `[\x00-\x1f\x7f]` range before `json.loads()`. Earlier fix only stripped `[\x00-\x08\x0b\x0c\x0e-\x1f]`, missing newlines inside strings. (3) **Footer redesigned** — replaced the 1924×556 banner (`channel.png`) with an exact replica of the Kit "pic + Channel" snippet: 181px circular headshot from Kit's own CDN (`embed.filekitcdn.com/e/ryjdbMCD8h44uP8HMNqBwC/4QqXGRzzAXGnbj3Uf5mUSx/email`), same 18px font, same colors as the Kit editor. Footer order is now: P.S. coaching block first, then circular pic + channel link at the bottom. (4) **User images sized down** — capped at `320px` wide (was full-width). (5) **Image captions** — Telegram photo caption now appears below the inserted image in the email as small grey text (14px, `#666`). (6) **Smart-quote SyntaxError** — edit tooling introduced curly apostrophes (`'` U+2019) as Python string delimiters, crashing the app on deploy. Fixed with a bulk byte-level replacement; `python3 -c "compile(...)"` is now used to gate every deploy.

**Key decisions**: Kit snippet HTML was reverse-engineered from the Kit editor DOM (saved as `static/snippet.html`). Profile image URL from Kit CDN is hardcoded as `KIT_PROFILE_IMG` — it's Aleks's Kit account headshot, not the local `channel.png`.

**Remaining**: `channel.png` is now unused in the footer (superseded by Kit CDN image) but still served at `/static/channel.png` — can be deleted or repurposed. No other known blockers.

**Gotchas**: Always run `python3 -c "compile(open('main.py').read(), 'main.py', 'exec')"` before `railway up` — the Edit tool has injected curly quotes as string delimiters twice now. The Kit CDN image URL (`KIT_PROFILE_IMG`) is tied to Aleks's Kit account; if the account/image changes it'll silently break. `static/snippet.html` and `static/broadcast.html` are large dev artefacts (635KB each) — they should not be deployed but currently are (no `.railwayignore`).

## Session 2026-06-13 (part 2)

**Status**: All three reported bugs fixed, deployed (Railway deploy `5a2f0563`), and pushed to GitHub. Verified live in prod.

**Work**: (1) **Gemini is now the primary LLM**, Groq the fallback. New `_gemini_request()` (REST via httpx, no SDK) + `llm_complete()` route generation, the editor pass, and image placement through Gemini `gemini-2.5-flash`; `transcribe_audio()` sends the voice note inline to Gemini (ogg/opus works). Any Gemini failure (incl. transient 429/5xx — retried 3× with backoff) falls back to Groq Llama/Whisper automatically. Confirmed in prod logs: Gemini calls succeed, and the fallback fired correctly on a real 503. (2) **Image persistence on regenerate** — added photos are stored on the session (`session["images"]`) and re-applied via `apply_images()` after text-feedback regeneration, so edits no longer wipe inserted images. (3) **`/send_draft` auto-provisions** — find-or-create the Preview tag + the preview subscriber, tag it, then send; refuses with a clear message if the preview email isn't an `active` Kit subscriber (Kit won't deliver to cancelled/unsubscribed). `KIT_PREVIEW_EMAIL` now defaults to `aleksandergornik@gmail.com` (an active subscriber); set in Railway too.

**Key decisions**: Gemini chosen for text quality (user's call); kept Groq as zero-config fallback so the bot never hard-fails. `_gemini_request` sets `thinkingConfig.thinkingBudget=0` — without it, 2.5-flash's thinking can eat the whole output-token budget and return an empty candidate. Auto-provisioning approved by user over dropping `/send_draft`, because the self-tag send goes through Kit's real pipeline (maximally representative, better than a simulated preview).

**Remaining**: User to confirm the live Telegram round-trip (`/send_draft` → inbox at aleksandergornik@gmail.com). The old default email `aleksgornikmedia@gmail.com` is `cancelled` in Kit — left as-is. Safety cap in `/send_draft` is effectively inert (Kit's tags-list response has no `subscriber_count`), but blast risk is nil since the bot owns the Preview tag and only tags one email.

**Gotchas**: The Gemini API key appears in the httpx request-URL log line (query param) in Railway logs. Setting Railway vars via the CLI needs proper header quoting and works with `--skip-deploys`; the MCP railway tools are NOT authenticated (use the CLI). Reading prod env vars and `railway up` are gated by the auto-mode classifier — deploys need explicit user OK. The repo does NOT auto-deploy from GitHub; a push does nothing until `railway up`.


