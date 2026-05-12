"""
settings_loader.py
------------------
Reads settings.yaml at startup and exposes global bot configuration to the
rest of the application.

WHAT THIS MODULE OWNS
----------------------
settings.yaml contains everything that is safe to commit and relevant to how
the bot presents and behaves — not secrets, not workflow definitions:

  bot.name                       → BOT_NAME
  bot.max_history                → MAX_HISTORY
  bot.session_timeout_hours      → SESSION_TIMEOUT_SECONDS
  bot.callback_token_ttl_minutes → CALLBACK_TOKEN_TTL_SECONDS
  ai.model                       → AI_MODEL
  icons.info / ack / error       → ICONS

command_loader.py owns commands.yaml (workflows, webhooks, access rules).
config.py owns environment variables (tokens, API keys).

WHAT THIS MODULE EXPOSES
-------------------------
  BOT_NAME                   — slash command name, e.g. "factobot" → /factobot
  MAX_HISTORY                — max messages kept per user between API calls
  SESSION_TIMEOUT_SECONDS    — inactivity window in seconds before history clears
  CALLBACK_TOKEN_TTL_SECONDS — how long a callback token stays valid (in seconds)
  AI_MODEL                   — LiteLLM model string
  MCP_SERVERS                — configured at the AI provider level, not here
  ICONS                      — dict with keys "info", "ack", "error" (str | None)

VALIDATION
----------
Missing or invalid values for bot.name, bot.max_history, bot.session_timeout_hours,
and bot.callback_token_ttl_minutes raise ValueError at startup with a clear message.
Icons are optional — missing URLs degrade to None (no image shown).

USAGE
-----
    from app.settings_loader import (
        BOT_NAME, MAX_HISTORY, SESSION_TIMEOUT_SECONDS,
        CALLBACK_TOKEN_TTL_SECONDS, AI_MODEL, ICONS,
    )
"""

import logging
import pathlib

import yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = pathlib.Path(__file__).parent.parent / "settings.yaml"

# Defaults used only when settings.yaml is absent entirely.
_DEFAULT_BOT_NAME                   = "factobot"
_DEFAULT_MAX_HISTORY                = 6
_DEFAULT_SESSION_TIMEOUT_HOURS      = 4
_DEFAULT_CALLBACK_TOKEN_TTL_MINUTES = 60
_DEFAULT_AI_MODEL                   = "anthropic/claude-sonnet-4-20250514"
_DEFAULT_ICONS                      = {"info": None, "ack": None, "error": None}
_DEFAULT_RATE_LIMIT_REQUESTS        = 20
_DEFAULT_RATE_LIMIT_WINDOW_SECONDS  = 60
_DEFAULT_MAX_PAYLOAD_BYTES          = 8192


# ---------------------------------------------------------------------------
# Top-level loader
# ---------------------------------------------------------------------------

def _load_settings() -> tuple[str, int, int, int, str, dict, int, int, int]:
    """
    Read settings.yaml and return all configuration values as a tuple.

    If settings.yaml does not exist, logs a warning and returns safe defaults
    so the bot can start in minimal environments (e.g. a bare clone for testing).

    Raises:
        yaml.YAMLError: If the file exists but contains invalid YAML syntax.
        ValueError:     If any required bot.* or ai.* value is missing or invalid.

    Returns:
        (bot_name, max_history, session_timeout_seconds, callback_token_ttl_seconds,
         ai_model, icons, rate_limit_requests, rate_limit_window_seconds, max_payload_bytes)
    """

    if not _SETTINGS_PATH.exists():
        logger.warning(
            "settings.yaml not found at %s — using built-in defaults. "
            "Create settings.yaml to configure the bot.",
            _SETTINGS_PATH,
        )
        return (
            _DEFAULT_BOT_NAME,
            _DEFAULT_MAX_HISTORY,
            _DEFAULT_SESSION_TIMEOUT_HOURS * 3600,
            _DEFAULT_CALLBACK_TOKEN_TTL_MINUTES * 60,
            _DEFAULT_AI_MODEL,
            dict(_DEFAULT_ICONS),
            _DEFAULT_RATE_LIMIT_REQUESTS,
            _DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            _DEFAULT_MAX_PAYLOAD_BYTES,
        )

    logger.info("Loading settings from %s", _SETTINGS_PATH)

    with open(_SETTINGS_PATH, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)

    if not isinstance(raw, dict):
        raise ValueError(
            "settings.yaml is empty or not a valid YAML mapping. "
            "See settings.yaml for the expected structure."
        )

    bot_section = raw.get("bot", {})

    if not isinstance(bot_section, dict):
        raise ValueError(
            "settings.yaml: 'bot' must be a mapping. "
            "Example:\n  bot:\n    name: factobot\n    max_history: 4\n"
            "    session_timeout_hours: 4\n    callback_token_ttl_minutes: 60"
        )

    bot_name                   = _parse_bot_name(bot_section)
    max_history                = _parse_max_history(bot_section)
    session_timeout_hours      = _parse_session_timeout(bot_section)
    callback_token_ttl_minutes = _parse_callback_token_ttl(bot_section)
    ai_model                   = _parse_ai_model(raw)
    icons                      = _parse_icons(raw)
    rate_limit_requests, rate_limit_window_seconds, max_payload_bytes = _parse_security(raw)

    logger.info(
        "Settings loaded — bot_name=%r, max_history=%d, "
        "session_timeout=%dh, callback_token_ttl=%dmin, "
        "ai_model=%r, icons=%s, rate_limit=%d req/%ds, max_payload=%dB",
        bot_name, max_history,
        session_timeout_hours, callback_token_ttl_minutes,
        ai_model, [k for k, v in icons.items() if v],
        rate_limit_requests, rate_limit_window_seconds, max_payload_bytes,
    )

    return (
        bot_name,
        max_history,
        session_timeout_hours * 3600,
        callback_token_ttl_minutes * 60,
        ai_model,
        icons,
        rate_limit_requests,
        rate_limit_window_seconds,
        max_payload_bytes,
    )


