"""
main.py
-------
Application entry point. Creates the Slack Bolt App, registers all event
handlers, starts the Flask callback server, and opens the Socket Mode
connection.

HOW STARTUP WORKS
-----------------
1. config.py is imported — all required environment variables are validated.
   If any are missing, the app raises RuntimeError and exits before touching
   Slack or starting Flask.

2. The Bolt App is created with the bot token from config.

3. handlers.register(app) attaches all event, command, action, and view
   handlers to the app instance.

4. The Flask callback server is started in a daemon background thread on
   CALLBACK_PORT (default 3000). This receives completion notifications from
   workflow receivers and DMs the result to the submitter.

5. SocketModeHandler opens the persistent WebSocket connection to Slack.
   The bot is now live and processing events.

TWO SERVERS, ONE PROCESS
-------------------------
The bot runs two servers simultaneously:

  Bolt (Socket Mode) — outbound WebSocket to Slack, main thread.
                       Handles all Slack events, commands, and modals.

  Flask (HTTP)       — inbound HTTP server, background daemon thread.
                       Receives workflow completion callbacks and posts
                       results to Slack via the shared app.client.

Both share the same Slack client instance (app.client). A threading.Lock()
in job_store.py protects the shared job registry from race conditions.

CALLBACK SERVER
---------------
The callback server is only started if CALLBACK_BASE_URL is set in the
environment. If not set, the bot runs in fire-and-forget mode — webhooks
are fired but the bot never receives completion notifications. This allows
the bot to run locally without exposing a public URL.

RUN
---
    python main.py
"""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, CALLBACK_BASE_URL, CALLBACK_PORT
from app import handlers

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Slack Bolt App
# ---------------------------------------------------------------------------
app = App(token=SLACK_BOT_TOKEN)
handlers.register(app)

# ---------------------------------------------------------------------------
# Callback server (Flask, background thread)
# ---------------------------------------------------------------------------
if CALLBACK_BASE_URL:
    from app.callback_server import run_in_background
    run_in_background(slack_client=app.client, port=CALLBACK_PORT)
    logger.info(
        "Callback server started on port %d. Public URL: %s/webhook/callback",
        CALLBACK_PORT,
        CALLBACK_BASE_URL.rstrip("/"),
    )
else:
    logger.info(
        "CALLBACK_BASE_URL not set — running in fire-and-forget mode. "
        "Set CALLBACK_BASE_URL to enable workflow completion notifications."
    )

# ---------------------------------------------------------------------------
# Start Socket Mode
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting bot in Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
