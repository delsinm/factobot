# Factobot Deployment Guide

This guide takes you from a fresh copy of the code to a live, production-ready
Factobot installation. It assumes you're deploying to **Railway** — the fastest
path to a running bot. If you're deploying to AWS instead, see `README_AWS.md`.

---

## Overview

Deploying Factobot has four stages:

1. **Create the Slack app** — register credentials so the bot can connect to your workspace
2. **Run the Scriptorium wizard** — configure the bot and generate your YAML files
3. **Deploy to Railway** — get the bot running on a public URL
4. **Verify** — confirm everything is working end-to-end

---

## Prerequisites

- A Slack workspace where you have permission to install apps
- A [Railway](https://railway.app) account (free tier is sufficient to start)
- An API key for your chosen AI provider:
  - **Anthropic** — [console.anthropic.com](https://console.anthropic.com) → API Keys
  - **OpenAI** — [platform.openai.com](https://platform.openai.com) → API Keys
  - **Google Gemini** — [aistudio.google.com](https://aistudio.google.com) → Get API Key

---

## Stage 1 — Create the Slack App

### 1.1 Create the app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Enter an app name (e.g. `Factobot`) and choose your development workspace
3. Click **Create App**

### 1.2 Enable Socket Mode

1. In the left sidebar, click **Socket Mode**
2. Toggle **Enable Socket Mode** on
3. When prompted, name the app-level token (e.g. `socket-token`) and click **Generate**
4. Copy the token — it starts with `xapp-`. This is your `SLACK_APP_TOKEN`. Store it safely.

### 1.3 Add bot token scopes

1. In the left sidebar, click **OAuth & Permissions**
2. Scroll to **Bot Token Scopes** and add all of the following:

   | Scope | Required for |
   |---|---|
   | `app_mentions:read` | Receiving @mention events in channels |
   | `channels:history` | Reading messages in public channels |
   | `chat:write` | Posting messages and DMs |
   | `im:history` | Reading direct messages sent to the bot |
   | `im:write` | Opening DMs to send results and confirmations |
   | `usergroups:read` | Resolving Slack user group membership for access control |

### 1.4 Subscribe to events

1. In the left sidebar, click **Event Subscriptions**
2. Toggle **Enable Events** on
3. Expand **Subscribe to bot events** and add:
   - `app_mention`
   - `message.im`
4. Click **Save Changes**

### 1.5 Register slash commands

1. In the left sidebar, click **Slash Commands** → **Create New Command**
2. Create the following two commands. The **Request URL** field can be left blank — Socket Mode doesn't need it.

   | Command | Short Description |
   |---|---|
   | `/factobot` | Main bot command (change this if you rename the bot) |
   | `/reset-chat` | Clears your conversation history with the bot |

   > If you plan to use a different bot name (e.g. `/itbot`), register it as `/itbot` here and remember to set `bot.name: itbot` in `settings.yaml` later.

### 1.6 Install the app

1. In the left sidebar, click **OAuth & Permissions**
2. Click **Install to Workspace** → **Allow**
3. Copy the **Bot User OAuth Token** — it starts with `xoxb-`. This is your `SLACK_BOT_TOKEN`.

At this point you have two tokens saved:
- `SLACK_BOT_TOKEN` — starts with `xoxb-`
- `SLACK_APP_TOKEN` — starts with `xapp-`

---

## Stage 2 — Configure with Scriptorium

Scriptorium is the first-run configuration wizard. Open `scriptorium/scriptorium.html`
in any browser — it runs entirely locally, no server needed.

### 2.1 Bot settings tab

Configure the core bot settings:

| Setting | Recommended value | Notes |
|---|---|---|\
| **Bot name** | `factobot` | Must exactly match the slash command you registered in Stage 1 |
| **AI model** | `anthropic/claude-sonnet-4-20250514` | Change to `openai/gpt-4o` or `gemini/gemini-2.0-flash` if using a different provider |
| **Max history** | `6` | Messages kept per user between AI calls (6 = 3 full exchanges) |
| **Session timeout** | `4` hours | Inactivity window before conversation history is cleared |
| **Callback token TTL** | `60` minutes | Set to comfortably exceed your slowest expected workflow |

### 2.2 Commands tab

This is where you define the commands your users will run. Each command needs:

- **Description** — shown in the help modal
- **Action type** — `webhook` to POST to an external URL, or `skill` to run a built-in AI skill
- **Webhook URL** — the Make, Zapier, n8n, or custom endpoint to call (webhook commands only)
- **Access control** — who can run the command. Use `all`, a Slack group handle (e.g. `hr-managers`), or a Slack user ID (e.g. `U012AB3CD`). Multiple entries are OR'd.
- **Notification channels** — optional. Channels to post the workflow result to, in addition to the DM sent to the submitter. Use `#channel-name` for public channels, or a channel ID (e.g. `C012ABC456`) for private channels. The bot must be a member of each channel.
- **Form fields** — the inputs shown in the modal dialog

Sample command definitions are pre-loaded to illustrate the structure. Edit or delete them to match your workflows.

### 2.3 Save configuration

Click **Save Configuration** in the top bar. This downloads two files:

- `settings.yaml` — bot identity, AI provider, tuning, and security settings
- `commands.yaml` — all command definitions

Move both files into the project root, replacing the placeholder files that came with the code.

---

## Stage 3 — Deploy to Railway

### 3.1 Push to GitHub

Commit everything to a GitHub repository:

```bash
git add .
git commit -m "Initial Factobot configuration"
git push
```

> `commands.yaml` contains webhook URLs — keep the repository **private** if those URLs should not be public.

### 3.2 Create a Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repository
3. Railway detects Python automatically and queues an initial deploy (it will fail until you add environment variables — that's expected)

### 3.3 Add environment variables

In Railway, go to your service → **Variables** tab and add:

#### Required

| Variable | Value |
|---|---|
| `SLACK_BOT_TOKEN` | Your `xoxb-...` token from Stage 1 |
| `SLACK_APP_TOKEN` | Your `xapp-...` token from Stage 1 |

#### AI provider (add the one matching your `ai.model`)

| Variable | When to add |
|---|---|
| `ANTHROPIC_API_KEY` | `ai.model` starts with `anthropic/` |
| `OPENAI_API_KEY` | `ai.model` starts with `openai/` |
| `GEMINI_API_KEY` | `ai.model` starts with `gemini/` |

#### Callback server (required for workflow completion notifications)

| Variable | Value |
|---|---|
| `CALLBACK_BASE_URL` | Your Railway-assigned URL, e.g. `https://factobot-production.up.railway.app` |

> Find your Railway URL under the service's **Settings** → **Networking** → **Public URL**. Add it before the first deploy so callbacks work from day one. Do not include a trailing slash.

#### Optional

| Variable | Value |
|---|---|
| `FALLBACK_WEBHOOK_URL` | A global webhook URL for AI-triggered workflows in conversational mode |
| `CALLBACK_PORT` | Port for the Flask callback server. Default: `3000`. Railway maps this automatically — only change it if required. |

### 3.4 Trigger a deploy

After adding variables, Railway automatically redeploys. Watch the **Deploy Logs** tab — a successful start looks like:

```
INFO  app.settings_loader  Settings loaded — bot_name='factobot' ...
INFO  app.command_loader   Loaded 5 command(s): list-access, offboard, onboard, ...
INFO  __main__             Callback server started on port 3000.
INFO  __main__             Starting bot in Socket Mode...
INFO  slack_bolt.App       Starting to receive messages from a new connection ...
```

---

## Stage 4 — Verify

### 4.1 Test the slash command

In Slack, type `/factobot`. You should see a command picker or help modal appear.

If the command isn't recognised, double-check that the slash command name in your Slack app settings exactly matches `bot.name` in `settings.yaml`.

### 4.2 Test conversational mode

Send a direct message to the bot. It should reply. If it doesn't respond, check the Railway logs for errors — the most common causes are a missing or incorrect API key, or the bot not being in the channel.

### 4.3 Test a webhook command

Run one of your webhook commands and submit the form. You should receive a DM confirming the webhook was triggered. If `CALLBACK_BASE_URL` is set and your workflow receiver calls back correctly, a second DM arrives with the result.

### 4.4 Check the health endpoint

Your bot exposes a health check at `GET /health`. From a terminal:

```bash
curl https://your-app.railway.app/health
```

Expected response:

```json
{
  "status": "ok",
  "pending_jobs": 0,
  "rate_limit_requests": 20,
  "rate_limit_window_seconds": 60,
  "max_payload_bytes": 8192
}
```

---

## Configuring Workflow Callbacks

When a webhook command fires, the bot embeds a `callback` block in the outbound
payload. Your workflow receiver uses this to report back when the job completes,
so users get a completion notification rather than just a "triggered" message.

### What the receiver must POST back

```json
{
  "callback_token": "Kx9mP2vQ...",
  "status":         "success",
  "message":        "Alex Johnson provisioned in Okta and GitHub.",
  "result_url":     "https://acme.okta.com/admin/user/00u1ab"
}
```

| Field | Required | Description |
|---|---|---|
| `callback_token` | Yes | The one-time token echoed from the outbound payload |
| `status` | Yes | `"success"` or `"failure"` |
| `message` | Yes | Human-readable result shown to the user in Slack |
| `result_url` | No | HTTPS URL to the created or affected resource — rendered as a "View Result" button on success |

The outbound payload includes an `example` object showing the receiver exactly what to POST — most workflow tools (Make, n8n, Zapier) can auto-populate their response fields from this sample.

### Notification channels

If a command has `notify_channels` configured, the completion result is also
posted to those channels in addition to the DM sent to the submitter. Channels
are configured per-command in Scriptorium or directly in `commands.yaml`:

```yaml
notify_channels:
  - '#it-operations'     # public channel — use # prefix
  - C012ABC456           # private channel — use the channel ID
```

The bot must be a member of each listed channel, or the post will fail silently
(logged as a warning; other channels are still notified).

---

## Access Control

Every command has an `allowed` list. Entries are checked in order; access is
granted on the first match.

```yaml
allowed:
  - all              # any workspace member
  - hr-managers      # Slack user group handle
  - it-admins        # another group
  - U012AB3CD        # a specific user by their Slack user ID
```

To find a group's handle: Slack workspace → **People & user groups** → select the group → **Edit** → **Handle**.

To find a user ID: click their profile → **More** (⋯) → **Copy member ID**.

> The `usergroups:read` OAuth scope (added in Stage 1.3) is required for group
> membership checks. Without it, group-based access control will deny everyone.

---

## Icons

Icons appear in modals and notification DMs. All URLs must be publicly accessible
over HTTPS — Slack fetches them server-side.

| Key | Shown in |
|---|---|
| `icons.info` | Modals and help messages |
| `icons.ack` | Confirmation and callback success DMs |
| `icons.error` | Access denied and callback failure messages |

Configure them in `settings.yaml` under `icons:`, or via the Scriptorium
settings tab. Good free hosting options:

| Option | Notes |
|---|---|
| GitHub raw URL | Free, works for public repos. URL format: `https://raw.githubusercontent.com/org/repo/main/icons/ack.png` |
| AWS S3 | Set the bucket policy to allow public `s3:GetObject` |
| Cloudflare R2 | Generous free tier, no egress fees |

Omit a URL and that message type renders without an image.

---

## Making Changes After Deployment

### Changing commands

1. Edit `commands.yaml` directly, or open Scriptorium and re-export
2. Commit and push — Railway redeploys automatically
3. No Python code changes needed

### Changing the bot name

1. Edit `bot.name` in `settings.yaml`
2. Go to your Slack app settings → **Slash Commands** → rename the existing command
3. Commit, push, and redeploy

Both steps are required. Updating only one will break the slash command.

### Rotating credentials

Update the variable in Railway's **Variables** tab. Railway redeploys automatically
when a variable changes.

### Changing the AI model

1. Update `ai.model` in `settings.yaml` to a new LiteLLM model string
2. Add the corresponding API key to Railway if switching providers
3. Commit, push, and redeploy

Full model list: [docs.litellm.ai/docs/providers](https://docs.litellm.ai/docs/providers)

---

## MCP Integrations

MCP (Model Context Protocol) lets the AI query external services during
conversations — Confluence, Jira, GitHub, and others. Configure MCP servers
in `settings.yaml` and add the relevant credentials as environment variables.

### Atlassian (Confluence + Jira)

1. Generate an API token at [id.atlassian.com](https://id.atlassian.com) → **Security** → **API tokens**
2. Add to Railway Variables:
   ```
   ATLASSIAN_API_TOKEN=your-api-token
   ATLASSIAN_EMAIL=you@yourcompany.com
   ```
3. Add to `settings.yaml`:
   ```yaml
   integrations:
     mcp_servers:
       - name: atlassian
         url: "https://mcp.atlassian.com/v1/mcp"
   ```

Users can then ask questions like *"What's the laptop policy?"* and the bot
queries Confluence automatically.

---

## Troubleshooting

### Bot doesn't respond to slash commands

- Confirm `bot.name` in `settings.yaml` matches the slash command name registered in Slack exactly (case-sensitive, no spaces)
- Check Railway logs for startup errors — a misconfigured variable often causes a clean crash with a descriptive message
- Confirm `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set and correct

### Bot doesn't respond to DMs or @mentions

- Confirm the `app_mentions:read`, `im:history`, and `im:write` scopes are added
- Confirm the `app_mention` and `message.im` events are subscribed
- Check that the correct AI provider API key is set and valid

### Access denied for a group member

- Confirm the `usergroups:read` scope is present in your Slack app settings
- Check that the group handle in `commands.yaml` matches the Slack group's handle exactly (case-insensitive, but no extra characters)
- Try using a specific user ID (`U012AB3CD`) instead of a group name to verify the command itself works

### Callbacks not arriving

- Confirm `CALLBACK_BASE_URL` is set to the correct public URL (no trailing slash)
- Test the health endpoint: `curl https://your-app.railway.app/health`
- Check that your workflow receiver is POSTing to `CALLBACK_BASE_URL/webhook/callback` with the correct `callback_token`
- Confirm the token hasn't expired — TTL is set by `callback_token_ttl_minutes` in `settings.yaml`

### Notification channels not receiving messages

- Confirm the bot is a member of the channel (invite it with `/invite @Factobot`)
- Confirm the channel reference is correct: `#channel-name` for public channels, `C012ABC456` ID for private channels
- Check Railway logs for warnings — a failed channel post is logged but doesn't block other channels

### Commands work but icons don't appear

- Confirm the icon URLs in `settings.yaml` are publicly accessible over HTTPS
- Test by opening the URL in a browser in an incognito window — if it requires a login, Slack can't fetch it
- For GitHub raw URLs, confirm the repository is public

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-level Socket Mode token (`xapp-...`) |
| `ANTHROPIC_API_KEY` | One of these | AI provider key — set the one matching `ai.model` |
| `OPENAI_API_KEY` | One of these | |
| `GEMINI_API_KEY` | One of these | |
| `CALLBACK_BASE_URL` | For callbacks | Public URL of the bot. Enables workflow completion notifications. |
| `CALLBACK_PORT` | No | Internal Flask port. Default: `3000`. |
| `FALLBACK_WEBHOOK_URL` | No | Global fallback webhook for AI-triggered workflows |
| `DATABASE_URL` | No | PostgreSQL connection string. Enables DB-backed config persistence. |
| `ATLASSIAN_API_TOKEN` | No | Required when the Atlassian MCP server is enabled |
| `ATLASSIAN_EMAIL` | No | Email associated with the Atlassian API token |