# ---------------------------------------------------------------------------
# Per-field parsers
# ---------------------------------------------------------------------------

def _parse_bot_name(bot: dict) -> str:
    """
    Extract and validate bot.name from the bot config section.

    The name is lowercased and stripped. An empty or missing value raises
    ValueError so the bot fails loudly at startup rather than registering
    an incorrect slash command.

    Args:
        bot: The parsed value of the top-level "bot" key.

    Returns:
        A non-empty lowercase bot name string.

    Raises:
        ValueError: If bot.name is missing, empty, or whitespace-only.
    """
    raw_name = bot.get("name", "")
    name     = str(raw_name).strip().lower() if raw_name else ""

    if not name:
        raise ValueError(
            "settings.yaml: bot.name is required but missing or empty.\n"
            "Set it to match your Slack slash command name, e.g.:\n"
            "  bot:\n    name: factobot"
        )

    return name


def _parse_max_history(bot: dict) -> int:
    """
    Extract and validate bot.max_history from the bot config section.

    Must be a positive integer. Raises ValueError with a descriptive message
    if the value is missing, not an integer, or less than 1.

    Args:
        bot: The parsed value of the top-level "bot" key.

    Returns:
        A positive integer for the max history message count.

    Raises:
        ValueError: If bot.max_history is missing, not an integer, or < 1.
    """
    raw = bot.get("max_history")

    if raw is None:
        raise ValueError(
            "settings.yaml: bot.max_history is required.\n"
            "Recommended value: 6 (3 full conversation exchanges).\n"
            "  bot:\n    max_history: 6"
        )

    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"settings.yaml: bot.max_history must be an integer, got {raw!r}."
        )

    if raw < 1:
        raise ValueError(
            f"settings.yaml: bot.max_history must be at least 1, got {raw}."
        )

    return raw


def _parse_session_timeout(bot: dict) -> int:
    """
    Extract and validate bot.session_timeout_hours from the bot config section.

    Must be a positive integer representing hours. The caller multiplies by
    3600 to convert to seconds for use in ai_client.py.

    Args:
        bot: The parsed value of the top-level "bot" key.

    Returns:
        A positive integer number of hours.

    Raises:
        ValueError: If bot.session_timeout_hours is missing, not an integer, or < 1.
    """
    raw = bot.get("session_timeout_hours")

    if raw is None:
        raise ValueError(
            "settings.yaml: bot.session_timeout_hours is required.\n"
            "Recommended value: 4 (clears history after 4 hours of inactivity).\n"
            "  bot:\n    session_timeout_hours: 4"
        )

    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"settings.yaml: bot.session_timeout_hours must be an integer, got {raw!r}."
        )

    if raw < 1:
        raise ValueError(
            f"settings.yaml: bot.session_timeout_hours must be at least 1, got {raw}."
        )

    return raw


def _parse_callback_token_ttl(bot: dict) -> int:
    """
    Extract and validate bot.callback_token_ttl_minutes from the bot config.

    Must be a positive integer representing minutes. The caller multiplies by
    60 to convert to seconds for use in job_store.py.

    Args:
        bot: The parsed value of the top-level "bot" key.

    Returns:
        A positive integer number of minutes.

    Raises:
        ValueError: If the value is missing, not an integer, or less than 1.
    """
    raw = bot.get("callback_token_ttl_minutes")

    if raw is None:
        raise ValueError(
            "settings.yaml: bot.callback_token_ttl_minutes is required.\n"
            "Recommended value: 60 (tokens expire after 60 minutes).\n"
            "  bot:\n    callback_token_ttl_minutes: 60"
        )

    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"settings.yaml: bot.callback_token_ttl_minutes must be an integer, got {raw!r}."
        )

    if raw < 1:
        raise ValueError(
            f"settings.yaml: bot.callback_token_ttl_minutes must be at least 1, got {raw}."
        )

    return raw


