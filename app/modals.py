"""
modals.py
---------
Builds Slack Block Kit modal payloads dynamically from command configurations
loaded out of commands.yaml.

HOW DYNAMIC MODAL BUILDING WORKS
---------------------------------
Previously, the modal was hardcoded Python — adding a field meant editing this
file. Now, modals are assembled at runtime from the command's "fields" list.

Each field definition in commands.yaml maps to a Block Kit input block:

  YAML type     →  Block Kit element
  ----------       ----------------
  text          →  plain_text_input
  date          →  datepicker
  select        →  static_select
  multiselect   →  multi_static_select

The field's "id" is used to construct two Block Kit identifiers:
  block_id  = "block_{id}"   e.g. "block_name"
  action_id = "input_{id}"   e.g. "input_name"

These identifiers are how Slack returns submitted values back to us, and
how command_loader.extract_field_values() knows where to find them.

PUBLIC FUNCTIONS
----------------
  build_modal(command_name, command)
      Returns the full Block Kit modal payload for a given command.
      Pass the result directly to client.views_open().

  build_confirmation_blocks(command_name, command, values, workflow_status)
      Returns Block Kit blocks for the DM sent after a successful submission.
      Pass the result to client.chat_postMessage().

  build_help_blocks(commands)
      Returns Block Kit blocks listing all available /factobot subcommands.
      Shown when the user runs /factobot with no subcommand, or an unknown one.

BLOCK KIT REFERENCE
-------------------
  Visual editor:  https://app.slack.com/block-kit-builder
  API reference:  https://api.slack.com/reference/block-kit
"""

import logging

from app.settings_loader import BOT_NAME, ICONS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def build_modal(command_name: str, command: dict) -> dict:
    """
    Build a complete Block Kit modal payload for the given command.

    The modal title and intro text are derived from the command's name and
    description. The form fields are built dynamically from the command's
    "fields" list.

    The modal's callback_id encodes the command name so the submission handler
    knows which command was submitted and can look up the right webhook URL.
    Format: "{bot_name}_modal:{command_name}", e.g. "factobot_modal:onboard".
    The prefix is derived from BOT_NAME so it stays consistent if the name changes.

    The INFO icon is shown as an image accessory in the intro section if
    configured under icons.info in settings.yaml.
    """

    # Build all field blocks from the command's field definitions
    field_blocks = [_build_field_block(field) for field in command["fields"]]

    return {
        "type": "modal",

        # The callback_id encodes the bot name and command name.
        # Format: "{bot_name}_modal:{command_name}", e.g. "factobot_modal:onboard"
        # Using BOT_NAME ensures the pattern stays consistent if the name changes.
        # handlers.py builds the regex pattern from BOT_NAME to match this format.
        "callback_id": f"{BOT_NAME}_modal:{command_name}",

        "title":  {"type": "plain_text", "text": _modal_title(command_name)},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close":  {"type": "plain_text", "text": "Cancel"},

        "blocks": [
            _build_intro_section(command["description"]),
            {"type": "divider"},
            *field_blocks,
        ],
    }


