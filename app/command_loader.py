"""
command_loader.py
-----------------
Reads commands.yaml at startup and exposes the command registry to the rest
of the application.

WHAT THIS MODULE DOES
---------------------
The bot's workflows are defined entirely in commands.yaml — this module is
the bridge between that file and the Python code. It:

  1. Reads and parses commands.yaml from disk
  2. Validates the structure (required keys, known field types, etc.)
  3. Exposes the command registry (COMMANDS) and helper functions

Everything is loaded once at import time and cached in module-level variables.
If the YAML is missing or invalid, the app raises at startup rather than
failing silently on the first user interaction.

SEPARATION OF CONCERNS
-----------------------
This module owns commands.yaml only — workflow definitions, webhook URLs,
access control rules, and field schemas. Global bot settings (name, icons)
are owned by settings_loader.py, which reads settings.yaml.

COMMAND REGISTRY STRUCTURE
---------------------------
After loading, COMMANDS is a dict with this shape:

    {
        "onboard": {
            "description":     "Onboard a new hire and trigger provisioning",
            "action_type":     "webhook",                          # default
            "webhook_url":     "https://your-webhook-receiver.com/onboard",
            "allowed":         ["hr-managers", "it-admins"],
            "notify_channels": ["C012ABC456"],                     # optional
            "fields": [ ... ],
        },
        "provision-hardware": {
            "description":     "Provision hardware via an AI skill",
            "action_type":     "skill",
            "skill_name":      "it-onboarding-provisioner",
            "allowed":         ["it-admins"],
            "notify_channels": ["C012ABC456", "C034DEF789"],       # optional
            "fields":  [ ... ],
        },
    }

ACTION TYPES
------------
  webhook  — (default) POSTs form values to webhook_url. Requires webhook_url.
  skill    — Runs a SKILL.md-based AI skill. Requires skill_name.

NOTIFY CHANNELS
---------------
  notify_channels  — optional list of Slack channel IDs to post the workflow
                     completion result to, in addition to the DM sent to the
                     submitter. Each entry must be a Slack channel ID (starts
                     with C, e.g. C012ABC456). Channel names are not accepted
                     because they are not stable — channels can be renamed.
                     The bot must be a member of each listed channel.
                     Only applies to webhook commands (skill commands complete
                     synchronously and notify inline).

SUPPORTED FIELD TYPES
---------------------
  text        — plain_text_input in Block Kit
  date        — datepicker in Block Kit
  select      — static_select in Block Kit (requires "options")
  multiselect — multi_static_select in Block Kit (requires "options")

USAGE
-----
    from app.command_loader import COMMANDS, get_command, list_commands

    command = get_command("onboard")   # Returns dict or None
    names   = list_commands()          # Returns ["list-access", "offboard", ...]
"""

import logging
import pathlib

import yaml

logger = logging.getLogger(__name__)

# Supported field types — used for validation at load time.
# If a field in the YAML has a type not in this set, loading fails loudly.
SUPPORTED_FIELD_TYPES = {"text", "date", "select", "multiselect"}

# Supported action types for commands.
# "webhook" (default) fires an HTTP POST; "skill" runs a SKILL.md-based AI skill.
SUPPORTED_ACTION_TYPES = {"webhook", "skill"}

# Field types that require an "options" list to be present and non-empty.
FIELD_TYPES_REQUIRING_OPTIONS = {"select", "multiselect"}