def _parse_ai_model(raw: dict) -> str:
    """
    Extract and validate ai.model from the top-level settings dict.

    The model string is passed directly to LiteLLM, which uses the provider
    prefix to route to the correct backend. The format is always:
        "provider/model-name"

    e.g. "openai/gpt-4o"
         "anthropic/claude-sonnet-4-20250514"
         "gemini/gemini-2.0-flash"

    Raises ValueError if ai.model is missing or empty, since running the bot
    without a configured model would fail on the first message anyway — better
    to fail loudly at startup.

    Args:
        raw: The full top-level parsed dict from settings.yaml.

    Returns:
        A non-empty model string.

    Raises:
        ValueError: If ai.model is missing or empty.
    """
    ai_section = raw.get("ai", {})

    if not isinstance(ai_section, dict):
        raise ValueError(
            "settings.yaml: 'ai' must be a mapping.\n"
            "Example:\n  ai:\n    model: openai/gpt-4o"
        )

    model = str(ai_section.get("model", "")).strip()

    if not model:
        raise ValueError(
            "settings.yaml: ai.model is required but missing or empty.\n"
            "Example:\n  ai:\n    model: openai/gpt-4o\n"
            "Full provider list: https://docs.litellm.ai/docs/providers"
        )

    return model


def _parse_security(raw: dict) -> tuple[int, int, int]:
    """
    Extract callback server security settings from the top-level security block.

    All three values are optional with safe defaults — a missing or incomplete
    security block degrades gracefully rather than failing startup.

    Validates that all values are positive integers. Logs a warning and uses
    the default if a value is present but invalid (e.g. set to 0 or a string).

    Args:
        raw: The full top-level parsed dict from settings.yaml.

    Returns:
        A (rate_limit_requests, rate_limit_window_seconds, max_payload_bytes)
        tuple of positive integers.
    """
    security = raw.get("security", {})
    callback = security.get("callback", {}) if isinstance(security, dict) else {}

    if not isinstance(callback, dict):
        callback = {}

    def _pos_int(key: str, default: int) -> int:
        """Return a positive integer from callback config, or the default."""
        val = callback.get(key)
        if val is None:
            return default
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            logger.warning(
                "settings.yaml: security.callback.%s must be a positive integer "
                "(got %r) — using default %d.",
                key, val, default,
            )
            return default
        return val

    rate_limit_requests       = _pos_int("rate_limit_requests",       _DEFAULT_RATE_LIMIT_REQUESTS)
    rate_limit_window_seconds = _pos_int("rate_limit_window_seconds",  _DEFAULT_RATE_LIMIT_WINDOW_SECONDS)
    max_payload_bytes         = _pos_int("max_payload_bytes",          _DEFAULT_MAX_PAYLOAD_BYTES)

    return rate_limit_requests, rate_limit_window_seconds, max_payload_bytes


def _parse_icons(raw: dict) -> dict:
    """
    Extract icon URLs from the top-level icons section of settings.yaml.

    All three icons (info, ack, error) are optional. Missing keys, empty
    strings, and whitespace-only values all produce None. Callers guard
    icon usage with a simple if-check so messages degrade gracefully to
    no-image rather than raising an error.

    Args:
        raw: The full top-level parsed dict from settings.yaml.

    Returns:
        A dict with keys "info", "ack", "error" mapping to str | None.
    """
    icons_section = raw.get("icons", {})

    if not isinstance(icons_section, dict):
        logger.warning(
            "settings.yaml: 'icons' is not a mapping — skipping icons."
        )
        return dict(_DEFAULT_ICONS)

    def _clean(value) -> str | None:
        """Return the stripped URL string if non-empty, otherwise None."""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return {
        "info":  _clean(icons_section.get("info")),
        "ack":   _clean(icons_section.get("ack")),
        "error": _clean(icons_section.get("error")),
    }


# ---------------------------------------------------------------------------
# Module-level exports — loaded once at import time
# ---------------------------------------------------------------------------

# BOT_NAME: slash command name from settings.yaml → bot.name
# MAX_HISTORY: max messages per user from settings.yaml → bot.max_history
# SESSION_TIMEOUT_SECONDS: inactivity window from bot.session_timeout_hours × 3600
# CALLBACK_TOKEN_TTL_SECONDS: token lifetime from bot.callback_token_ttl_minutes × 60
# AI_MODEL: LiteLLM model string from settings.yaml → ai.model
# ICONS: icon URLs from settings.yaml → icons.*
# RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS, MAX_PAYLOAD_BYTES: security settings
# Note: MCP servers are configured at the AI provider level, not in settings.yaml
BOT_NAME: str
MAX_HISTORY: int
SESSION_TIMEOUT_SECONDS: int
CALLBACK_TOKEN_TTL_SECONDS: int
AI_MODEL: str
ICONS: dict
RATE_LIMIT_REQUESTS: int
RATE_LIMIT_WINDOW_SECONDS: int
MAX_PAYLOAD_BYTES: int

(BOT_NAME, MAX_HISTORY, SESSION_TIMEOUT_SECONDS, CALLBACK_TOKEN_TTL_SECONDS,
 AI_MODEL, ICONS,
 RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS, MAX_PAYLOAD_BYTES) = _load_settings()
