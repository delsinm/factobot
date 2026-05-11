"""
access_control.py
-----------------
Enforces per-command access control based on the allowlist defined in
commands.yaml.

HOW ACCESS CONTROL WORKS
--------------------------
Each command in commands.yaml declares an "allowed" list — a combination
of Slack user group handles and/or individual Slack user IDs that are
permitted to run it.

The special keyword "all" grants access to every member of the Slack
workspace the bot is installed on, with no further checks needed.

Example commands.yaml entries:

    # Anyone in the workspace can run this
    list-access:
      allowed:
        - all

    # Only members of these groups can run this
    offboard:
      allowed:
        - hr-managers
        - it-admins

    # A specific group plus one individual by Slack user ID
    onboard:
      allowed:
        - hr-managers
        - U012AB3CD

SLACK USER GROUPS vs USER IDs
-------------------------------
Slack user group handles look like:   hr-managers, it-admins
Slack user IDs look like:             U012AB3CD  (always starts with U, 9+ chars)

This module distinguishes between them automatically using _is_user_id().
User IDs are checked directly against the submitting user's ID.
Group handles are resolved to their member list via the Slack API.

SLACK API REQUIREMENTS
-----------------------
Checking group membership requires:
  - The usergroups:read scope on the bot token
  - The users.read scope (already needed for general bot operation)

These scopes must be added in the Slack app settings under
OAuth & Permissions → Bot Token Scopes.

CACHING
--------
Group membership lists are cached in memory for CACHE_TTL_SECONDS (default 5
minutes) to avoid hammering the Slack API on every command invocation. The
cache is per-group and is invalidated by age only — if group membership changes
in Slack, the bot will reflect it within one cache TTL window.

USAGE
-----
    from app.access_control import check_access, AccessResult

    result = check_access(
        client=slack_client,
        user_id="U012AB3CD",
        command_name="offboard",
        command=command_config,
    )

    if not result.allowed:
        respond(result.denial_message)
        return
"""

import logging
import time

from app.settings_loader import BOT_NAME, ICONS

logger = logging.getLogger(__name__)

# How long (in seconds) to cache group membership lists from the Slack API.
# At 300 seconds (5 minutes), a membership change in Slack takes at most
# 5 minutes to be reflected in the bot's access decisions.
CACHE_TTL_SECONDS = 300

# Cache structure: { group_handle: {"members": set[str], "fetched_at": float} }
# Populated lazily on first access check for each group.
_group_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class AccessResult:
    """
    The result of an access check.

    Attributes:
        allowed:        True if the user is permitted to run the command.
        denial_message: A plain-text fallback denial message. Used when
                        the caller can only post plain text.
        denial_blocks:  A list of Block Kit blocks for a richer denial message
                        that includes the ERROR icon if configured. Used when
                        the caller can post Block Kit content (respond() calls).
        reason:         A short internal description of why access was
                        granted or denied — used for logging.
    """

    def __init__(
        self,
        allowed: bool,
        reason: str,
        denial_message: str = None,
        denial_blocks: list = None,
    ):
        self.allowed        = allowed
        self.reason         = reason
        self.denial_message = denial_message
        self.denial_blocks  = denial_blocks


