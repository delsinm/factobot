"""
callback_server.py
------------------
A lightweight Flask HTTP server that receives completion callbacks from
workflow receivers (Make, Zapier, n8n, custom APIs, etc.) and posts the
result to the appropriate Slack user.

HOW IT FITS INTO THE ARCHITECTURE
-----------------------------------
                ┌─────────────────────┐
  Slack ──WS───▶│  Bolt (Socket Mode)  │
                │  main thread         │
                └─────────────────────┘
                ┌─────────────────────┐
  HTTP ────────▶│  Flask (this file)   │
  callbacks     │  background thread   │
                └─────────────────────┘

Both servers share the same process and the same Slack client instance.
The Flask thread reads from job_store and posts to Slack via the shared
client. The threading.Lock() in job_store protects the shared job registry.

CALLBACK ENDPOINT
-----------------
POST /webhook/callback

Expected request body (JSON):
    {
      "callback_token": "Kx9mP2vQ...",   ← echoed from outbound payload
      "status":         "success",        ← "success" or "failure"
      "message":        "Alex Johnson provisioned in Okta and GitHub.",
      "result_url":     "https://acme.okta.com/admin/user/00u1ab",  ← optional
      "fields":         {"Okta group": "engineering", "GitHub org": "acme"}  ← optional
    }

Responses:
    200 {"ok": true}             — callback accepted and Slack notified
    400 {"error": "..."}         — missing required fields
    401 {"error": "..."}         — invalid or expired token
    413 {"error": "..."}         — request body exceeds MAX_PAYLOAD_BYTES
    429 {"error": "..."}         — rate limit exceeded for this IP
    500 {"error": "..."}         — unexpected server error

HEALTH CHECK
------------
GET /health — returns 200 {"status": "ok", "pending_jobs": N}

SECURITY
--------
Three layers of protection on the callback endpoint:

  1. Payload size cap (MAX_PAYLOAD_BYTES from settings.yaml)
     Requests larger than this are rejected before the body is parsed,
     preventing memory exhaustion from oversized payloads.

  2. Per-IP rate limiting (RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW_SECONDS)
     Each IP is allowed at most N requests per window. Exceeding the limit
     returns 429. The sliding window is maintained in-memory per IP.
     All limits are read from settings.yaml and apply immediately on change
     after a restart.

  3. One-time callback token (from job_store)
     Every callback must present the exact token issued when the job was
     created. Tokens expire after CALLBACK_TOKEN_TTL_SECONDS and are
     consumed on first use — replayed requests are rejected.

USAGE
-----
    from app.callback_server import create_app

    flask_app = create_app(slack_client)
    flask_app.run(host="0.0.0.0", port=3000)
"""

import collections
import logging
import threading
import time

from flask import Flask, jsonify, request

from app import job_store
from app.modals import build_callback_result_blocks
from app.settings_loader import (
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    MAX_PAYLOAD_BYTES,
)

logger = logging.getLogger(__name__)

# How often (in requests) to run expired job cleanup.
CLEANUP_INTERVAL = 50
_request_count   = 0

# ---------------------------------------------------------------------------
# In-memory rate limiter
# ---------------------------------------------------------------------------
# Structure: { ip_address (str): deque of monotonic timestamps }
# Each entry holds the timestamps of recent requests from that IP.
# Timestamps older than RATE_LIMIT_WINDOW_SECONDS are pruned on each check.
_rate_limit_store: dict[str, collections.deque] = {}
_rate_limit_lock  = threading.Lock()


def _check_rate_limit(ip: str) -> bool:
    """
    Check whether the given IP address is within the configured rate limit.

    Uses a sliding window: only requests within the last RATE_LIMIT_WINDOW_SECONDS
    count toward the limit. Old timestamps are pruned on each call so the deque
    never grows beyond RATE_LIMIT_REQUESTS + 1 entries.

    All limit values (RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS) are read
    from settings_loader at call time, so changes to settings.yaml take effect
    after the next restart without code changes.

    Args:
        ip: The requesting IP address string (from request.remote_addr).

    Returns:
        True if the request is within the rate limit and should proceed.
        False if the limit has been exceeded and the request should be rejected.
    """
    now    = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS

    with _rate_limit_lock:
        if ip not in _rate_limit_store:
            _rate_limit_store[ip] = collections.deque()

        timestamps = _rate_limit_store[ip]

        # Remove timestamps older than the window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        if len(timestamps) >= RATE_LIMIT_REQUESTS:
            logger.warning(
                "Rate limit exceeded for IP %s: %d requests in %ds window.",
                ip, len(timestamps), RATE_LIMIT_WINDOW_SECONDS,
            )
            return False

        timestamps.append(now)
        return True