# Path to the YAML file, resolved relative to this file's location.
# This means the path works correctly regardless of which directory
# the bot is launched from.
_YAML_PATH = pathlib.Path(__file__).parent.parent / "commands.yaml"


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def _load_commands() -> dict:
    """
    Read commands.yaml from disk, validate its structure, and return the
    parsed command registry dict.

    Validation checks performed:
      - File exists and is valid YAML
      - Top-level "commands" key is present and is a non-empty dict
      - Each command has "description", "allowed", and "fields"
      - webhook commands additionally require "webhook_url"
      - skill commands additionally require "skill_name"
      - Each command's "allowed" list is present, non-empty, and well-formed
      - Each field has "id", "label", and "type"
      - Each field's "type" is one of the supported values
      - Fields of type "select" or "multiselect" have a non-empty "options" list
      - Each option has "label" and "value"

    Raises:
        FileNotFoundError: If commands.yaml does not exist at the expected path.
        ValueError:        If the YAML structure fails any validation check.
        yaml.YAMLError:    If the file contains invalid YAML syntax.

    Returns:
        The validated commands dict, ready to use as the module-level registry.
    """

    if not _YAML_PATH.exists():
        raise FileNotFoundError(
            f"commands.yaml not found at {_YAML_PATH}.\n"
            f"Create it in the project root alongside main.py."
        )

    logger.info("Loading commands from %s", _YAML_PATH)

    with open(_YAML_PATH, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)

    if not isinstance(raw, dict) or "commands" not in raw:
        raise ValueError("commands.yaml must have a top-level 'commands' key.")

    commands = raw["commands"]

    if not isinstance(commands, dict) or not commands:
        raise ValueError("commands.yaml 'commands' must be a non-empty mapping.")

    for command_name, command_config in commands.items():
        _validate_command(command_name, command_config)

    logger.info(
        "Loaded %d command(s): %s",
        len(commands),
        ", ".join(commands.keys()),
    )

    return commands


def _validate_command(name: str, config: dict) -> None:
    """
    Validate a single command's configuration dict.

    Checks that all required keys are present and that each field definition
    is well-formed. Raises ValueError with a descriptive message if anything
    is wrong, including the command name so the developer knows exactly where
    to look in the YAML.

    Args:
        name:   The command name (e.g. "onboard"). Used in error messages.
        config: The command's configuration dict from the parsed YAML.

    Raises:
        ValueError: If any part of the command config is invalid.
    """

    # Determine action type — defaults to "webhook" for backward compatibility
    action_type = config.get("action_type", "webhook")

    if action_type not in SUPPORTED_ACTION_TYPES:
        raise ValueError(
            f"Command '{name}': unsupported action_type '{action_type}'.\n"
            f"Supported types: {sorted(SUPPORTED_ACTION_TYPES)}"
        )

    # Required keys differ by action type
    if action_type == "webhook":
        required_keys = {"description", "webhook_url", "fields", "allowed"}
        missing_hint  = "description, webhook_url, allowed, and fields"
    else:  # skill
        required_keys = {"description", "skill_name", "fields", "allowed"}
        missing_hint  = "description, skill_name, allowed, and fields"

    missing_keys = required_keys - set(config.keys())

    if missing_keys:
        raise ValueError(
            f"Command '{name}' is missing required keys: {missing_keys}.\n"
            f"Each {action_type} command needs: {missing_hint}."
        )

    if not isinstance(config["fields"], list) or not config["fields"]:
        raise ValueError(
            f"Command '{name}': 'fields' must be a non-empty list."
        )

    # Validate the allowed list
    _validate_allowed(name, config["allowed"])

    # Validate optional notify_channels list
    if "notify_channels" in config:
        _validate_notify_channels(name, config["notify_channels"])

    # Validate each field definition
    for field in config["fields"]:
        _validate_field(name, field)


def _validate_notify_channels(command_name: str, notify_channels: list) -> None:
    """
    Validate the optional notify_channels list for a command.

    Each entry must be either:
      - A Slack channel ID: starts with 'C' followed by alphanumeric characters
        (e.g. C012ABC456). Works for both public and private channels.
      - A public channel name with a '#' prefix (e.g. #it-notifications).
        Slack resolves the name to an ID at message-send time. Only works for
        public channels — use an ID for private channels.

    Args:
        command_name:    The parent command name. Used in error messages.
        notify_channels: The value of the command's "notify_channels" key.

    Raises:
        ValueError: If notify_channels is not a list, or contains invalid entries.
    """
    if not isinstance(notify_channels, list):
        raise ValueError(
            f"Command '{command_name}': 'notify_channels' must be a list.\n"
            f"Example:\n"
            f"  notify_channels:\n"
            f"    - '#it-notifications'\n"
            f"    - C012ABC456"
        )

    for entry in notify_channels:
        entry_str = str(entry).strip()

        if not entry_str:
            raise ValueError(
                f"Command '{command_name}': 'notify_channels' contains a blank entry. "
                f"Remove it or replace it with a channel name (e.g. #it-notifications) "
                f"or a channel ID (e.g. C012ABC456)."
            )

        is_channel_id   = entry_str.startswith("C") and entry_str[1:].isalnum()
        is_channel_name = entry_str.startswith("#") and len(entry_str) > 1

        if not is_channel_id and not is_channel_name:
            raise ValueError(
                f"Command '{command_name}': notify_channels entry {entry_str!r} is not "
                f"a valid channel reference.\n"
                f"Use a channel name prefixed with '#' (e.g. #it-notifications) for "
                f"public channels, or a channel ID starting with 'C' "
                f"(e.g. C012ABC456) for public or private channels."
            )



