"""
handlers.py
-----------
Registers all Slack Bolt event, command, and view handlers onto the App instance.

HOW COMMAND ROUTING WORKS
--------------------------
The bot now exposes a single slash command — /factobot — which accepts an optional
subcommand as its first word:

  /factobot              → shows a list of all available subcommands
  /factobot onboard      → opens the onboarding form modal
  /factobot offboard     → opens the offboarding form modal
  /factobot update-access → opens the update-access form modal
  /factobot list-access  → opens the list-access form modal
  /factobot anything-else → shows the help list with an "unknown command" message

Subcommands and their fields are defined entirely in commands.yaml. Adding a new
subcommand requires only editing that file — no changes to this module are needed.

HOW MODAL SUBMISSION ROUTING WORKS
------------------------------------
Because there is now one handler for many different modals, we can't use a fixed
callback_id string. Instead, modals.py sets the callback_id to:

    "{bot_name}_modal:{command_name}"     e.g. "factobot_modal:onboard"

where bot_name is the value of BOT_NAME (from settings.yaml). Bolt's @app.view()
decorator accepts a compiled regex, so we match any callback_id that starts with
"{BOT_NAME}_modal:" and extract the command name from it inside the handler.
This keeps a single handler for all modal submissions.

TIMING CONSTRAINTS
------------------
Slack requires every incoming event to be acknowledged within 3 seconds by
calling ack(). The trigger_id used to open a modal also expires after 3 seconds.
Because of this:

  - ack() is ALWAYS the very first call in every handler
  - views_open() follows immediately after ack() — no slow work in between
  - All API calls and webhook triggers happen AFTER ack() and views_open()

USAGE
-----
    from slack_bolt import App
    from app import handlers

    app = App(token=SLACK_BOT_TOKEN)
    handlers.register(app)
"""

import logging
import re

from slack_bolt import App

from app import ai_client, skill_runner, webhook_client
from app.access_control import check_access, list_accessible_commands
from app.command_loader import COMMANDS, get_command, extract_field_values, validate_field_values
from app.settings_loader import BOT_NAME
from app.modals import build_modal, build_confirmation_blocks, build_help_blocks
logger = logging.getLogger(__name__)

# Matches any modal callback_id set by modals.build_modal().
# Format: "{bot_name}_modal:{command_name}", e.g. "factobot_modal:onboard"
# Built from BOT_NAME so it stays correct if the name is changed in config.
MODAL_CALLBACK_PATTERN = re.compile(rf"^{re.escape(BOT_NAME)}_modal:.+$")


