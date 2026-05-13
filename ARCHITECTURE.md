# Architecture

This document describes the high-level design of Factobot, the key architectural
decisions made during its construction, and the reasoning behind each one.

---

## System Overview

Factobot is a Slack bot with two distinct interaction modes that share the same
underlying infrastructure:

**Modal mode** — a user runs `/factobot <command>`, fills out a structured form,
and the bot either fires a webhook to trigger a downstream workflow (`action_type: webhook`)
or executes an AI skill (`action_type: skill`) inline.

**Conversational mode** — a user sends a DM or @mention, the configured AI model
answers using its own knowledge and, when relevant, queries external services
(Confluence, Jira, etc.) via MCP servers configured at the AI provider level
(not in this codebase — see your provider's console).

Both modes run in a single Python process. The process also runs a Flask HTTP
server in a background thread to receive workflow completion callbacks.

```
┌─────────────────────────────────────────────────────────┐
│                        Slack                            │
│                                                         │
│   User DM / @mention        /factobot <command>         │
│          │                         │                    │
└──────────┼─────────────────────────┼────────────────────┘
           │   WebSocket (outbound)  │
           ▼                         ▼
┌─────────────────────────────────────────────────────────┐
│                   Factobot Process                      │
│                                                         │
│   Bolt (Socket Mode, main thread)                       │
│   ├── Conversational mode  (ai_client.py)               │
│   │       └── LiteLLM → AI provider                     │
│   │               └── MCP servers (Confluence, Jira...) │
│   └── Modal mode  (modals.py + handlers.py)             │
│           ├── webhook commands                          │
│           │       └── webhook_client.py → receiver      │
│           └── skill commands                            │
│                   └── skill_runner.py → LiteLLM         │
│                                                         │
│   Flask (callback server, daemon thread)                │
│   └── POST /webhook/callback ← Workflow receivers       │
│           └── job_store.py (token registry)             │
│                                                         │
│   settings.yaml  ←──  settings_loader.py               │
│   commands.yaml  ←──  command_loader.py                 │
│   config.py      ←──  all modules                       │
│   access_control.py  ←──  handlers.py                   │
│   db.py  ←── loaders (optional, when DATABASE_URL set)  │
└─────────────────────────────────────────────────────────┘
```

---

## Module Responsibilities

Each module has a single, well-defined responsibility. Dependencies flow inward
— outer modules (handlers) depend on inner modules (clients, loaders), never
the reverse.

```
main.py
  ├── handlers.py          ← registers all Slack event handlers
  │     ├── ai_client.py        ← LiteLLM API calls + conversation history
  │     ├── skill_runner.py     ← SKILL.md-based AI skill execution (skill commands)
  │     ├── webhook_client.py   ← outbound webhook POSTs + callback block injection
  │     ├── job_store.py        ← ephemeral callback token registry
  │     ├── access_control.py   ← per-command access checks
  │     ├── command_loader.py   ← reads and validates commands.yaml
  │     ├── settings_loader.py  ← reads and validates settings.yaml
  │     ├── modals.py           ← Block Kit modal/message builders
  │     └── config.py           ← environment variables (imported by all)
  └── callback_server.py   ← Flask HTTP server for workflow completion callbacks
        ├── job_store.py        ← shared with handlers (thread-safe)
        └── modals.py           ← shared result block builders

db.py  ← optional PostgreSQL backend (used when DATABASE_URL is set)
         loaded by settings_loader.py and command_loader.py at startup
         if a database row exists, file-based YAML is overridden by the DB value
```

`commands.yaml` and `settings.yaml` are the two non-Python inputs. Both are
read once at startup by their respective loaders and cached for the lifetime
of the process. When `DATABASE_URL` is set, `db.py` is consulted first and
the file-based YAML serves as the fallback.

---

## Flow 1 — Slash Command (Happy Path)

The primary interaction mode. A user types `/factobot onboard`, fills out the
modal, and the bot fires the command's configured webhook URL.

```
User types /factobot onboard
        │
        ▼
Slack sends slash command event to bot (includes trigger_id)
        │
        ▼
handlers.on_factobot_command()
        │
        ├─── ack()                         ← must happen within 3 seconds
        │
        ├─── command_loader.get_command("onboard")
        │         returns command config dict
        │
        ├─── access_control.check_access()
        │         resolves user group membership via Slack API
        │         checks against command's "allowed" list
        │
        ├── [DENIED] ──► respond(denial_blocks)   ← ephemeral, user only sees it
        │
        └── [ALLOWED]
                │
                ▼
        client.views_open(trigger_id, build_modal("onboard", command))
                │
                ▼
        Slack renders modal to user
                │
        User fills out form and clicks Submit
                │
                ▼
        Slack sends view_submission event
                │
                ▼
        handlers.on_modal_submit()
                │
                ├─── ack()                         ← closes the modal
                │
                ├─── extract command name from callback_id
                │
                ├─── command_loader.extract_field_values()
                │         reads submitted values from view["state"]["values"]
                │
                ├─── command_loader.validate_field_values()
                │
                ├── [INVALID] ──► ack(response_action="errors")
                │                  modal stays open, errors shown inline
                │
                └── [VALID]
                        │
                        ├─── job_store.create_job()
                        │         generates one-time 32-char callback token
                        │         registers job (command, user, channel)
                        │
                        ▼
                webhook_client.trigger(payload, webhook_url, callback_token)
                        │   injects self-documenting callback block into payload
                        │
                        ▼
                Workflow receiver processes the request
                        │
                        ▼
                client.chat_postMessage(user_id, confirmation_blocks)
                        │
                        ▼
                User receives confirmation DM:
                "🔄 Workflow triggered — you'll be notified when it completes." ✅

[Later — when the workflow finishes]

Receiver POSTs to /webhook/callback
        │  {"callback_token": "...", "status": "success", "message": "..."}
        ▼
callback_server.receive_callback()
        ├─── rate limit check (per source IP)
        ├─── payload size check (enforced by Flask)
        ├─── job_store.redeem_job(token)
        │         validates token, checks TTL, consumes (single-use)
        │
        └─── client.chat_postMessage(channel_id, callback_result_blocks)
                        │
                        ▼
                User receives completion DM:
                "✅ /onboard completed: Alex provisioned in Okta and GitHub." ✅
```

---

## Flow 2 — Conversational Message (Happy Path)

The secondary interaction mode. Used for questions about the bot's capabilities
or general IT process questions.

```
User sends DM or @mention
        │
        ▼
Slack sends message event to bot
        │
        ▼
handlers.on_direct_message() or handlers.on_mention()
        │
        ├─── filter out bot messages and subtypes (prevent loops)
        │
        ├─── strip @mention prefix if present
        │
        └─── process_message(user_id, text, say, client)
                │
                ├─── say("Thinking... 🤔")
                │         posts placeholder — gives user immediate feedback
                │         while the AI generates a response (can take 2-5s)
                │
                ├─── ai_client.get_response(user_id, text)
                │         │
                │         ├─── get_history(user_id)
                │         │         checks session timeout (configurable, default 4h)
                │         │         returns [] if expired or new user
                │         │
                │         ├─── append user message to history
                │         │
                │         ├─── trim to MAX_HISTORY=6 messages
                │         │
                │         ├─── LiteLLM completion call
                │         │         sends: system prompt + trimmed history
                │         │         routes to configured AI provider
                │         │         returns: reply (may include
                │         │                  <webhook_trigger> block)
                │         │
                │         └─── append reply, save_history(user_id, history)
                │                   records _last_seen[user_id] = now
                │
                ├─── ai_client.extract_webhook_trigger(response)
                │         looks for <webhook_trigger>...</webhook_trigger> in reply
                │
                ├── [TRIGGER FOUND]
                │         webhook_client.trigger(payload)   ← global fallback webhook
                │         append workflow status to reply
                │
                └── [NO TRIGGER]
                        │
                        ▼
                client.chat_update(placeholder.ts, final_message)
                        │
                        ▼
                Placeholder replaced with AI's reply ✅
```

---

## Flow 3 — Session Timeout

History is automatically cleared after 4 hours of inactivity. This happens
lazily — at the point of the next message — rather than on a background timer.

```
User sends a message (any message)
        │
        ▼
ai_client.get_history(user_id)
        │
        ├─── _last_seen[user_id] exists?
        │         NO  ──► return []   (new user, no history)
        │
        └─── YES
                │
                ▼
        now - _last_seen[user_id] > SESSION_TIMEOUT (configurable, default 4h)?
                │
                ├── NO  ──► return _history[user_id]   (active session)
                │
                └── YES
                        │
                        ▼
                del _history[user_id]
                del _last_seen[user_id]
                        │
                        ▼
                return []   (fresh start)
                        │
                        ▼
        AI answers without stale context ✅
```

---

## Flow 4 — Access Control

Access is checked on every `/factobot <command>` invocation, before the modal
opens. Denied users never see the form.

```
/factobot offboard
        │
        ▼
access_control.check_access(client, user_id, "offboard", command)
        │
        ├─── command["allowed"] missing or empty?
        │         YES ──► AccessResult(allowed=False, "no_allowed_list_configured")
        │
        └─── iterate allowed entries in order
                │
                ├─── entry == "all"
                │         ──► AccessResult(allowed=True)   ← immediate grant
                │
                ├─── entry looks like a Slack user ID (starts with U, 9+ chars)
                │         user_id == entry?
                │         YES ──► AccessResult(allowed=True)
                │
                └─── entry is a group handle (e.g. "hr-managers")
                          │
                          ▼
                    _get_group_members(client, "hr-managers")
                          │
                          ├─── cached and fresh (< 5 min old)?
                          │         YES ──► return cached set
                          │
                          └─── NO
                                    │
                                    ▼
                              client.usergroups_list()        ← Slack API
                              find group by handle
                              client.usergroups_users_list()  ← Slack API
                              cache result with timestamp
                                    │
                                    ▼
                              user_id in member set?
                              YES ──► AccessResult(allowed=True)
                              NO  ──► continue to next entry

        All entries exhausted without a match
                │
                ▼
        AccessResult(allowed=False, denial_message, denial_blocks)
                │
                ▼
        respond(blocks=denial_blocks)   ← ephemeral message, user only ✅
```

---

## Key Architectural Decisions

### 1. Socket Mode + Flask in One Process

The bot uses two servers simultaneously in a single process:

- **Bolt (Socket Mode)** — outbound WebSocket to Slack on the main thread. Handles all Slack events, commands, and modals. No inbound port required.
- **Flask** — inbound HTTP server on a daemon background thread. Receives workflow completion callbacks from external receivers.

**Why Socket Mode for Slack:** No public URL, no reverse proxy, no SSL termination, no firewall rules. The bot connects outbound to Slack — local development and production are identical.

**Why Flask for callbacks:** Workflow receivers need somewhere to POST completion notifications. Socket Mode can't receive inbound HTTP, so a second server is required. Running it as a daemon thread in the same process keeps the deployment simple — one Railway service, one process, shared Slack client, no inter-service communication.

**Why one process, not two services:** Splitting Bolt and Flask into separate Railway services would require them to share the job token store (necessitating Redis or a database) and share the Slack client credentials. A single process shares both in memory with zero coordination overhead. For the traffic volumes of an internal IT bot, one process is more than sufficient.

**Tradeoff:** Socket Mode holds a single persistent WebSocket connection — only one process can hold it, ruling out horizontal scaling. The Flask thread means `Procfile` uses `web` (not `worker`) so Railway exposes the callback port, but the Socket Mode connection doesn't benefit from Railway's HTTP routing.

---

### 2. Two-File YAML Configuration

Bot configuration is split across two YAML files with intentionally different
audiences, sensitivity levels, and edit cadences:

```
settings.yaml   — bot name, AI model, history/timeout tuning, security limits,
                  icon URLs. Safe to share. No secrets.
                  Edited by IT admins, designers, or security reviewers.

commands.yaml   — workflows, webhook URLs, field definitions, access rules.
                  Contains webhook URLs — keep the repo private.
                  Edited when adding/changing workflows.
```

**Why split them?** The people who edit these files and the reasons they edit
them are different. A designer updating icons should not have to navigate a
file full of field definitions and webhook URLs. An IT administrator adding a
new workflow should not have to scroll past branding config. The split also
has a safety dimension — `commands.yaml` contains webhook URLs, while
`settings.yaml` contains nothing sensitive and can be freely shared or
committed publicly.

**Why not environment variables for icons and bot name?** Icon URLs are
presentation config, not deployment infrastructure. Changing an icon is a
content decision, not a deployment secrets decision. Storing them in YAML
means a non-developer can update them by editing a file rather than navigating
a deployment platform's environment variable UI.

**Tradeoff:** The YAML schemas can only express what the loaders know how to
parse. More complex field types or conditional logic would require extending
both the YAML schema and the Python loader together. For the current supported
field types (text, date, select, multiselect) this is not a constraint.

---

### 3. Dynamic Modal Generation

Modals are built at runtime from the command's field definitions rather than
being hardcoded in Python.

**Why:** A hardcoded modal ties the UI directly to specific fields, meaning any
change to a command's form requires a code change. Dynamic generation means
`commands.yaml` is the single source of truth for both the data and the UI —
add a field to the YAML and it appears in the modal automatically.

**How it works:** Each field type (`text`, `date`, `select`, `multiselect`)
maps to a Block Kit element type. `modals.py` dispatches to a type-specific
builder function for each field in the command's field list. The block_id and
action_id follow a deterministic naming convention (`block_{id}` /
`input_{id}`) so `command_loader.extract_field_values()` can read submitted
values back without any extra coordination.

---

### 4. Single Slash Command with Subcommands

The bot exposes one slash command (`/factobot`) that accepts a subcommand as
its first argument, rather than registering a separate slash command per
workflow.

**Why:** Slack limits the number of slash commands per app and requires each
one to be registered manually in the app settings. A single entry point scales
to any number of workflows without touching Slack's configuration. It also
gives users a consistent mental model — there is one bot, and it does many
things.

**How subcommand routing works:** The slash command handler reads the first
word of `body["text"]`, looks it up in `COMMANDS`, and opens the corresponding
modal. Unknown subcommands show a filtered help list. The modal's `callback_id`
encodes the command name (`factobot_modal:onboard`) and a compiled regex matches
all submissions to a single handler, which extracts the command name from the
callback_id.

---

### 5. Per-Command Webhook URLs

Each command in `commands.yaml` has its own webhook URL rather than all
commands sharing a single endpoint.

**Why:** A single shared URL would require the receiver to inspect the payload and
route to the right workflow internally — adding logic in the receiver that
duplicates what the bot already knows. Separate URLs let each workflow be
self-contained and independently deployable. It also makes it trivial to point a
command at a completely different automation platform in future.

---

### 6. In-Memory Conversation History with Session Timeouts

Conversation history is stored in a Python dict (`_history`) in `ai_client.py`
rather than an external store like Redis.

**Why:** The bot's conversational mode is a short-lived reference tool. Users
ask one or two questions about the bot's capabilities, then switch to a slash
command. The history's only job is to give the AI enough context to resolve
pronouns across consecutive messages — "what fields does it ask for?" after
"what does the onboard command do?". A dict in process memory is zero-overhead
and zero-dependency for this purpose.

**Session timeouts:** Without a timeout, a user's Monday morning session would
still be in the dict on Thursday afternoon and would be sent as recent context.
A 4-hour inactivity timeout clears history automatically so every new work
session starts fresh. The timeout is checked lazily at read time — no background
threads or schedulers required.

**MAX_HISTORY=6:** Six messages (three full exchanges) gives the AI enough
context to track a follow-up question across a short multi-topic conversation
without accumulating context that will almost certainly never be referenced
again for this use case.

**Provider-agnostic:** History is stored as standard `[{"role": ..., "content": ...}]`
dicts — the same format every major LLM API uses. Switching providers via
`settings.yaml` requires no changes to the history implementation.

**Tradeoff:** History is lost on bot restart. For this use case this is
acceptable — a restart mid-conversation is rare, and the conversational mode
is not the primary value of the bot.

---

### 7. Access Control at the Entry Point

Access is checked before the modal opens, not after submission.

**Why:** Checking access after submission means the user can see and fill out
a form they're not allowed to submit. This is confusing and wastes their time.
Checking before the modal opens means unauthorised users get an immediate,
clear denial and never see a form they can't use. Denial messages are ephemeral
— visible only to the user who ran the command — so access restrictions are
not broadcast to the channel.

**Group membership caching:** Resolving a Slack user group requires two API
calls (list all groups, then list members of the matched group). Caching the
result for 5 minutes means a busy workspace doesn't generate a Slack API call
on every command invocation. A 5-minute TTL is a reasonable balance between
freshness and API efficiency — membership changes are reflected within one
cache window.

---

### 8. The "Thinking..." Placeholder Pattern

When processing a conversational message, the bot immediately posts a
"Thinking... 🤔" message and then updates it with the real reply.

**Why:** The AI API can take 2-5 seconds to respond. In Slack, silence for
that long feels like the bot is broken or didn't receive the message. The
placeholder gives instant feedback that the message was received and is being
processed. It also means the reply appears in-place rather than as a second
message, keeping the conversation thread clean.

**How it works:** Bolt's `say()` returns metadata including the message
timestamp (`ts`) and channel. After the AI responds, `client.chat_update()` is
called with those values to replace the placeholder in-place.

---

### 9. MCP for External Service Integrations

External service access (Confluence, Jira, GitHub, etc.) is provided via MCP
(Model Context Protocol) servers rather than custom API wrappers.

**Why:** Writing integration code for each external service is significant
maintenance burden — each API has its own auth model, pagination, rate limits,
and response shape. MCP servers are pre-built, maintained by the service
providers themselves, and speak a standard protocol that LiteLLM understands
natively. Adding a new integration is a one-line config change in `settings.yaml`
rather than a new module, new API wrapper, and new tool definition.

**Tradeoff:** You have less control over exactly what gets queried compared to
custom tool definitions. The AI has whatever access the API credentials allow,
and relies on its own judgement about when to query.

---

### 10. Bidirectional Callbacks via Self-Documenting Payloads

When the bot fires a webhook, it embeds a `callback` block in the outbound
payload that tells the workflow receiver exactly how to report back — URL,
token, instructions, and a worked example.

**Why self-documenting:** The person configuring the workflow receiver is often
a different person from the developer who wrote the bot, working at a different
time. They open the incoming payload in their HTTP module (Make, Zapier, n8n)
and need to know what to send back without finding separate documentation. The
`example` field is particularly useful because workflow tools can auto-generate
field mappings from a sample payload.

**Why per-job tokens instead of a shared secret:** A shared secret would need
to be configured in both the bot and every workflow receiver — a coordination
problem at every new workflow setup. A per-job token is self-contained in the
payload: the receiver receives it, stores it, and echoes it back. No pre-shared
configuration required. The token is single-use so a replayed callback is
automatically rejected.

---

### 11. Callback Security: Layered, Configurable, All in settings.yaml

The callback endpoint uses three independent security layers, all configurable
in `settings.yaml` without code changes:

**Layer 1 — Payload size cap** (`max_payload_bytes`, default 8 KB): Flask
rejects oversized bodies before the handler runs. A legitimate callback (token
+ status + message) is well under 1 KB. This prevents memory exhaustion from
multi-megabyte attack payloads.

**Layer 2 — Per-IP rate limiting** (`rate_limit_requests` / `rate_limit_window_seconds`,
default 20 req/60s): An in-memory sliding window per source IP. Exceeds the
limit → 429. Prevents brute-force token guessing and denial-of-service via
request flooding. Implemented without external dependencies (no Redis needed)
using a `collections.deque` per IP with timestamp pruning.

**Layer 3 — One-time callback token** (`callback_token_ttl_minutes`, default
60): Each token is 32 URL-safe characters (192 bits of entropy, generated with
`secrets.token_urlsafe(24)`). Tokens are consumed on first use and expire after
the configured TTL. Even a valid token fails if the job has already been
redeemed or has timed out.

**Why all in settings.yaml:** Security parameters are operational decisions
that change over time — a team might start with the defaults and tighten limits
after observing traffic patterns. Keeping them in `settings.yaml` means a
security reviewer can audit and adjust them without touching Python code.

---

### 12. Skill Commands as a First-Class Action Type

Commands can set `action_type: skill` to run a `SKILL.md`-based AI skill
instead of firing an external webhook. The skill runner (`skill_runner.py`):

1. Resolves the skill by name from an ordered list of search paths:
   `/mnt/skills/user` → `/mnt/skills/examples` → `/mnt/skills/public`
2. Reads the `SKILL.md` file, which encodes task-specific instructions
3. Builds a prompt combining the skill instructions with the form's submitted values
4. Calls the AI model via LiteLLM and returns the result as a string

**Why skills instead of webhooks:** Some operations (Jira ticket creation,
hardware assignment lookup, policy checks) can be handled entirely by the AI
with the right instructions, without requiring an external workflow tool.
Skills are synchronous — the result is posted in the confirmation DM
immediately, without a callback token or TTL. This makes them suitable for
lower-latency tasks where Make/Zapier round-trips would add unnecessary delay.

**Why the search-path priority order:** User skills (`/mnt/skills/user`) take
precedence over example and public skills so an operator can override or extend
a built-in skill without modifying shared files. The first match wins.

---

### 13. Optional PostgreSQL Backend via db.py

When `DATABASE_URL` is set, `db.py` provides a PostgreSQL persistence layer
for bot configuration. `settings_loader.py` and `command_loader.py` check for
a database row at startup; if one exists, it overrides the on-disk YAML files.
If not, the files are used as normal.

**Why optional:** Many deployments don't need database persistence — the YAML
files are edited in source control, committed, and deployed. The database layer
exists for teams that want live configuration changes without redeployments,
and for the Scriptorium UI, which can save configuration back to the database
after each edit session.

**Schema:** Three tables under the public schema:
- `config_settings` — single-row JSONB blob for all settings.yaml values
- `config_commands` — one row per command, JSONB for the full command config
- `breakglass_users` — bcrypt-hashed credentials for emergency admin access

All tables are created with `CREATE TABLE IF NOT EXISTS` — startup is safe on
both brand-new and pre-provisioned databases.

---
```
                    commands.yaml
                         │
                         ▼
              command_loader.py (startup)
                         │
              ┌──────────┴───────────┐
              │                     │
              ▼                     ▼
        COMMANDS dict         validation
        (in memory)           (fail fast)
              │
    ┌─────────┴──────────┐
    │                    │
    ▼                    ▼
handlers.py          modals.py
    │                    │
    │   access_control   │
    │        │           │
    │        ▼           ▼
    │   Slack API    Block Kit
    │   (groups)     modal JSON
    │
    ├── ai_client.py
    │        │
    │        ├── _history dict (in-memory, session timeout)
    │        ├── _last_seen dict (monotonic timestamps)
    │        └── LiteLLM → AI provider → MCP servers
    │
    ├── skill_runner.py  (action_type: skill commands)
    │        │
    │        ├── reads SKILL.md from /mnt/skills/<name>/
    │        └── LiteLLM → AI provider → result string
    │
    ├── webhook_client.py  (action_type: webhook commands)
    │        │
    │        ├── HTTP POST → webhook receiver
    │        └── callback block injected into payload
    │
    └── job_store.py ────────────────────────────┐
             │                                   │
             └── _store dict (token → job)       │
                  ↑ write on webhook fire        │ read on callback
                  (includes notify_channels)     │
                                                 ▼
                              callback_server.py (Flask thread)
                                     │
                                     └── POST /webhook/callback
                                               ↓ rate limit
                                               ↓ payload cap
                                               ↓ token validation
                                         Slack DM to submitter
                                         + Slack post to notify_channels
```

---

## Security Considerations

**Secrets** — all credentials are loaded from environment variables at startup via `require_env()`. The app refuses to start if any required variable is missing. No credentials are hardcoded or committed to source control.

**Webhook URLs** — stored in `commands.yaml` alongside the code. The repository should be private. For public repositories, webhook URLs should be stored as environment variables and resolved at runtime. Furute versions will store these in a database for greater security.

**Access control** — enforced at the application layer before the modal opens. Slack's own slash command restrictions provide a coarse outer layer; the bot's per-command allowlists provide fine-grained control. Denial messages are ephemeral (Slack's `respond()`) so they are visible only to the requesting user.

**User group resolution** — the bot calls `usergroups_list` and `usergroups_users_list` at access check time, not at startup. This ensures group membership is always current (within the 5-minute cache window) rather than stale from when the bot last started.

**Callback endpoint** — protected by three independent layers configured in `settings.yaml`: payload size cap (prevents memory exhaustion), per-IP rate limiting (prevents brute-force and flooding), and one-time tokens with configurable TTL (prevents replay attacks and expired-job callbacks). See decision #11 for detailed reasoning.

**Callback message content** — the `message` field from the workflow receiver is posted directly to Slack. If a receiver is compromised and sends malicious content, it appears in the user's DM. Slack's Block Kit sanitises most injection vectors, but the token should be treated as a secret — it authorises a DM to a real user.

**Ack timing** — Slack's 3-second acknowledgment window means all slow operations (LiteLLM API, webhook call, Slack group resolution) happen after `ack()` is called. This is enforced by convention in every handler and documented explicitly in the handler docstrings. 