def check_access(client, user_id: str, command_name: str, command: dict) -> AccessResult:
    """
    Check whether a Slack user is permitted to run a given command.

    Reads the "allowed" list from the command config and checks the user
    against each entry. Returns an AccessResult immediately on the first
    match that grants access. If no entry grants access, returns a denial.

    The "allowed" list may contain:
      - "all"          — grants access to everyone in the workspace
      - A Slack user ID (e.g. "U012AB3CD") — grants access to that specific user
      - A Slack user group handle (e.g. "hr-managers") — grants access to all
        members of that group (resolved via the Slack API with caching)

    If the "allowed" key is missing from the command config, access is DENIED
    by default. This is a safe default — an unconfigured command is locked
    down rather than open, forcing an explicit decision in the YAML.

    Args:
        client:       The Slack Web API client (from the Bolt handler context).
                      Used to resolve group membership via the Slack API.
        user_id:      The Slack user ID of the person running the command.
        command_name: The subcommand name (e.g. "offboard"). Used in log messages
                      and the denial message shown to the user.
        command:      The command config dict from command_loader.COMMANDS.

    Returns:
        An AccessResult with allowed=True if access is granted, or
        allowed=False with a denial_message if access is denied.
    """

    allowed_entries = command.get("allowed", [])

    # No "allowed" key at all — deny by default with a clear message
    if not allowed_entries:
        logger.warning(
            "Command '%s' has no 'allowed' list — denying access to user %s.",
            command_name, user_id,
        )
        message = (
            f"⚠️ The `{command_name}` command has not been configured with an "
            f"access list. Please contact your IT administrator."
        )
        return AccessResult(
            allowed=False,
            reason="no_allowed_list_configured",
            denial_message=message,
            denial_blocks=_denial_blocks(message),
        )

    # Check each entry in the allowed list
    for entry in allowed_entries:
        entry = str(entry).strip()

        # "all" — open to the entire workspace, no further checks needed
        if entry.lower() == "all":
            logger.info(
                "Access granted to user %s for command '%s' (allowed: all).",
                user_id, command_name,
            )
            return AccessResult(allowed=True, reason="allowed_all")

        # Individual Slack user ID (e.g. "U012AB3CD")
        if _is_user_id(entry):
            if user_id == entry:
                logger.info(
                    "Access granted to user %s for command '%s' (direct user ID match).",
                    user_id, command_name,
                )
                return AccessResult(allowed=True, reason=f"user_id_match:{entry}")

        # Slack user group handle (e.g. "hr-managers")
        else:
            group_members = _get_group_members(client, entry)

            if group_members is None:
                # Group could not be resolved — log and skip this entry rather
                # than failing the whole check. Other entries may still grant access.
                logger.warning(
                    "Could not resolve group '%s' for command '%s' — skipping entry.",
                    entry, command_name,
                )
                continue

            if user_id in group_members:
                logger.info(
                    "Access granted to user %s for command '%s' (member of group '%s').",
                    user_id, command_name, entry,
                )
                return AccessResult(allowed=True, reason=f"group_match:{entry}")

    # No entry granted access — build a clear denial message
    logger.info(
        "Access denied to user %s for command '%s'. Allowed entries: %s",
        user_id, command_name, allowed_entries,
    )

    allowed_groups = [
        e for e in allowed_entries
        if not _is_user_id(str(e)) and str(e).lower() != "all"
    ]

    if allowed_groups:
        groups_display = ", ".join(f"`{g}`" for g in allowed_groups)
        detail = f"This command is restricted to: {groups_display}."
    else:
        detail = "You don't have permission to run this command."

    message = (
        f"🔒 Access denied for `/{BOT_NAME} {command_name}`.\n"
        f"{detail}\n"
        f"Contact your IT administrator if you need access."
    )

    return AccessResult(
        allowed=False,
        reason="no_matching_entry",
        denial_message=message,
        denial_blocks=_denial_blocks(message),
    )


def list_accessible_commands(client, user_id: str, commands: dict) -> list[str]:
    """
    Return a sorted list of command names the given user is allowed to run.

    Used by the help message to show each user only the commands they can
    actually use, rather than the full list. A user who can't run "offboard"
    shouldn't see it advertised to them.

    Args:
        client:   The Slack Web API client.
        user_id:  The Slack user ID to check access for.
        commands: The full COMMANDS dict from command_loader.

    Returns:
        A sorted list of command name strings the user is permitted to run.
    """
    accessible = []

    for command_name, command in commands.items():
        result = check_access(client, user_id, command_name, command)
        if result.allowed:
            accessible.append(command_name)

    return sorted(accessible)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_user_id(entry: str) -> bool:
    """
    Determine whether an allowlist entry is a Slack user ID or a group handle.

    Slack user IDs always begin with "U" and are at least 9 characters long
    (e.g. "U012AB3CD", "USLACKBOT"). Group handles are lowercase strings like
    "hr-managers" or "it-admins" and never start with "U" followed by digits.

    This heuristic is reliable in practice — Slack user IDs are uppercase and
    alphanumeric, while group handles are lowercase and may contain hyphens.

    Args:
        entry: A single entry from the command's "allowed" list.

    Returns:
        True if the entry looks like a Slack user ID, False otherwise.
    """
    return (
        len(entry) >= 9
        and entry[0].upper() == "U"
        and entry[1:].isalnum()
    )


