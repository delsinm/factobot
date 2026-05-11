# Factobot — Configurable Slack Workflow Bot

A Slack bot that combines AI for conversational help with Block Kit modals
for structured data collection, per-command access control, webhook integration
for workflow automation, and bidirectional callback support so users are
notified when a triggered workflow completes.

The bot is entirely data-driven — adding a new command, changing who can run it,
or pointing it at a different webhook requires only editing `commands.yaml` or
`settings.yaml`. No Python code changes are needed.

---

## Features

- **Configurable slash command** — set `bot.name` in `settings.yaml` (`/factobot`, `/itbot`, etc.)
- **Per-command access control** — restrict each command to Slack user groups or individuals; use `all` for everyone
- **Block Kit modals** — rich form dialogs built dynamically from the YAML
- **Custom icons** — configure separate icons for info, acknowledgment, and error states in `settings.yaml`
- **Webhook integration** — each command POSTs to its own URL; works with Make, Zapier, n8n, or any receiver
- **Bidirectional callbacks** — workflow receivers call back when a job completes; the bot DMs the result to the submitter
- **Callback security** — one-time tokens, configurable TTL, per-IP rate limiting, and payload size cap
- **Provider-agnostic AI** — swap between Anthropic, OpenAI, and Gemini with one line in `settings.yaml`
- **Conversational AI** — chat via DM or @mention with per-user history and configurable session timeout
- **Reset command** — `/reset-chat` clears a user's conversation history

---

## Project Structure

```
factobot/
├── main.py                    # Entry point — starts Bolt + Flask callback server
├── settings.yaml              # Bot identity, AI, tuning, security, icons — safe to commit
├── commands.yaml              # Command definitions: fields, webhooks, access control
├── requirements.txt
├── Procfile                   # Railway deployment (web: python main.py)
├── .env.example               # Copy to .env and fill in your values
├── .gitignore
├── README.md
├── ARCHITECTURE.md
└── app/
    ├── __init__.py
    ├── config.py              # Loads and validates environment variables
    ├── settings_loader.py     # Reads settings.yaml — all tunable values
    ├── access_control.py      # Per-command access checks against Slack user groups
    ├── command_loader.py      # Reads, validates, and exposes commands.yaml
    ├── ai_client.py           # LiteLLM API calls + per-user conversation history
    ├── webhook_client.py      # Outbound webhook POSTs with callback block injection
    ├── job_store.py           # Ephemeral callback token registry
    ├── callback_server.py     # Flask server receiving workflow completion callbacks
    ├── modals.py              # Dynamic Block Kit modal and message builders
    └── handlers.py            # All Slack event, command, action, and view handlers
```

---

## Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name your app and choose your workspace

#### Enable Socket Mode
3. **Socket Mode** → Enable → Generate an App-Level Token with `connections:write` scope → save as `SLACK_APP_TOKEN`

#### Add Bot Token Scopes
4. **OAuth & Permissions** → **Bot Token Scopes** → add:
   ```
   app_mentions:read    — receive @mention events
   channels:history     — read messages in public channels
   chat:write           — post messages
   im:history           — read direct messages
   im:write             — send direct messages
   usergroups:read      — resolve Slack user group membership for access control
   ```

#### Subscribe to Events
5. **Event Subscriptions** → Enable → **Bot Events** → add:
   ```
   app_mention
   message.im
   ```

#### Register Slash Commands
6. **Slash Commands** → **Create New Command** for each of the following.
   Request URL can be left blank for Socket Mode.

   | Command | Description |
   |---|---|
   | `/factobot` | Main bot command (or whatever you set `bot.name` to) |
   | `/reset-chat` | Clears the user's conversation history |

   > **Note:** The name you register here must exactly match `bot.name` in `settings.yaml`.
   > If you change `bot.name`, update the slash command registration to match.

7. **Install to Workspace** → Authorise → copy the **Bot User OAuth Token** (`xoxb-...`) → save as `SLACK_BOT_TOKEN`

---

### 2. Configure settings.yaml

Edit `settings.yaml` to configure the bot. This file is safe to commit — it contains no secrets.

