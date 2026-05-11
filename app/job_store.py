"""
job_store.py
------------
In-memory registry of pending webhook jobs, keyed by ephemeral callback tokens.

WHAT THIS MODULE DOES
---------------------
When the bot fires an outbound webhook it generates a one-time callback token
and registers the job here. When the workflow receiver calls back, the bot
looks up the token to find which Slack user to notify and with which context.

TOKEN DESIGN
------------
Tokens are generated with secrets.token_urlsafe(24), producing 32 URL-safe
characters with 192 bits of entropy — unguessable and well within the 64-char
budget for readable payloads. Tokens are single-use: deleted on first redemption
so replayed requests are rejected.

TTL EXPIRY
----------
Jobs expire after CALLBACK_TOKEN_TTL_SECONDS, read from settings.yaml
(bot.callback_token_ttl_minutes, default 60). Once expired, a callback is
rejected even if the token is correct. Set the TTL based on your slowest
expected workflow — it should comfortably exceed your worst-case duration.

Expiry is checked lazily at redemption time and on periodic cleanup — no
background thread required.

THREAD SAFETY
-------------
The store is accessed from two threads:
  - Bolt Socket Mode thread (writes on webhook fire)
  - Flask callback thread (reads/deletes on callback receipt)

A threading.Lock() guards all mutations to prevent race conditions.

USAGE
-----
    from app.job_store import create_job, redeem_job

    token = create_job("onboard", "U012AB3CD", "U012AB3CD")

    job = redeem_job(token)   # None if unknown or expired
"""

import logging
import secrets
import threading
import time

logger = logging.getLogger(__name__)


def _get_ttl() -> int:
    """
    Return the configured callback token TTL in seconds from settings_loader.

    Imported at call time (not module load time) to avoid circular import
    issues during startup. settings_loader imports nothing from app/, so
    there is no actual dependency cycle — this is purely a load-order concern.
    """
    from app.settings_loader import CALLBACK_TOKEN_TTL_SECONDS
    return CALLBACK_TOKEN_TTL_SECONDS


# The token store.
# Structure: { token (str): job_record (dict) }
# job_record keys:
#   command_name  — the slash command that triggered the job
#   user_id       — Slack user ID of the person who submitted the form
#   channel_id    — Slack channel/DM to send the result notification to
#   created_at    — monotonic timestamp of job creation (for TTL checks)
_store: dict[str, dict] = {}

# Protects all mutations to _store from concurrent access between the
# Bolt thread (writes) and the Flask callback thread (reads/deletes).
_lock = threading.Lock()


def create_job(command_name: str, user_id: str, channel_id: str,
               notify_channels: list[str] | None = None) -> str:
    """
    Register a new pending job and return its one-time callback token.

    Args:
        command_name:     The slash command that triggered this job (e.g. "onboard").
        user_id:          Slack user ID of the submitter. Used for audit logging.
        channel_id:       Slack channel or DM to post the result to.
        notify_channels:  Optional list of additional Slack channel IDs to notify
                          when the job completes. These are posted to in addition
                          to the DM sent to channel_id.

    Returns:
        A 32-character URL-safe token string to include in the outbound payload
        as callback.token.
    """
    token = secrets.token_urlsafe(24)   # 32 chars, 192 bits of entropy

    with _lock:
        _store[token] = {
            "command_name":    command_name,
            "user_id":         user_id,
            "channel_id":      channel_id,
            "notify_channels": notify_channels or [],
            "created_at":      time.monotonic(),
        }

    logger.info(
        "Job registered: command=%s user=%s token=%s… TTL=%ds notify_channels=%s",
        command_name, user_id, token[:8], _get_ttl(), notify_channels or [],
    )

    return token


def redeem_job(token: str) -> dict | None:
    """
    Look up and consume a pending job by its callback token.

    Single-use: the job is removed immediately on redemption so replayed
    requests are rejected. TTL is checked against the value currently
    configured in settings.yaml — changes take effect without a restart.

    Args:
        token: The callback token received from the workflow receiver.

    Returns:
        The job record dict if valid and unexpired, otherwise None.
    """
    with _lock:
        job = _store.pop(token, None)

    if job is None:
        logger.warning("Callback with unknown or already-redeemed token.")
        return None

    age_seconds = time.monotonic() - job["created_at"]
    ttl         = _get_ttl()

    if age_seconds > ttl:
        logger.warning(
            "Token expired: age=%.0fmin TTL=%dmin. Rejecting.",
            age_seconds / 60,
            ttl // 60,
        )
        return None

    logger.info(
        "Job redeemed: command=%s user=%s age=%.0fs",
        job["command_name"], job["user_id"], age_seconds,
    )

    return job


def cleanup_expired() -> int:
    """
    Remove all expired jobs from the store and return the count removed.

    Called periodically by the Flask server to prevent unbounded memory
    growth if workflow receivers never call back. Thread-safe.

    Returns:
        The number of expired jobs removed.
    """
    now = time.monotonic()
    ttl = _get_ttl()

    with _lock:
        expired = [t for t, j in _store.items() if (now - j["created_at"]) > ttl]
        for token in expired:
            del _store[token]

    if expired:
        logger.info("Cleaned up %d expired job(s).", len(expired))

    return len(expired)


def pending_count() -> int:
    """
    Return the number of jobs currently waiting for a callback.

    Used for health checks. Thread-safe.
    """
    with _lock:
        return len(_store)