def register(app: App) -> None:
    """
    Attach all Slack event, command, action, and view handlers to the given App.

    Called once from main.py during startup. All handlers are defined as
    closures inside this function so they share access to `app` without globals.

    Args:
        app: The Slack Bolt App instance to register handlers onto.
    """

    # ------------------------------------------------------------------
    # Core message processing — shared by DM and @mention handlers
    # ------------------------------------------------------------------

    def process_message(user_id: str, text: str, say, client) -> None:
        """
        The central message processing pipeline for conversational interactions.

        Called by both the @mention handler and the direct message handler.
        Sends the user's message to the AI model (with conversation history),
        checks whether the model wants to trigger a workflow, and posts the reply.

        The "Thinking..." placeholder pattern is used because Slack has no
        native typing indicator for Socket Mode bots. Without it, users see
        silence for several seconds while the model generates a response.

        If anything goes wrong after the placeholder is posted, the placeholder
        is updated with an error message so it never hangs unresolved.

        Args:
            user_id: Slack user ID of the sender.
            text:    The cleaned message text to send to the AI model.
            say:     Bolt's say() helper — posts to the same conversation.
            client:  Slack Web API client — used to update the placeholder.
        """

        # Post the "Thinking..." placeholder immediately.
        # say() returns metadata we need (channel, ts) to update the message later.
        placeholder = say("Thinking... 🤔")

        try:
            # Get the AI model's response, including full conversation history context
            ai_response = ai_client.get_response(user_id, text)

            # Check if the model included a <webhook_trigger> block in its response.
            # Returns a dict if found, None if this is just a conversational reply.
            make_payload = ai_client.extract_webhook_trigger(ai_response)

            # Strip the <webhook_trigger> block so users only see the readable text.
            display_text = ai_client.visible_text(ai_response)

            if make_payload:
                # The AI decided to trigger a workflow — fire the webhook.
                # webhook_client.trigger() never raises; it always returns a string.
                workflow_status = webhook_client.trigger(make_payload)

                # Combine the conversational reply with the workflow status.
                # If display_text is empty (model sent only a trigger block),
                # show just the status to avoid a blank leading line.
                if display_text:
                    final_message = f"{display_text}\n\n{workflow_status}"
                else:
                    final_message = workflow_status

            else:
                final_message = display_text

            # Replace the placeholder with the real response.
            # chat_update() requires the channel and ts from the original post.
            client.chat_update(
                channel=placeholder["channel"],
                ts=placeholder["ts"],
                text=final_message,
            )

        except Exception as error:
            logger.exception("Unexpected error processing message for user %s.", user_id)
            client.chat_update(
                channel=placeholder["channel"],
                ts=placeholder["ts"],
                text=f"❌ Something went wrong: {str(error)}",
            )

    # ------------------------------------------------------------------
    # @mention handler
    # ------------------------------------------------------------------

    @app.event("app_mention")
    def on_mention(event, say, client):
        """
        Handle @mentions of the bot in public or private channels.

        If the user typed only the @mention with no text, show a welcome
        message listing only the /factobot commands they are permitted to run.
        If they included a question, pass it through the AI pipeline.

        The mention text always starts with "<@BOT_USER_ID>" — we split on
        ">" to strip the prefix before sending the message to the AI model.
        """
        user_id = event["user"]

        # event["text"] format: "<@U0XXXXXXX> the user's actual message"
        # split(">", 1) splits on the first ">" only, [-1] takes what's after it.
        text_after_mention = event["text"].split(">", 1)[-1].strip()

        if not text_after_mention:
            # Bare @mention — show filtered help based on what this user can access
            accessible = list_accessible_commands(client, user_id, COMMANDS)
            accessible_commands = {name: COMMANDS[name] for name in accessible}
            say(blocks=_welcome_blocks(accessible_commands))
            return

        process_message(user_id, text_after_mention, say, client)

    # ------------------------------------------------------------------
    # Direct message handler
    # ------------------------------------------------------------------

    @app.event("message")
    def on_direct_message(event, say, client):
        """
        Handle direct messages sent to the bot.

        Filters out bot messages and message subtypes (edits, deletions,
        file shares, etc.) to prevent processing our own replies or
        triggering on non-user events, which would cause loops.

        Only plain text messages from human users are passed to the AI model.
        """
        is_bot_message   = bool(event.get("bot_id"))
        is_message_event = bool(event.get("subtype"))

        if is_bot_message or is_message_event:
            return

        user_id = event["user"]
        text    = event.get("text", "").strip()

        if not text:
            return

        process_message(user_id, text, say, client)

    # ------------------------------------------------------------------
    # /factobot slash command
    # ------------------------------------------------------------------

    @app.command(f"/{BOT_NAME}")
    def on_factobot_command(ack, body, client, respond):
        """
        Handle the /factobot slash command.

        Parses the first word of the command text as the subcommand name,
        checks the user's access, and opens the corresponding modal.

        If no subcommand is given, shows the help list filtered to only the
        commands the user is permitted to run. If the subcommand is unknown,
        shows the same filtered help with an "unknown command" note. If the
        user lacks access to a known command, responds with a denial message.

        TIMING: ack() and views_open() must both complete within 3 seconds.
        The trigger_id expires after 3 seconds — do not add slow operations
        between ack() and views_open().

        ACCESS CONTROL: The access check happens before views_open(). If the
        user is denied, respond() sends them an ephemeral message (visible
        only to them) and we return early without opening any modal.
        """

        # Acknowledge immediately — required within 3 seconds
        ack()

        user_id        = body["user_id"]
        raw_text       = (body.get("text") or "").strip()
        subcommand     = raw_text.split()[0].lower() if raw_text else ""

        if not subcommand:
            # No subcommand — show the commands this user can actually run
            accessible = list_accessible_commands(client, user_id, COMMANDS)
            accessible_commands = {name: COMMANDS[name] for name in accessible}
            respond(blocks=build_help_blocks(accessible_commands))
            return

        command = get_command(subcommand)

        if not command:
            # Unknown subcommand — show the filtered help with a note
            accessible = list_accessible_commands(client, user_id, COMMANDS)
            accessible_commands = {name: COMMANDS[name] for name in accessible}
            respond(
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⚠️ Unknown command: `{subcommand}`.",
                        },
                    },
                    *build_help_blocks(accessible_commands),
                ],
            )
            return

        # Known command — check access before opening the modal.
        # check_access() calls the Slack API to resolve group membership if needed.
        # respond() sends an ephemeral message (visible only to the requesting user).
        access = check_access(client, user_id, subcommand, command)

        if not access.allowed:
            logger.info(
                "Access denied: user %s attempted /%s %s. Reason: %s",
                user_id, BOT_NAME, subcommand, access.reason,
            )
            respond(
                text=access.denial_message,
                blocks=access.denial_blocks,
            )
            return

        # Access granted — open the modal.
        # views_open() must be called before the trigger_id expires (3 seconds).
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_modal(subcommand, command),
        )

    # ------------------------------------------------------------------
    # Modal submission handler — matches all factobot modals via regex
    # ------------------------------------------------------------------

    @app.view(MODAL_CALLBACK_PATTERN)
    def on_modal_submit(ack, body, client, view):
        """
        Handle submission of any /factobot modal form.

        Matched by MODAL_CALLBACK_PATTERN, which matches any callback_id
        starting with "{BOT_NAME}_modal:". The command name is extracted from
        the callback_id so we know which command was submitted and can look
        up its webhook URL and field definitions.

        Flow:
          1. Extract the command name from the modal's callback_id
          2. Look up the command config (fields, webhook URL)
          3. Extract submitted values from view["state"]["values"]
          4. Validate the values
          5. If errors: ack with errors, modal stays open
          6. If valid: ack to close, fire webhook, send confirmation DM

        FIELD VALUE EXTRACTION
        Slack returns submitted data at view["state"]["values"], structured as:
            { block_id: { action_id: { value_key: submitted_value } } }

        The value_key differs by input type — extract_field_values() abstracts
        over these differences and returns a clean flat dict.

        VALIDATION ERRORS
        ack(response_action="errors", errors={block_id: message}) keeps the
        modal open with inline error messages under the relevant fields.

        TIMING
        ack() is called first (within 3 seconds). The webhook call and
        DM happen after ack() so they don't block the acknowledgment.
        """

        # Extract the command name from the callback_id.
        # callback_id format: "{bot_name}_modal:{command_name}"
        # e.g. "factobot_modal:onboard" → command_name = "onboard"
        callback_id  = view["callback_id"]
        command_name = callback_id.split(":", 1)[1]

        command = get_command(command_name)

        if not command:
            # The command was valid when the modal opened but is no longer in
            # the registry — most likely the YAML was edited and the bot was
            # redeployed mid-session. Close the modal and notify the user.
            ack()
            submitter_user_id = body["user"]["id"]
            client.chat_postMessage(
                channel=submitter_user_id,
                text=(
                    f"❌ The `{command_name}` command is no longer available.\n"
                    f"Run `/factobot` to see current commands."
                ),
            )
            return

        # Extract submitted values from Slack's nested state structure.
        # extract_field_values() handles the different paths for each field type.
        form_state = view["state"]["values"]
        values     = extract_field_values(command, form_state)

        # Validate — command_loader handles required/optional and type-specific rules
        errors = validate_field_values(command, values)

        if errors:
            # Return errors to Slack. The modal stays open; error messages
            # appear beneath each invalid field. Do not proceed further.
            ack(response_action="errors", errors=errors)
            return

        # No errors — close the modal
        ack()

        submitter_user_id = body["user"]["id"]

        # Dispatch to webhook or skill depending on the command's action_type.
        action_type = command.get("action_type", "webhook")

        if action_type == "skill":
            # Run the skill via the AI model — no HTTP call, no callback token.
            workflow_status = skill_runner.run(
                skill_name=command["skill_name"],
                command_name=command_name,
                values=values,
                user_id=submitter_user_id,
            )

        else:
            # Default: fire a webhook (original behaviour).
            # Build the webhook payload. Include the command name and submitter so
            # the receiver can route correctly and maintain an audit trail.
            make_payload = {
                "command":      command_name,
                "requested_by": submitter_user_id,
                **values,
            }

            # Register a pending job and obtain a one-time callback token.
            # If CALLBACK_BASE_URL is not configured, the token is still created
            # but webhook_client will omit the callback block from the payload —
            # the bot runs in fire-and-forget mode transparently.
            from app.job_store import create_job
            callback_token = create_job(
                command_name=command_name,
                user_id=submitter_user_id,
                channel_id=submitter_user_id,   # DM the submitter with the result
                notify_channels=command.get("notify_channels", []),
            )

            # Trigger the webhook for this command.
            # We pass the webhook_url explicitly so webhook_client can use it
            # instead of the global FALLBACK_WEBHOOK_URL from config.
            workflow_status = webhook_client.trigger(
                payload=make_payload,
                webhook_url=command["webhook_url"],
                callback_token=callback_token,
            )

        logger.info(
            "/%s submitted by %s. Values: %s. Status: %s",
            command_name, submitter_user_id, values, workflow_status,
        )

        # DM the submitter with a structured confirmation.
        # Using the user_id as the channel opens a direct message.
        client.chat_postMessage(
            channel=submitter_user_id,
            blocks=build_confirmation_blocks(
                command_name=command_name,
                command=command,
                values=values,
                workflow_status=workflow_status,
            ),
        )

    # ------------------------------------------------------------------
    # /reset-chat slash command
    # ------------------------------------------------------------------

    @app.command("/reset-chat")
    def on_reset_chat(ack, respond, command):
        """
        Handle /reset-chat by clearing the user's AI conversation history.

        Deletes the calling user's stored message history so their next
        conversation with the bot starts completely fresh. Useful when the
        conversation has gone off-track or the user wants a clean slate.

        respond() sends an ephemeral message — visible only to the user
        who ran the command, not to the whole channel.
        """
        ack()

        user_id     = command["user_id"]
        had_history = ai_client.clear_history(user_id)

        if had_history:
            respond("✅ Conversation history cleared. Starting fresh!")
        else:
            respond("Nothing to clear — we haven't talked yet.")


# ---------------------------------------------------------------------------
# Private block helpers
# ---------------------------------------------------------------------------

def _welcome_blocks(accessible_commands: dict) -> list:
    """
    Build the Block Kit blocks for the welcome message shown on a bare @mention.

    Only lists commands the user is actually permitted to run, so each person
    sees a personalised view of the bot's capabilities rather than a full list
    that includes commands they'll be denied if they try to run them.

    Args:
        accessible_commands: A filtered subset of COMMANDS containing only
                             the commands the user has access to.

    Returns:
        A list of Block Kit block dicts.
    """
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Hi! I'm {BOT_NAME.title()}. I can answer IT questions and help you "
                    f"trigger workflows.\n\n"
                    f"Use `/{BOT_NAME} <command>` to open a form, or just ask me "
                    f"a question directly."
                ),
            },
        },
        *build_help_blocks(accessible_commands),
    ]
