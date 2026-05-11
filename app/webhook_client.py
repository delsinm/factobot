"""
webhook_client.py
-----------------
Handles all outbound webhook calls triggered by the bot.

The bot is workflow-tool-agnostic — it fires HTTP POST requests to URLs
defined in commands.yaml and .env. The receiving end can be Make, Zapier,
n8n, a custom API, or any service that accepts a webhook.

CALLBACK BLOCK
--------------
Every outbound payload includes a "callback" object that tells the workflow
receiver exactly how to report back when the job completes. The receiver
simply echoes back the token and adds a status and message — no documentation
to find, no guessing:

    {
      "command": "onboard",
      "name": "Alex Johnson",
      ...
      "callback": {
        "schema_version": "1",
        "url":   "https://your-bot.railway.app/webhook/callback",
        "token": "Kx9mP2vQ...",
        "instructions": "POST to callback.url when the job completes.",
        "example": {
          "callback_token": "Kx9mP2vQ...",
          "status":         "success",
          "message":        "Alex Johnson provisioned in Okta and GitHub."
        }
      }
    }

If CALLBACK_BASE_URL is not configured, the callback block is omitted and
the bot operates in fire-and-forget mode — the initial "triggered" message
is the only feedback the user receives.

WEBHOOK URL PRIORITY
---------------------
  1. webhook_url argument (command-specific, from commands.yaml)
  2. FALLBACK_WEBHOOK_URL from config.py (global fallback)

trigger() never raises — all errors are caught and returned as human-readable
strings so handlers can post them directly to Slack.

USAGE
-----
    from app import webhook_client

    status = webhook_client.trigger(
        payload={"command": "onboard", ...},
        webhook_url="https://your-endpoint.com/hook",
        callback_token="Kx9mP2vQ...",   # from job_store.create_job()
    )
"""

import json
import logging

import requests

from app.config import FALLBACK_WEBHOOK_URL, CALLBACK_BASE_URL

logger = logging.getLogger(__name__)

# The path on the bot's public URL that receives workflow callbacks.
CALLBACK_PATH = "/webhook/callback"


def trigger(
    payload:        dict,
    webhook_url:    str  = None,
    callback_token: str  = None,
    timeout:        int  = 15,
) -> str:
    """
    POST a JSON payload to a webhook URL and return a human-readable status string.

    Injects a "callback" block into the payload if CALLBACK_BASE_URL is
    configured and a callback_token is provided. This gives the workflow
    receiver everything it needs to report back on completion — the URL to
    call, the token to echo, and a worked example of the expected response.

    Resolves the target URL in priority order:
      1. The webhook_url argument (command-specific, from commands.yaml)
      2. FALLBACK_WEBHOOK_URL from config.py (global fallback)

    This function never raises — every failure mode is caught and returned
    as a descriptive string so callers can post it directly to Slack.

    Args:
        payload:        A dict to send as JSON in the request body.
        webhook_url:    Optional. The target URL. Falls back to FALLBACK_WEBHOOK_URL.
        callback_token: Optional. The one-time token from job_store.create_job().
                        If provided and CALLBACK_BASE_URL is set, a "callback"
                        block is injected into the payload.
        timeout:        Request timeout in seconds. Default 15s.

    Returns:
        A human-readable status string with an emoji prefix, e.g.:
          "🔄 Workflow triggered — you'll be notified when it completes."
          "🔄 Workflow triggered successfully."
          "❌ Webhook timed out — please trigger manually."
          "⚠️ No webhook URL configured — workflow not triggered."
    """
    resolved_url = webhook_url or FALLBACK_WEBHOOK_URL

    if not resolved_url:
        logger.warning(
            "No webhook URL provided and FALLBACK_WEBHOOK_URL is not set. "
            "Skipping workflow trigger."
        )
        return "⚠️ No webhook URL configured — workflow not triggered."

    # Build the outbound payload, injecting the callback block if configured
    outbound = _build_payload(payload, callback_token)

    logger.info(
        "POSTing to webhook %s. Callback configured: %s",
        resolved_url,
        bool(callback_token and CALLBACK_BASE_URL),
    )

    try:
        response = _post(resolved_url, outbound, timeout)
        return _parse_response(response, has_callback=bool(callback_token and CALLBACK_BASE_URL))

    except requests.exceptions.Timeout:
        logger.error("Webhook at %s timed out after %ds.", resolved_url, timeout)
        return "❌ Webhook timed out — please trigger manually."

    except requests.exceptions.HTTPError as error:
        status_code = error.response.status_code
        logger.error("Webhook at %s returned HTTP %s.", resolved_url, status_code)
        return f"❌ Webhook returned HTTP {status_code}."

    except requests.exceptions.RequestException as error:
        logger.error("Network error reaching webhook at %s: %s", resolved_url, error)
        return f"❌ Failed to reach webhook: {str(error)}"

    except json.JSONDecodeError:
        logger.error(
            "Webhook at %s returned non-JSON: %s",
            resolved_url,
            response.text[:200],
        )
        return "❌ Webhook returned an unexpected response format."


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_payload(payload: dict, callback_token: str | None) -> dict:
    """
    Build the final outbound payload, optionally injecting a callback block.

    The callback block gives the workflow receiver everything it needs to
    report back on completion — self-contained, no external documentation
    required. The "example" field is especially useful for workflow tools
    like Make that can parse a sample payload to auto-generate field mappings.

    If CALLBACK_BASE_URL is not set or no token is provided, the original
    payload is returned unchanged and the bot runs in fire-and-forget mode.

    Args:
        payload:        The original payload dict from the handler.
        callback_token: The one-time job token, or None.

    Returns:
        A copy of the payload, with "callback" injected if applicable.
    """
    if not callback_token or not CALLBACK_BASE_URL:
        return payload

    callback_url = CALLBACK_BASE_URL.rstrip("/") + CALLBACK_PATH

    # Build self-documenting callback block.
    # The "example" field shows the receiver exactly what to POST back —
    # particularly useful for workflow builders who open the incoming
    # payload in their HTTP module and need to map fields by example.
    callback_block = {
        "schema_version": "1",
        "url":   callback_url,
        "token": callback_token,
        "instructions": (
            "When the job completes, POST to callback.url with the fields "
            "shown in callback.example. Use callback.token as-is. "
            "Set status to 'success' or 'failure'. "
            "Set message to a human-readable description of what happened."
        ),
        "example": {
            "callback_token": callback_token,
            "status":         "success",
            "message":        "Job completed successfully.",
        },
    }

    return {**payload, "callback": callback_block}