def _validate_allowed(command_name: str, allowed: list) -> None:
    """
    Validate the "allowed" access control list for a command.

    Checks that the list is non-empty and that each entry is either the
    special keyword "all", a plausible Slack user ID, or a non-empty string
    that could be a group handle. Does not verify that the groups or user IDs
    actually exist in Slack — that happens at runtime when access is checked.

    Args:
        command_name: The parent command name. Used in error messages.
        allowed:      The value of the command's "allowed" key from the YAML.

    Raises:
        ValueError: If the allowed list is missing, not a list, empty,
                    or contains invalid entries.
    """

    if not isinstance(allowed, list):
        raise ValueError(
            f"Command '{command_name}': 'allowed' must be a list.\n"
            f"Example:\n"
            f"  allowed:\n"
            f"    - all\n"
            f"  or:\n"
            f"    - hr-managers\n"
            f"    - it-admins"
        )

    if not allowed:
        raise ValueError(
            f"Command '{command_name}': 'allowed' must not be empty.\n"
            f"Use 'all' to allow all workspace members, or list specific groups/user IDs."
        )

    for entry in allowed:
        entry_str = str(entry).strip()

        if not entry_str:
            raise ValueError(
                f"Command '{command_name}': 'allowed' contains a blank entry. "
                f"Remove it or replace it with a valid group handle, user ID, or 'all'."
            )


def _validate_field(command_name: str, field: dict) -> None:
    """
    Validate a single field definition within a command.

    Args:
        command_name: The parent command name. Used in error messages.
        field:        The field dict from the parsed YAML.

    Raises:
        ValueError: If the field is missing required keys, has an unsupported
                    type, or is a select/multiselect without valid options.
    """

    # Every field must have at least these three keys
    required_keys = {"id", "label", "type"}
    missing_keys  = required_keys - set(field.keys())

    if missing_keys:
        raise ValueError(
            f"Command '{command_name}': a field is missing keys {missing_keys}.\n"
            f"Field definition: {field}"
        )

    field_id   = field["id"]
    field_type = field["type"]

    # Check that the type is one we know how to render
    if field_type not in SUPPORTED_FIELD_TYPES:
        raise ValueError(
            f"Command '{command_name}', field '{field_id}': "
            f"unsupported type '{field_type}'.\n"
            f"Supported types: {sorted(SUPPORTED_FIELD_TYPES)}"
        )

    # Select and multiselect fields must have a non-empty options list
    if field_type in FIELD_TYPES_REQUIRING_OPTIONS:
        options = field.get("options", [])

        if not isinstance(options, list) or not options:
            raise ValueError(
                f"Command '{command_name}', field '{field_id}': "
                f"type '{field_type}' requires a non-empty 'options' list."
            )

        # Each option must have both a label and a value
        for option in options:
            if "label" not in option or "value" not in option:
                raise ValueError(
                    f"Command '{command_name}', field '{field_id}': "
                    f"each option must have 'label' and 'value'. Got: {option}"
                )


# ---------------------------------------------------------------------------
# Module-level registry — loaded once at import time
# ---------------------------------------------------------------------------

# The full command registry. Import directly for read access,
# or use get_command() for safe None-guarded lookups.
COMMANDS: dict = _load_commands()


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------

def get_command(name: str) -> dict | None:
    """
    Look up a command by name and return its configuration dict.

    Args:
        name: The subcommand name as typed by the user, e.g. "onboard".
              Case-sensitive — YAML keys are lowercase by convention.

    Returns:
        The command configuration dict, or None if the name is not found.
    """
    return COMMANDS.get(name)