def build_callback_result_blocks(
    command_name: str,
    status: str,
    message: str,
    result_url: str | None = None,
    fields: dict | None = None,
) -> list:
    """
    Build Block Kit blocks for the callback result DM.

    Sent to the submitter when the workflow receiver calls back with its
    completion status. Visually distinct from the initial confirmation DM:
    green checkmark for success, red X for failure.

    If result_url is provided, a "View Result" button is appended as an
    actions block so the recipient can jump directly to the created or
    affected resource (e.g. a Jira ticket, Okta user profile, GitHub PR).

    If fields is provided, a structured key/value summary block is inserted
    between the header and the button, showing what was done in a scannable
    two-column layout (e.g. {"Okta group": "engineering", "GitHub org": "acme"}).
    Block Kit supports up to 10 fields per section; extras are silently truncated.

    Args:
        command_name: The slash command that was run (e.g. "onboard").
        status:       "success" or "failure" as reported by the receiver.
        message:      Human-readable result message from the receiver.
        result_url:   Optional URL to the resource created or affected by
                      the workflow. Rendered as a "View Result" button.
                      Must be a valid HTTPS URL; ignored if empty or None.
        fields:       Optional dict of label->value pairs summarising the
                      workflow outcome (e.g. {"Okta user": "ajohnson",
                      "Hardware tier": "developer"}). Rendered as a
                      two-column Block Kit fields section. Non-string values
                      are coerced to strings. Maximum 10 pairs.

    Returns:
        A list of Block Kit block dicts for use in chat_postMessage().
    """
    is_success = status == "success"
    icon       = "\u2705" if is_success else "\u274c"
    label      = "completed" if is_success else "failed"

    section: dict = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"{icon} */{command_name} {label}*\n{message}",
        },
    }

    # Attach ACK icon on success, ERROR icon on failure
    icon_url = ICONS["ack"] if is_success else ICONS["error"]
    if icon_url:
        section["accessory"] = {
            "type":      "image",
            "image_url": icon_url,
            "alt_text":  label.title(),
        }

    blocks = [section]

    # Append a structured key/value summary when the receiver supplies fields.
    # Block Kit's section fields property renders up to 10 items in two columns.
    if fields and isinstance(fields, dict):
        field_items = list(fields.items())[:10]  # Block Kit hard limit of 10
        blocks.append({
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*{str(k)}*\n{str(v)}",
                }
                for k, v in field_items
            ],
        })

    # Append a "View Result" button when the receiver supplies a result URL.
    # Only rendered for success — a failure URL would point to something broken
    # or incomplete, which is more confusing than helpful.
    if result_url and is_success:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "View Result", "emoji": False},
                    "url":       result_url,
                    "style":     "primary",
                    "action_id": f"view_result_{command_name}",
                }
            ],
        })

    return blocks


def build_confirmation_blocks(
    command_name: str,
    command: dict,
    values: dict,
    workflow_status: str,
) -> list:
    """
    Build Block Kit blocks for the confirmation DM sent after form submission.

    The confirmation summarises what was submitted, formatted as a Slack
    blockquote-style section, followed by the webhook trigger status in a
    smaller context block.

    Each submitted value is listed using the field's label (from the command
    config) as the key, so the output is always human-readable regardless of
    what the internal field ID is.

    Multiselect values (lists) are joined with ", " for display.

    Args:
        command_name:    The subcommand name, e.g. "onboard".
        command:         The command config dict.
        values:          The flat dict of submitted values from extract_field_values().
        workflow_status: The status string returned by make_client.trigger().

    Returns:
        A list of Block Kit block dicts for use in chat_postMessage().
    """

    # Build the summary lines — one per field that has a non-empty value.
    # We iterate the command's field definitions (not the values dict) so the
    # order matches the form order, not whatever order the dict happens to be in.
    summary_lines = []

    for field in command["fields"]:
        field_id    = field["id"]
        field_label = field["label"]
        value       = values.get(field_id)

        # Skip fields that were left blank (optional fields)
        if not value and value != 0:
            continue

        # Format lists (multiselect) as a comma-separated string for display
        if isinstance(value, list):
            display_value = ", ".join(value)
        else:
            display_value = str(value)

        summary_lines.append(f"> *{field_label}:* {display_value}")

    summary_text = "\n".join(summary_lines)
    title        = _modal_title(command_name)

    # Build the main section block
    section_block: dict = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"✅ *{title} submitted*\n{summary_text}",
        },
    }

    # Attach the ACK icon as an accessory if configured
    if ICONS["ack"]:
        section_block["accessory"] = {
            "type":      "image",
            "image_url": ICONS["ack"],
            "alt_text":  "Confirmed",
        }

    return [
        section_block,
        {
            # Context blocks render smaller — good for status/metadata
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": workflow_status}
            ],
        },
    ]