```yaml
bot:
  name: factobot                      # slash command name — must match Slack registration
  max_history: 6                      # messages kept per user (6 = 3 exchanges)
  session_timeout_hours: 4            # clear history after 4h of inactivity
  callback_token_ttl_minutes: 60      # how long a callback token stays valid

ai:
  model: "anthropic/claude-sonnet-4-20250514"

security:
  callback:
    rate_limit_requests: 20           # max callback POSTs per IP per window
    rate_limit_window_seconds: 60     # sliding window duration in seconds
    max_payload_bytes: 8192           # max callback request body size (8 KB)

icons:
  info:  "https://your-bucket.s3.amazonaws.com/info-icon.png"
  ack:   "https://your-bucket.s3.amazonaws.com/ack-icon.png"
  error: "https://your-bucket.s3.amazonaws.com/error-icon.png"
```

---

### 3. Configure commands.yaml

Edit `commands.yaml` to define your commands. Each command needs:

```yaml
commands:
  my-command:
    description: "What this command does"
    webhook_url: "https://your-webhook-receiver.com/my-workflow"
    allowed:
      - all               # or a Slack group handle, or a Slack user ID
    fields:
      - id: name
        label: "Full Name"
        type: text
        placeholder: "e.g. Alex Johnson"
        required: true
```

**Access control (`allowed` key)**

| Entry | Who gets access |
|---|---|
| `all` | Every member of the Slack workspace |
| `hr-managers` | All members of the `hr-managers` Slack user group |
| `U012AB3CD` | A specific individual by their Slack user ID |

Multiple entries are OR'd — a user only needs to match one. The `allowed` key is required; a command without it is locked down for everyone.

**Supported field types**

| Type | Block Kit element | Returned value |
|---|---|---|
| `text` | Plain text input | String |
| `date` | Calendar date picker | `"YYYY-MM-DD"` string |
| `select` | Single-choice dropdown | Option value string |
| `multiselect` | Multi-choice dropdown | List of value strings |

---

### 4. Configure Environment

```bash
cp .env.example .env
# Fill in your tokens and API keys
```

See `.env.example` for the full list of variables with descriptions.

---

### 5. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

### 6. Run Locally

```bash
python main.py
```

Test it by:
- Sending a DM to your bot
- Typing `@Factobot` in a channel
- Running `/factobot`

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-level token for Socket Mode (`xapp-...`) |

### AI Provider (one required)

Set the key matching your `ai.model` in `settings.yaml`. Only one is needed.

| Variable | When to set |
|---|---|
| `ANTHROPIC_API_KEY` | `ai.model` starts with `anthropic/` |
| `OPENAI_API_KEY` | `ai.model` starts with `openai/` |
| `GEMINI_API_KEY` | `ai.model` starts with `gemini/` |

### Icons (all optional)

Icons are configured in `settings.yaml` under `icons:`, not as environment variables.

| Key | Shown in | Description |
|---|---|---|
| `icons.info` | Modals, help messages | Neutral icon — robot face, info symbol, or logo |
| `icons.ack` | Confirmation DMs and callback success | Positive icon — checkmark, thumbs up |
| `icons.error` | Denial, error, and callback failure messages | Warning icon — padlock, caution triangle |

All icon URLs must be publicly accessible over HTTPS. Slack fetches images server-side.

### Optional

| Variable | Description |
|---|---|
| `FALLBACK_WEBHOOK_URL` | Global fallback webhook for AI-triggered workflows. Per-command webhooks are in `commands.yaml`. |
| `CALLBACK_BASE_URL` | Public base URL of this bot (e.g. `https://your-app.railway.app`). Required to enable workflow completion callbacks. When absent the bot runs in fire-and-forget mode. |
| `CALLBACK_PORT` | Port the Flask callback server listens on internally. Default: `3000`. |

> **Note:** `bot.name`, `ai.model`, `max_history`, `session_timeout_hours`, `callback_token_ttl_minutes`, and all security settings are configured in `settings.yaml`, not as environment variables.

---

## Customising the Bot Name

To rename the bot from `/factobot` to something else (e.g. `/itbot`):

1. Edit `settings.yaml` → set `bot.name: itbot`
2. Go to your Slack app settings → **Slash Commands**
3. Edit the existing `/factobot` command and change the name to `/itbot`
4. Redeploy the bot

Both steps are required. If only one is updated, the slash command will stop working.

---

## Hosting Icons

Icon URLs must be publicly accessible over HTTPS. Slack fetches them server-side.

| Option | Best for |
|---|---|
| GitHub raw URL (`raw.githubusercontent.com/...`) | Public repos — free, version-controlled alongside code |
| AWS S3 public bucket | Already on AWS |
| Google Cloud Storage public bucket | Already on GCP |
| Cloudflare R2 | Generous free tier, no egress fees |

GitHub raw URLs work only for public repositories. For private repos, use a cloud storage bucket with public read access.