def list_commands() -> list[str]:
    """
    Return a sorted list of all registered command names.

    Sorted alphabetically so the help message always displays in a
    consistent order regardless of how commands are ordered in the YAML.

    Returns:
        A sorted list of command name strings, e.g. ["list-access", "offboard", ...]
    """
    return sorted(COMMANDS.keys())


def extract_field_values(command: dict, form_state: dict) -> dict:
    """
    Extract submitted form values from Slack's view state for a given command.

    Slack returns submitted modal data in a deeply nested structure:
        view["state"]["values"][block_id][action_id][value_key]

    The value_key differs by field type:
      - text        → "value"           (returns a string)
      - date        → "selected_date"   (returns "YYYY-MM-DD" string)
      - select      → "selected_option" → "value" (returns the option's value string)
      - multiselect → "selected_options" (returns a list of option dicts)

    This function abstracts over those differences so handlers don't need
    to know the Block Kit internals — they just call this and get a flat dict.

    For multiselect fields, the returned value is a list of selected value
    strings (e.g. ["okta", "github"]), not the raw list of option dicts.

    Args:
        command:    The command config dict (from get_command() or COMMANDS).
        form_state: The view["state"]["values"] dict from the Slack submission event.

    Returns:
        A flat dict mapping field_id → submitted value, e.g.:
        {
            "name":          "Alex Johnson",
            "start_date":    "2025-06-15",
            "employee_type": "fte",
            "systems":       ["okta", "github"],
        }
    """
    extracted = {}

    for field in command["fields"]:
        field_id   = field["id"]
        field_type = field["type"]

        # Block Kit uses "block_{id}" as block_id and "input_{id}" as action_id.
        # These are set by modals.py when building the modal — they must match here.
        block_id  = f"block_{field_id}"
        action_id = f"input_{field_id}"

        # Get the element state for this field.
        # If the field wasn't submitted (e.g. optional field left blank),
        # element_state may be None or missing the value key.
        field_state    = form_state.get(block_id, {})
        element_state  = field_state.get(action_id, {})

        if field_type == "text":
            # Plain text — value is a string or None if left blank
            raw_value       = element_state.get("value") or ""
            extracted[field_id] = raw_value.strip()

        elif field_type == "date":
            # Date picker — value is "YYYY-MM-DD" or None if not selected
            extracted[field_id] = element_state.get("selected_date")

        elif field_type == "select":
            # Single select — selected_option is a dict with "value" and "text"
            selected_option     = element_state.get("selected_option") or {}
            extracted[field_id] = selected_option.get("value")

        elif field_type == "multiselect":
            # Multi-select — selected_options is a list of option dicts.
            # We extract just the "value" string from each dict.
            selected_options    = element_state.get("selected_options") or []
            extracted[field_id] = [opt["value"] for opt in selected_options]

    return extracted


def validate_field_values(command: dict, values: dict) -> dict:
    """
    Validate extracted field values against the command's field definitions.

    Slack enforces that required fields are not completely empty, but does
    not enforce minimum lengths or other custom rules. This function adds
    those checks and returns a dict of errors suitable for passing to
    ack(response_action="errors", errors=errors).

    Currently enforced rules:
      - Required text fields must be at least 2 characters after stripping whitespace
      - Required select fields must have a non-None value
      - Required multiselect fields must have at least one selection
      - Required date fields must have a non-None value

    Args:
        command: The command config dict.
        values:  The flat values dict returned by extract_field_values().

    Returns:
        A dict mapping block_id → error message string for any invalid fields.
        Returns an empty dict if all fields are valid.
        The block_id format ("block_{field_id}") matches what Slack expects
        for inline validation errors.
    """
    errors = {}

    for field in command["fields"]:
        field_id   = field["id"]
        field_type = field["type"]
        is_required = field.get("required", False)
        block_id   = f"block_{field_id}"
        value      = values.get(field_id)

        if not is_required:
            # Optional fields — skip validation entirely
            continue

        if field_type == "text":
            if not value or len(value) < 2:
                errors[block_id] = f"{field['label']} must be at least 2 characters."

        elif field_type == "date":
            if not value:
                errors[block_id] = f"Please select a {field['label']}."

        elif field_type == "select":
            if not value:
                errors[block_id] = f"Please select a {field['label']}."

        elif field_type == "multiselect":
            if not value:
                errors[block_id] = f"Please select at least one {field['label']}."

    return errors