def create_app(slack_client) -> Flask:
    """
    Create and configure the Flask application.

    Takes the Slack Web API client as a parameter so callback handlers can
    post messages without importing a global. This keeps the Flask app
    testable in isolation.

    Configures MAX_CONTENT_LENGTH on the Flask app so oversized request
    bodies are rejected by Flask itself before reaching any handler code.

    Args:
        slack_client: The Bolt App's Slack Web API client instance,
                      obtained via app.client in main.py.

    Returns:
        A configured Flask application instance, ready to run.
    """
    flask_app = Flask(__name__)

    # Reject request bodies larger than MAX_PAYLOAD_BYTES before parsing.
    # Flask enforces this automatically and returns 413 for oversized requests.
    # A legitimate callback body (token + status + message) is well under 1 KB.
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_PAYLOAD_BYTES

    # Suppress Flask's default request logging — our structured logger handles it
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @flask_app.post("/webhook/callback")
    def receive_callback():
        """
        Accept a workflow completion callback and notify the Slack user.

        Security checks applied in order:
          1. Payload size — enforced by Flask before this handler runs (413)
          2. Rate limit   — checked per source IP (429)
          3. Token        — validated against job_store (401)

        Validates required fields, redeems the pending job, and posts a
        success or failure DM to the user who triggered the workflow.
        """
        global _request_count
        _request_count += 1

        if _request_count % CLEANUP_INTERVAL == 0:
            job_store.cleanup_expired()

        # --- Rate limit check ---
        client_ip = request.remote_addr or "unknown"

        if not _check_rate_limit(client_ip):
            return jsonify({
                "error": (
                    f"Rate limit exceeded. Maximum {RATE_LIMIT_REQUESTS} requests "
                    f"per {RATE_LIMIT_WINDOW_SECONDS} seconds."
                )
            }), 429

        # --- Parse and validate request body ---
        body = request.get_json(silent=True)

        if not body:
            logger.warning("Callback from %s with no JSON body.", client_ip)
            return jsonify({"error": "Request body must be JSON."}), 400

        token      = body.get("callback_token", "").strip()
        status     = body.get("status", "").strip().lower()
        message    = body.get("message", "").strip()
        result_url = body.get("result_url", "").strip() or None
        fields     = body.get("fields") or None

        # Validate result_url if supplied — must be https to be safe to render
        # as a Slack button URL. Non-https URLs are silently dropped rather than
        # rejected so a misconfigured receiver doesn't break the whole callback.
        if result_url and not result_url.startswith("https://"):
            logger.warning("Callback result_url ignored (not https): %r", result_url)
            result_url = None

        # Validate fields if supplied — must be a dict. Non-dict values are
        # silently dropped so a misconfigured receiver doesn't break the callback.
        if fields is not None and not isinstance(fields, dict):
            logger.warning("Callback fields ignored (not a dict): %r", type(fields).__name__)
            fields = None

        missing = [f for f, v in [("callback_token", token), ("status", status), ("message", message)] if not v]
        if missing:
            logger.warning("Callback missing fields: %s", missing)
            return jsonify({"error": f"Missing required fields: {missing}"}), 400

        if status not in ("success", "failure"):
            return jsonify({"error": "status must be 'success' or 'failure'."}), 400

        # --- Redeem the job token ---
        job = job_store.redeem_job(token)

        if job is None:
            return jsonify({"error": "Invalid or expired callback token."}), 401

        # --- Notify the Slack user ---
        command_name    = job["command_name"]
        channel_id      = job["channel_id"]
        notify_channels = job.get("notify_channels", [])

        logger.info(
            "Callback accepted: command=%s status=%s channel=%s notify_channels=%s result_url=%s fields=%s ip=%s",
            command_name, status, channel_id, notify_channels,
            result_url or "(none)", list(fields.keys()) if fields else "(none)", client_ip,
        )

        result_blocks = build_callback_result_blocks(
            command_name=command_name,
            status=status,
            message=message,
            result_url=result_url,
            fields=fields,
        )
        result_text = (
            f"✅ /{command_name} completed: {message}"
            if status == "success"
            else f"❌ /{command_name} failed: {message}"
        )

        # Post to all target channels: submitter DM first, then notify_channels.
        # Failures on individual channels are logged but do not affect the others
        # or the 200 response — a partial delivery is better than a full retry.
        all_channels = [channel_id] + [c for c in notify_channels if c != channel_id]

        post_errors = []
        for ch in all_channels:
            try:
                slack_client.chat_postMessage(
                    channel=ch,
                    blocks=result_blocks,
                    text=result_text,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to post callback result to channel %s: %s", ch, exc
                )
                post_errors.append(ch)

        if post_errors:
            logger.warning(
                "Callback for command=%s delivered to %d/%d channel(s). "
                "Failed channels: %s",
                command_name,
                len(all_channels) - len(post_errors),
                len(all_channels),
                post_errors,
            )

        return jsonify({"ok": True}), 200

    @flask_app.get("/health")
    def health():
        """
        Health check endpoint. Returns 200 with current server state.

        Reports pending job count, rate limit config, and payload cap so
        operators can confirm the server is running with expected settings.
        """
        return jsonify({
            "status":                    "ok",
            "pending_jobs":              job_store.pending_count(),
            "rate_limit_requests":       RATE_LIMIT_REQUESTS,
            "rate_limit_window_seconds": RATE_LIMIT_WINDOW_SECONDS,
            "max_payload_bytes":         MAX_PAYLOAD_BYTES,
        }), 200

    return flask_app


def run_in_background(slack_client, port: int = 3000) -> threading.Thread:
    """
    Start the Flask callback server in a daemon background thread.

    Using a daemon thread means the Flask server exits automatically when
    the main Bolt process exits — no manual cleanup required.

    Args:
        slack_client: The Bolt App's Slack Web API client.
        port:         The port to listen on. Default 3000. Must match the
                      port Railway exposes and CALLBACK_BASE_URL in config.

    Returns:
        The started daemon Thread.
    """
    flask_app = create_app(slack_client)

    def _run():
        logger.info(
            "Callback server starting on port %d "
            "(rate limit: %d req/%ds, max payload: %dB).",
            port, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS, MAX_PAYLOAD_BYTES,
        )
        flask_app.run(
            host="0.0.0.0",
            port=port,
            threaded=True,
            use_reloader=False,
        )

    thread = threading.Thread(target=_run, name="callback-server", daemon=True)
    thread.start()
    return thread