def _post(url: str, payload: dict, timeout: int) -> requests.Response:
    """
    Send the HTTP POST request and return the raw Response object.

    Calls raise_for_status() so HTTP errors (4xx, 5xx) surface as HTTPError
    exceptions caught by trigger()'s except blocks.

    Args:
        url:     The webhook URL to POST to.
        payload: The dict to serialise as JSON in the request body.
        timeout: Request timeout in seconds.

    Returns:
        The requests.Response object from the webhook receiver.

    Raises:
        requests.exceptions.HTTPError:        For 4xx or 5xx responses.
        requests.exceptions.Timeout:          If the request exceeds timeout.
        requests.exceptions.RequestException: For any other network failure.
    """
    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def _parse_response(response: requests.Response, has_callback: bool) -> str:
    """
    Interpret the webhook response body and return an appropriate status string.

    When a callback is configured, the initial "triggered" message tells the
    user to expect a follow-up notification rather than implying the work
    is already done. Without a callback, the message reflects the final state.

    Handles the most common response shapes from webhook receivers:

      1. Empty body           — fire-and-forget, no response body
      2. {"accepted": true}   — standard fire-and-forget acknowledgment
      3. {"status": "success"} — explicit success from the receiver
      4. {"status": "error", "message": "..."} — application-level error
      5. Anything else        — unexpected but not a network failure

    Args:
        response:     The Response object from _post().
        has_callback: True if a callback token was injected into the payload.

    Returns:
        A human-readable status string.

    Raises:
        json.JSONDecodeError: If the body is non-empty but not valid JSON.
                              Caught by trigger()'s except block.
    """
    # The pending message differs based on whether we expect a callback
    pending_message = (
        "🔄 Workflow triggered — you'll be notified here when it completes."
        if has_callback
        else "🔄 Workflow triggered successfully."
    )

    # Case 1: Empty body
    if not response.text.strip():
        return pending_message

    result = response.json()

    # Case 4: Application-level error reported by the receiver
    if result.get("status") == "error":
        error_message = result.get("message", "No details provided.")
        logger.error("Webhook receiver returned an error: %s", error_message)
        return f"❌ Webhook error: {error_message}"

    # Cases 2 & 3: Standard acceptance/success responses
    if result.get("accepted") or result.get("status") == "success":
        return pending_message

    # Case 5: Unexpected but non-error response
    logger.warning("Unexpected webhook response: %s", json.dumps(result))
    return f"⚠️ Webhook responded: {json.dumps(result)}"