---

## Workflow Callbacks

When a webhook is fired, the bot embeds a `callback` block in the outbound
payload. The workflow receiver uses this to report back when the job is done,
so the submitter gets a completion DM rather than just a "triggered" message.

### How it works

```
User submits /factobot onboard
  → bot fires webhook with embedded callback block
  → bot DMs: "🔄 Workflow triggered — you'll be notified when it completes."

[Workflow runs — seconds, minutes, whatever it takes]

  → Receiver POSTs to callback.url:
    {"callback_token": "...", "status": "success", "message": "Alex provisioned."}
  → Bot DMs: "✅ /onboard completed: Alex provisioned in Okta and GitHub."
```

### The callback block

Every outbound webhook payload includes:

```json
{
  "command": "onboard",
  "name": "Alex Johnson",
  "callback": {
    "schema_version": "1",
    "url":   "https://your-app.railway.app/webhook/callback",
    "token": "Kx9mP2vQ...",
    "instructions": "POST to callback.url when the job completes.",
    "example": {
      "callback_token": "Kx9mP2vQ...",
      "status":         "success",
      "message":        "Alex Johnson provisioned in Okta and GitHub."
    }
  }
}
```

The `example` object shows the receiver exactly what to POST back — useful for workflow tools that auto-generate field mappings from a sample payload.

### Enabling callbacks

Set `CALLBACK_BASE_URL` in `.env` to the bot's public URL:
```
CALLBACK_BASE_URL=https://your-app.railway.app
```

Without this, the bot runs in fire-and-forget mode — webhooks fire but no completion notification is sent.

### Callback security

Three layers protect the callback endpoint:

| Layer | Mechanism | Configured in |
|---|---|---|
| One-time token | Each token is consumed on first use; replayed requests are rejected | Automatic |
| Token TTL | Tokens expire after `callback_token_ttl_minutes` (default 60) | `settings.yaml` |
| Per-IP rate limiting | Max `rate_limit_requests` per `rate_limit_window_seconds` per source IP; excess returns 429 | `settings.yaml` |
| Payload size cap | Requests larger than `max_payload_bytes` (default 8 KB) are rejected with 413 | `settings.yaml` |

### Health check

`GET /health` returns the current server state:

```json
{
  "status": "ok",
  "pending_jobs": 3,
  "rate_limit_requests": 20,
  "rate_limit_window_seconds": 60,
  "max_payload_bytes": 8192
}
```

---

## Adding a New Command

1. Open `commands.yaml`
2. Add a new entry under `commands:` following the existing pattern
3. Set `webhook_url` to your webhook receiver URL
4. Set `allowed` to control who can run it
5. Define your `fields`
6. Redeploy

No Python changes required.

---

## Deploying to Railway

1. Push the project to a GitHub repository
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Select your repository — Railway auto-detects Python
4. Go to your service's **Variables** tab and add all required environment variables
5. Set `CALLBACK_BASE_URL` to your Railway-assigned URL to enable callbacks
6. Railway deploys automatically on every push to `main`

The `Procfile` runs the bot as a `web` process so Railway exposes a public port for the callback server:
```
web: python main.py
```

> **Note:** This differs from a pure Socket Mode bot which would use `worker`. The `web` type is needed because the Flask callback server requires a publicly accessible port.

---

## Production Notes

**Conversation history** is stored in memory and wiped on restart. Capped at 6 messages (3 exchanges) with a 4-hour session timeout. Both are configurable in `settings.yaml`.

**Callback tokens** expire after `callback_token_ttl_minutes` (default 60 minutes). Set this to comfortably exceed your slowest expected workflow. Expired tokens are rejected even if valid — the submitter receives no completion DM for expired jobs.

**Rate limiting** is per source IP, in-memory, with a sliding window. It resets on bot restart. For persistent rate limiting across restarts, a Redis-backed solution would be needed — not necessary for typical internal bot traffic volumes.

**Group membership caching** — `access_control.py` caches Slack user group membership for 5 minutes to avoid hammering the Slack API. Call `access_control.invalidate_cache()` to force a refresh without restarting.

**Socket Mode + Flask** — the bot runs two servers in one process: Bolt's Socket Mode WebSocket (main thread) and Flask's HTTP server (daemon thread). For high-traffic deployments, splitting these into two separate Railway services is an option but adds cost and operational complexity.

**Secrets** — never commit `.env`. On Railway, use the Variables tab. On Google Cloud Run, use Secret Manager. On AWS, use Parameter Store or Secrets Manager.