def _get_group_members(client, group_handle: str) -> set[str] | None:
    """
    Return the set of Slack user IDs belonging to a user group.

    Results are cached in memory for CACHE_TTL_SECONDS to avoid making a
    Slack API call on every command invocation. The cache is checked first;
    if the cached entry is stale or missing, the Slack API is called and the
    result is stored.

    Requires the usergroups:read scope on the bot token.

    Args:
        client:       The Slack Web API client.
        group_handle: The user group handle as defined in commands.yaml,
                      e.g. "hr-managers". Must match the Slack group handle
                      exactly (case-insensitive comparison is used).

    Returns:
        A set of Slack user ID strings, or None if the group could not be
        found or the API call failed. None signals to the caller that this
        entry should be skipped rather than treated as a denial.
    """

    now = time.monotonic()

    # Return cached result if it's still fresh
    cached = _group_cache.get(group_handle)
    if cached and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        logger.debug(
            "Returning cached members for group '%s' (%d members).",
            group_handle, len(cached["members"]),
        )
        return cached["members"]

    # Cache miss or stale — fetch from Slack API
    logger.info("Fetching members for Slack group '%s' from API.", group_handle)

    try:
        # First, list all user groups to find the one matching our handle.
        # The Slack API doesn't support lookup by handle directly.
        groups_response = client.usergroups_list(include_users=False)
        all_groups      = groups_response.get("usergroups", [])

        # Find the group whose handle matches (case-insensitive)
        matched_group = None
        for group in all_groups:
            if group.get("handle", "").lower() == group_handle.lower():
                matched_group = group
                break

        if not matched_group:
            logger.warning(
                "Slack group '%s' not found. Available groups: %s",
                group_handle,
                [g.get("handle") for g in all_groups],
            )
            return None

        group_id = matched_group["id"]

        # Now fetch the member list for that group
        members_response = client.usergroups_users_list(usergroup=group_id)
        member_ids       = set(members_response.get("users", []))

        # Store in cache with the current timestamp
        _group_cache[group_handle] = {
            "members":    member_ids,
            "fetched_at": now,
        }

        logger.info(
            "Cached %d members for group '%s'.", len(member_ids), group_handle,
        )

        return member_ids

    except Exception as error:
        # Broad catch — Slack API errors, network errors, key errors, etc.
        # We return None so the caller skips this entry rather than crashing.
        logger.error(
            "Failed to fetch members for group '%s': %s", group_handle, error,
        )
        return None


def invalidate_cache(group_handle: str = None) -> None:
    """
    Manually invalidate the group membership cache.

    Useful in tests or if you want to force a fresh Slack API call without
    waiting for the TTL to expire. Pass a group handle to invalidate one
    group, or call with no arguments to clear the entire cache.

    Args:
        group_handle: The group handle to invalidate, or None to clear all.
    """
    if group_handle:
        _group_cache.pop(group_handle, None)
        logger.info("Invalidated cache for group '%s'.", group_handle)
    else:
        _group_cache.clear()
        logger.info("Invalidated entire group membership cache.")


def _denial_blocks(message: str) -> list:
    """
    Build Block Kit blocks for an access denial message.

    Returns a section block with the denial text. If ERROR_ICON_URL is
    configured, the error icon is attached as an image accessory displayed
    to the right of the text.

    These blocks are used by handlers.py when responding to denied commands
    via respond(), which supports Block Kit. They are stored on the
    AccessResult alongside the plain-text denial_message fallback.

    Args:
        message: The plain-text denial message string.

    Returns:
        A list of Block Kit block dicts.
    """
    section: dict = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": message,
        },
    }

    if ICONS["error"]:
        section["accessory"] = {
            "type":      "image",
            "image_url": ICONS["error"],
            "alt_text":  "Access Denied",
        }

    return [section]