def build_help_blocks(commands: dict) -> list:
    """
    Build Block Kit blocks listing all available /factobot subcommands.

    Shown in two situations:
      1. The user runs /factobot with no subcommand
      2. The user runs /factobot with an unrecognised subcommand

    Each command is listed with its name (formatted as inline code) and
    its description. Commands are shown in alphabetical order.

    Args:
        commands: The full COMMANDS dict from command_loader, mapping
                  command name → command config.

    Returns:
        A list of Block Kit block dicts.
    """

    # Build one line per command: `/factobot onboard` — description
    command_lines = []

    for name in sorted(commands.keys()):
        description = commands[name].get("description", "")
        command_lines.append(f"• `/{BOT_NAME} {name}` — {description}")

    commands_text = "\n".join(command_lines)

    # Build the main section, optionally with the INFO icon as an accessory
    help_section: dict = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Available commands:*\n" + commands_text,
        },
    }

    if ICONS["info"]:
        help_section["accessory"] = {
            "type":      "image",
            "image_url": ICONS["info"],
            "alt_text":  BOT_NAME.title(),
        }

    return [
        help_section,
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Run `/{BOT_NAME} <command>` to open the form for that action.",
                }
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Private helpers — modal structure builders
# ---------------------------------------------------------------------------

def _modal_title(command_name: str) -> str:
    """
    Convert a command name to a human-readable modal title.

    Replaces hyphens with spaces and title-cases the result so that
    "update-access" becomes "Update Access", "list-access" becomes
    "List Access", etc.

    Args:
        command_name: The raw command name from commands.yaml.

    Returns:
        A capitalised, hyphen-free title string.
    """
    return command_name.replace("-", " ").title()


def _build_intro_section(description: str) -> dict:
    """
    Build the intro section block shown at the top of every modal.

    Displays the command's description as intro text. If icons.info is
    configured in settings.yaml, the bot icon is attached as an image
    accessory and Slack renders it to the right of the text. If not
    configured, the section renders without the image — fully functional
    either way.

    Args:
        description: The command's description string from commands.yaml.

    Returns:
        A Block Kit section block dict.
    """
    section: dict = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*{description}*\nFill in the fields below and click Submit.",
        },
    }

    if ICONS["info"]:
        section["accessory"] = {
            "type":      "image",
            "image_url": ICONS["info"],
            "alt_text":  BOT_NAME.title(),
        }

    return section


def _build_field_block(field: dict) -> dict:
    """
    Build a single Block Kit input block from a field definition dict.

    Dispatches to the appropriate type-specific builder based on field["type"].
    The block_id and action_id follow the convention "block_{id}" and
    "input_{id}" so command_loader.extract_field_values() can find the
    submitted value without any extra coordination.

    Args:
        field: A field definition dict from commands.yaml, e.g.:
               {"id": "name", "label": "Full Name", "type": "text", ...}

    Returns:
        A Block Kit input block dict.
    """
    field_type = field["type"]

    if field_type == "text":
        return _build_text_block(field)

    elif field_type == "date":
        return _build_date_block(field)

    elif field_type == "select":
        return _build_select_block(field)

    elif field_type == "multiselect":
        return _build_multiselect_block(field)

    else:
        # This should never happen — command_loader validates types at startup.
        # If it does happen, log and return a disabled placeholder block.
        logger.error("Unknown field type '%s' for field '%s'.", field_type, field.get("id"))
        return _build_unknown_field_placeholder(field)


def _build_text_block(field: dict) -> dict:
    """
    Build a plain text input block for a field of type "text".

    Block Kit element: plain_text_input
    Value path after submission: state[block_id][action_id]["value"]

    Args:
        field: The field definition dict from commands.yaml.

    Returns:
        A Block Kit input block dict.
    """
    block: dict = {
        "type":     "input",
        "block_id": f"block_{field['id']}",
        "label":    {"type": "plain_text", "text": field["label"]},
        "element":  {
            "type":      "plain_text_input",
            "action_id": f"input_{field['id']}",
        },
    }

    # Add placeholder text if provided
    if field.get("placeholder"):
        block["element"]["placeholder"] = {
            "type": "plain_text",
            "text": field["placeholder"],
        }

    # optional=True tells Slack not to block submission if this field is empty.
    # Required fields have optional=False (the default), so Slack enforces them.
    block["optional"] = not field.get("required", False)

    return block


