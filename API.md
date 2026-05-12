# Factobot API Reference

Factobot exposes two HTTP endpoints on its callback server. Both are served from the same process as the Slack bot, on the port configured by `CALLBACK_PORT `(default `3000`) and reachable at the public URL set in `CALLBACK_BASE_URL`.

These endpoints are intended for **workflow receivers** — Make, Zapier, n8n, or any custom service that Factobot fires webhooks at. They are not user-facing and require no authentication header; security is handled via time-limited, one-time tokens (see [Security](#security) below).

> [!NOTE]
> Flask does not support handling of HTTPS traffic. Please deploy Factobot behind an proxy server or an ALB to ensure that all traffic from your Workflow tool to Factobot is encrypted in transit.

---

## Endpoints


| Method | Path                | Purpose                                       |
| ------ | ------------------- | --------------------------------------------- |
| `POST` | `/webhook/callback` | Report the result of a completed workflow job |
| `GET`  | `/health`           | Check that the server is running              |


---

## POST /webhook/callback

Report the outcome of a workflow job back to Factobot. The bot will DM the result to the user who submitted the original form, and optionally post to any configured notification channels.

### How to get the URL and token

When Factobot fires a webhook it injects a self-documenting `callback` block into the outbound payload:

```json
{
  "command":      "onboard",
  "name":         "Alex Johnson",
  "role":         "Engineer",
  "requested_by": "U012AB3CD",
  "callback": {
    "schema_version": "1",
    "url":   "https://your-bot.railway.app/webhook/callback",
    "token": "Kx9mP2vQrT8sN3uL...",
    "instructions": "When the job completes, POST to callback.url with the fields shown in callback.example. Use callback.token as-is. Set status to 'success' or 'failure'. Set message to a human-readable description of what happened. Set next_action to a command name (e.g. 'provision-hardware') to prompt the user to run that command as their next step; omit if not needed.",
    "example": {
      "callback_token": "Kx9mP2vQrT8sN3uL...",
      "status":         "success",
      "message":        "Job completed successfully.",
      "next_action":    "next-command-name"
    }
  }
}
```

Use `callback.url` as the request URL and echo `callback.token` back as `callback_token` in the request body. Everything you need is in the payload you already received — no separate documentation lookup required.

### Request

**Content-Type:** `application/json`

**Body fields:**


| Field            | Type   | Required | Description                                                                                                                                                                                                                                                                    |
| ---------------- | ------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `callback_token` | string | Yes      | The one-time token from `callback.token` in the outbound payload.                                                                                                                                                                                                              |
| `status`         | string | Yes      | `"success"` or `"failure"`.                                                                                                                                                                                                                                                    |
| `message`        | string | Yes      | Human-readable description of what happened. Shown directly to the user in Slack.                                                                                                                                                                                              |
| `result_url`     | string | No       | HTTPS URL to the resource created or affected by the workflow (e.g. a Jira ticket, Okta user profile). Rendered as a **View Result** button in the Slack message. Non-HTTPS URLs are silently ignored.                                                                         |
| `fields`         | object | No       | Key/value pairs summarising the workflow outcome (e.g. `{"Okta group": "engineering", "GitHub org": "acme"}`). Rendered as a two-column summary block in Slack. Values are coerced to strings. Maximum 10 pairs; extras are truncated. Non-object values are silently ignored. |
| `next_action`    | string | No       | Name of a Factobot command to prompt the user to run next (e.g. `"provision-hardware"`). Only sent when `status` is `"success"`. The command must exist in `commands.yaml` — an unknown name is rejected with `400` before any Slack messages are sent.                        |


**Minimal example:**

```json
{
  "callback_token": "Kx9mP2vQrT8sN3uL...",
  "status":         "success",
  "message":        "Alex Johnson provisioned in Okta and GitHub."
}
```

**Full example:**

```json
{
  "callback_token": "Kx9mP2vQrT8sN3uL...",
  "status":         "success",
  "message":        "Alex Johnson provisioned in Okta and GitHub.",
  "result_url":     "https://acme.okta.com/admin/user/00u1ab2cd3EFGhIJK4x6",
  "fields": {
    "Okta username": "ajohnson",
    "Okta group":    "engineering",
    "GitHub org":    "acme",
    "Hardware tier": "developer"
  },
  "next_action": "provision-hardware"
}
```

### Responses


| Status | Body                                              | Meaning                                                                                   |
| ------ | ------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `200`  | `{"ok": true}`                                    | Callback accepted. Slack has been notified.                                               |
| `400`  | `{"error": "..."}`                                | Missing required fields, invalid `status` value, non-JSON body, or unknown `next_action`. |
| `401`  | `{"error": "Invalid or expired callback token."}` | Token not recognised or already redeemed.                                                 |
| `413`  | `{"error": "..."}`                                | Request body exceeds `max_payload_bytes` (default 8 KB).                                  |
| `429`  | `{"error": "..."}`                                | Rate limit exceeded for this IP address.                                                  |
| `500`  | `{"error": "..."}`                                | Unexpected server error.                                                                  |


A `200` response means the bot received and accepted the callback. It does not guarantee that the Slack message was delivered — delivery failures are logged server-side but do not affect the HTTP response, since the workflow job was complete regardless.

### What the user sees

**On `status: "success"`:**

> ✅ **/onboard completed**  
> Alex Johnson provisioned in Okta and GitHub.
> | Okta username | ajohnson |  
> | Okta group | engineering |
> [View Result]  ← button, if result_url was supplied

**On `status: "failure"`:**

> ❌ **/onboard failed**  
> Provisioning failed — Okta returned a duplicate user error.

**If `next_action` was supplied (success only):**

A separate DM is sent to the submitter immediately after the result:

> **Next step:** Provision hardware for a new hire  
> Run `/provision-hardware` when you're ready to continue.

---

## GET /health

Returns the current state of the callback server. Use this to confirm the server is running and to check its active configuration.

### Request

No body or parameters required.

### Response

`**200 OK**`

```json
{
  "status":                    "ok",
  "pending_jobs":              3,
  "rate_limit_requests":       20,
  "rate_limit_window_seconds": 60,
  "max_payload_bytes":         8192
}
```


| Field                       | Type    | Description                                                        |
| --------------------------- | ------- | ------------------------------------------------------------------ |
| `status`                    | string  | Always `"ok"` when the server is running.                          |
| `pending_jobs`              | integer | Number of jobs currently registered and waiting for a callback.    |
| `rate_limit_requests`       | integer | Maximum requests per IP per window, from `settings.yaml`.          |
| `rate_limit_window_seconds` | integer | Sliding window duration in seconds, from `settings.yaml`.          |
| `max_payload_bytes`         | integer | Maximum accepted request body size in bytes, from `settings.yaml`. |


---

## Security

The callback endpoint uses three independent layers of protection, all configurable in `settings.yaml` under `security.callback`:

**Payload size cap** (`max_payload_bytes`, default 8192)  
Flask rejects request bodies larger than this limit before the handler runs, returning `413`. A legitimate callback body is well under 1 KB — this limit prevents memory exhaustion from oversized payloads.

**Per-IP rate limiting** (`rate_limit_requests` / `rate_limit_window_seconds`, default 20 req/60s) 

Each source IP is allowed at most `rate_limit_requests` requests within any `rate_limit_window_seconds` sliding window. Exceeding the limit returns `429`. The window is in-memory and resets on bot restart.

**One-time token with TTL** (`bot.callback_token_ttl_minutes`, default 60)  
Every callback must present the exact token issued when the job was created. Tokens are 32 URL-safe characters (192 bits of entropy). They are consumed on first use — replaying a valid token returns `401`. Tokens also expire after the configured TTL, even if never redeemed — an expired token returns `401`.

All three limits can be tightened or relaxed in `settings.yaml` without code changes; they take effect after the next restart.

---

## Fire-and-forget mode

If `CALLBACK_BASE_URL` is not set in the environment, Factobot runs in fire-and-forget mode: webhooks are fired but no `callback` block is injected into the payload and the `/webhook/callback` endpoint receives no traffic. In this mode the submitter's confirmation DM reads:

> 🔄 Workflow triggered successfully.

rather than the usual "you'll be notified when it completes" message.

---

## Configuring limits

All callback server limits are set in `settings.yaml`:

```yaml
bot:
  callback_token_ttl_minutes: 60   # token expiry

security:
  callback:
    rate_limit_requests:       20   # max POSTs per IP per window
    rate_limit_window_seconds: 60   # sliding window duration
    max_payload_bytes:         8192  # max request body size
```

Changes take effect after the next bot restart.