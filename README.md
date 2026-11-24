# LifeOS — Autonomous AI Life Planner

LifeOS is an autonomous, single-user life planning and capture backend combining 3-tier Gemini/Gemma routing, multimodal `gemini-embedding-2` RAG, an adaptive Goldilocks daily capacity controller, and bidirectional integrations across Notion, Apple iCloud CalDAV, Apple Reminders/Notes/Health (via iOS Shortcuts), Telegram, and read-only Gmail.

## Architecture Overview

- **AI Routing Engine (`lifeos/ai/`)**
  - **Tier 1 (`gemini-decision-1`)**: Single-pass calibrated triage (`is_actionable`, `item_type`, `energy_tier`, `needs_hydrogen`, `urgency`, `cognitive_load`).
  - **STT (`gemini-3.5-transcribe`)**: Voice transcription with automatic fallback to `gemini-4-hydrogen` for complex/noisy audio or outages.
  - **Tier 2 (`gemma-4-31b-it` + NVIDIA NIM fallback)**: Structured task extraction, Definition of Done (DoD) generation, stalling-task micro-step splitting, and daily rebalancing.
  - **Tier 3 (`gemini-4-hydrogen`)**: Deep conflict resolution, holistic weekly planning, and weekly behavioral analytics guarded by a Redis-backed daily quota manager (`HYDROGEN_DAILY_LIMIT=20`, `HYDROGEN_RESERVED_SYSTEM=3`).
  - **Multimodal RAG (`gemini-embedding-2`)**: 768-dim embeddings across text, images, audio, and PDFs stored in PostgreSQL 16 + `pgvector` with SHA-256 deduplication.
- **Goldilocks Adaptive Planner (`lifeos/planner/`)**
  - Computes `daily_budget = base_capacity * k_calendar * k_health * k_velocity`, strips deep tasks on low-recovery days (`k_health < 0.8`), rolls over unfinished items silently, and splits tasks deferred `>= 3` times into micro-steps.
- **Runtime Stack (`docker-compose.yml`)**
  - `postgres` (`pgvector/pgvector:pg16`), `redis` (`redis:7-alpine`), `api` (FastAPI + Alembic migrations), `worker` (ARQ background jobs), `bot` (aiogram Telegram bot), and `caddy` (automatic HTTPS reverse proxy).

## 1. Environment Setup (`.env`)

Copy the template and fill in your credentials:

```bash
cp .env.example .env
```

Minimum required variables:
- `API_BEARER_TOKEN`: Long random secret for iOS Shortcuts and HTTP API auth.
- `LIFEOS_DOMAIN`: Domain (or `localhost` for local dev) used by Caddy.
- `GEMINI_API_KEY`: Google AI Studio / Gemini API key (and optional `NVIDIA_NIM_API_KEY` fallback).
- `DATABASE_URL` / `REDIS_URL`: Preconfigured for Compose (`postgres:5432`, `redis:6379`).

## 2. Docker Compose Startup

Build and start all services (Alembic runs `alembic upgrade head` automatically before `uvicorn` starts):

```bash
docker compose up -d --build
```

Verify running containers and logs:

```bash
docker compose ps
docker compose logs -f api worker bot
```

## 3. Notion 5-Database Bootstrap

1. Create a Notion internal integration at `https://www.notion.so/my-integrations` and set `NOTION_TOKEN` in `.env`.
2. Create an empty parent page in Notion, share it with your integration, and copy its page ID into `NOTION_PARENT_PAGE_ID`.
3. Bootstrap the 5 linked databases (**Global Goals**, **Projects**, **Tasks**, **Daily Telemetry**, **Weekly Insights**) using `NotionSyncService` (`lifeos/integrations/notion/client.py`) and store the resulting IDs in:
   - `NOTION_GOALS_DB_ID`
   - `NOTION_PROJECTS_DB_ID`
   - `NOTION_TASKS_DB_ID`
   - `NOTION_TELEMETRY_DB_ID`
   - `NOTION_INSIGHTS_DB_ID`

## 4. Apple iCloud CalDAV (Events Only)

LifeOS reads busy calendar slots to compute `k_calendar` and writes **only** `event` items (never tasks) to iCloud Calendar:

1. Generate an **App-Specific Password** at `https://appleid.apple.com` -> Sign-In and Security -> App-Specific Passwords.
2. Configure `.env`:
   - `CALDAV_URL=https://caldav.icloud.com/`
   - `CALDAV_USERNAME=your-apple-id@icloud.com`
   - `CALDAV_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`
   - `CALDAV_CALENDAR_NAME=LifeOS`

## 5. Gmail (`gmail.readonly` OAuth)

Gmail ingestion is strictly read-only (`https://www.googleapis.com/auth/gmail.readonly`):

1. In Google Cloud Console, enable the **Gmail API** and create an **OAuth 2.0 Desktop/Web Client ID**.
2. Export client credentials JSON into `GMAIL_CREDENTIALS_JSON` (inline JSON or file path, depending on your deployment).
3. Run the OAuth consent flow with the `gmail.readonly` scope to obtain the refresh/access token JSON and set `GMAIL_TOKEN_JSON`.
4. Optionally customize `GMAIL_POLL_QUERY` (default: `is:unread newer_than:7d`).

## 6. Telegram Bot & Bot API 10.4/10.5 HTML Widgets

1. Create a bot via `@BotFather` and set `TELEGRAM_BOT_TOKEN`.
2. Set `TELEGRAM_ALLOWED_USER_ID` to your numeric Telegram user ID (fail-closed single-user ACL: `0` rejects all users).
3. Keep `TELEGRAM_WIDGET_ENABLED=true` to render the compact Today HTML Widget (`<= 32 KB` raw / `<= 4 KB` gzip) with 1-tap task toggle and candidate approve/dismiss actions. If the client or Bot API rejects widgets, the bot automatically falls back to an HTML message with `InlineKeyboardMarkup`.

## 7. Telethon Read-Only Userbot Whitelist

To passively monitor selected Telegram channels/chats without ever sending messages:

1. Obtain `TELETHON_API_ID` and `TELETHON_API_HASH` from `https://my.telegram.org`.
2. Generate a `StringSession` and set `TELETHON_SESSION_STRING`.
3. Restrict ingestion strictly via allowlists in `.env`:
   - `TELETHON_WHITELIST_CHAT_IDS=[123456789,-100987654321]`
   - `TELETHON_WHITELIST_FOLDERS=["Work","Inbox"]`

## 8. iOS Shortcuts Setup

Follow [ios_shortcuts/README.md](ios_shortcuts/README.md) to install the **LifeOS Sync** and **Siri Quick Capture** shortcuts on iPhone/iPad for Apple Reminders, Apple Notes, and Apple Health telemetry sync.

## 9. Local Development & Testing

Install dev dependencies and run the test suite and linter:

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```