def _build_date_block(field: dict) -> dict:
    """
    Build a date picker input block for a field of type "date".

    Slack renders this as a native calendar picker in the modal.
    Value path after submission: state[block_id][action_id]["selected_date"]
    The returned value is always a "YYYY-MM-DD" string.

    Args:
        field: The field definition dict from commands.yaml.

    Returns:
        A Block Kit input block dict.
    """
    return {
        "type":     "input",
        "block_id": f"block_{field['id']}",
        "optional": not field.get("required", False),
        "label":    {"type": "plain_text", "text": field["label"]},
        "element":  {
            "type":        "datepicker",
            "action_id":   f"input_{field['id']}",
            "placeholder": {"type": "plain_text", "text": "Select a date"},
        },
    }


def _build_select_block(field: dict) -> dict:
    """
    Build a static select (single-choice dropdown) block for a field of type "select".

    Value path after submission:
        state[block_id][action_id]["selected_option"]["value"]

    Args:
        field: The field definition dict from commands.yaml.
               Must include a non-empty "options" list of {label, value} dicts.

    Returns:
        A Block Kit input block dict.
    """
    return {
        "type":     "input",
        "block_id": f"block_{field['id']}",
        "optional": not field.get("required", False),
        "label":    {"type": "plain_text", "text": field["label"]},
        "element":  {
            "type":        "static_select",
            "action_id":   f"input_{field['id']}",
            "placeholder": {"type": "plain_text", "text": f"Select {field['label'].lower()}"},
            "options":     _build_options(field["options"]),
        },
    }


def _build_multiselect_block(field: dict) -> dict:
    """
    Build a multi-select dropdown block for a field of type "multiselect".

    Allows the user to select multiple options. Value path after submission:
        state[block_id][action_id]["selected_options"]
    Returns a list of option dicts — use extract_field_values() to get
    just the value strings.

    Args:
        field: The field definition dict from commands.yaml.
               Must include a non-empty "options" list of {label, value} dicts.

    Returns:
        A Block Kit input block dict.
    """
    return {
        "type":     "input",
        "block_id": f"block_{field['id']}",
        "optional": not field.get("required", False),
        "label":    {"type": "plain_text", "text": field["label"]},
        "element":  {
            "type":        "multi_static_select",
            "action_id":   f"input_{field['id']}",
            "placeholder": {"type": "plain_text", "text": f"Select {field['label'].lower()}"},
            "options":     _build_options(field["options"]),
        },
    }


def _build_options(options: list) -> list:
    """
    Convert the YAML options list into the Block Kit options format.

    YAML format:    [{"label": "Full-Time Employee", "value": "fte"}, ...]
    Block Kit format: [{"text": {"type": "plain_text", "text": "..."}, "value": "..."}, ...]

    Args:
        options: The list of option dicts from commands.yaml.

    Returns:
        A list of Block Kit option dicts.
    """
    return [
        {
            "text":  {"type": "plain_text", "text": option["label"]},
            "value": option["value"],
        }
        for option in options
    ]


def _build_unknown_field_placeholder(field: dict) -> dict:
    """
    Build a disabled placeholder block for a field with an unknown type.

    This should never appear in production since command_loader validates
    field types at startup. It exists as a safe fallback so an unexpected
    type doesn't crash the modal builder mid-render.

    Args:
        field: The field definition dict with the unknown type.

    Returns:
        A Block Kit section block with an error message.
    """
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"⚠️ Unknown field type `{field.get('type')}` for field `{field.get('id')}`.",
        },
    }
