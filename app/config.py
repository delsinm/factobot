"""
config.py
---------
Central configuration module. Loads all environment variables once at startup
and exposes them as named constants used throughout the application.

WHY THIS MODULE EXISTS
----------------------
Centralising config here means:
  - Every other module imports from one place — no scattered os.environ calls
  - Missing variables are caught immediately at startup with a clear error,
    not halfway through a request with a confusing KeyError
  - Defaults and type conversions (e.g. int()) live in one place

REQUIRED vs OPTIONAL VARIABLES
-------------------------------
Required variables raise RuntimeError at startup if absent.
Optional variables return None (or a default) and the relevant feature
is gracefully disabled at runtime rather than crashing.

USAGE
-----
    from app.config import SLACK_BOT_TOKEN, FALLBACK_WEBHOOK_URL

ADDING A NEW VARIABLE
---------------------
Required:               MY_KEY = require_env("MY_KEY")
Optional:               MY_KEY = os.environ.get("MY_KEY")
Optional with default:  MY_KEY = os.environ.get("MY_KEY", "default_value")
"""

import os
from dotenv import load_dotenv

# Load variables from a .env file if one exists.
# In production (Railway, Cloud Run, etc.) variables are injected directly
# into the process environment, so load_dotenv() becomes a no-op — fine.
load_dotenv()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def require_env(key: str) -> str:
    """
    Read a required environment variable and return its value.

    Raises RuntimeError immediately if the variable is missing or empty,
    with a message that names the missing key and tells the developer
    where to add it. This is intentionally loud — a bot that starts
    without its credentials will fail in confusing ways at runtime.

    Args:
        key: The environment variable name, e.g. "SLACK_BOT_TOKEN".

    Returns:
        The variable's string value.

    Raises:
        RuntimeError: If the variable is missing or set to an empty string.
    """
    value = os.environ.get(key)

    if not value:
        raise RuntimeError(
            f"\n"
            f"  Missing required environment variable: {key}\n"
            f"  Add it to your .env file or your deployment environment.\n"
            f"  See .env.example for the full list of required variables.\n"
        )

    return value


# ---------------------------------------------------------------------------
# Required — the app cannot start without these
# ---------------------------------------------------------------------------

# Slack bot OAuth token (starts with xoxb-).
# Found at: api.slack.com/apps → Your App → OAuth & Permissions → Bot User OAuth Token
SLACK_BOT_TOKEN = require_env("SLACK_BOT_TOKEN")

# Slack app-level token for Socket Mode (starts with xapp-).
# Found at: api.slack.com/apps → Your App → Basic Information → App-Level Tokens
SLACK_APP_TOKEN = require_env("SLACK_APP_TOKEN")


# ---------------------------------------------------------------------------
# AI provider API keys — set the one matching ai.model in settings.yaml
# ---------------------------------------------------------------------------
# LiteLLM reads these directly from the environment — no extra wiring needed.
# Only the key for your chosen provider is required. Others can be left unset.

# Anthropic — required when ai.model starts with "anthropic/"
# Found at: console.anthropic.com → API Keys
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# OpenAI — required when ai.model starts with "openai/"
# Found at: platform.openai.com → API Keys
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Google Gemini — required when ai.model starts with "gemini/"
# Found at: aistudio.google.com → Get API Key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


# ---------------------------------------------------------------------------
# Optional — the app starts without these; affected features are disabled
# ---------------------------------------------------------------------------

# PostgreSQL connection string for the database backend.
# Required when using the Scriptorium UI or persisting configuration across
# redeploys. When absent, the bot falls back to settings.yaml / commands.yaml.
#
# Format:  postgresql://user:password@host:5432/dbname?sslmode=require
# Railway: set automatically if you attach a Postgres plugin.
# Supabase / RDS / other managed providers: copy from their connection panel.
#
# The Scriptorium first-run wizard generates and copies this value for you.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Global fallback webhook URL for AI-triggered workflows in conversational mode.
# Each command's webhook URL is defined in commands.yaml.
# When absent, webhook_client.trigger() returns a warning instead of calling out.
# Can point to Make, Zapier, n8n, a custom API, or any webhook-capable receiver.
FALLBACK_WEBHOOK_URL = os.environ.get("FALLBACK_WEBHOOK_URL")

# ---------------------------------------------------------------------------
# Callback server
# ---------------------------------------------------------------------------
# The callback server receives completion notifications from workflow receivers
# after they finish processing a job. Set CALLBACK_BASE_URL to the public URL
# of this bot (e.g. https://your-app.railway.app) to enable callbacks.
# When absent, the bot operates in fire-and-forget mode.

# The public base URL of this bot — used to build the callback URL sent to
# webhook receivers. Do not include a trailing slash.
# Example: https://your-app.railway.app
CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL")

# The port the Flask callback server listens on internally.
# Railway maps this to the public HTTPS URL automatically.
# Only change this if your deployment platform requires a specific port.
CALLBACK_PORT = int(os.environ.get("CALLBACK_PORT", "3000"))
