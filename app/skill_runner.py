"""
skill_runner.py
---------------
Executes an AI skill in response to a modal form submission.

HOW SKILLS WORK
---------------
A skill is a SKILL.md file that encodes how the AI should perform a specific
task (e.g. IT onboarding provisioning). This module:

  1. Reads the SKILL.md from /mnt/skills/<skill_name>/SKILL.md
  2. Builds a prompt from the SKILL.md instructions + the form values
  3. Calls the configured AI model via LiteLLM and returns the result

SKILL DIRECTORY LAYOUT
-----------------------
Skills are looked up by name from the paths defined in SKILL_SEARCH_PATHS.
The first match wins. Paths are checked in order, allowing user skills
(higher priority) to override public skills (lower priority).

COMMANDS.YAML USAGE
--------------------
A command uses a skill instead of a webhook by setting action_type and skill_name:

    provision-hardware:
      description: "Provision hardware for a new hire"
      action_type: skill
      skill_name: it-onboarding-provisioner
      allowed:
        - it-admins
      fields:
        - id: employee_name
          ...

USAGE
-----
    from app import skill_runner

    result = skill_runner.run(
        skill_name="it-onboarding-provisioner",
        command_name="provision-hardware",
        values={"employee_name": "Alex Johnson", ...},
        user_id="U012AB3CD",
    )
    # result is a human-readable string, suitable for posting to Slack
"""

import logging
import pathlib

import litellm

from app.settings_loader import AI_MODEL

logger = logging.getLogger(__name__)

# Ordered list of base directories to search for skills.
# User skills (higher specificity) are checked before public skills.
SKILL_SEARCH_PATHS = [
    pathlib.Path("/mnt/skills/user"),
    pathlib.Path("/mnt/skills/examples"),
    pathlib.Path("/mnt/skills/public"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_skill_md(skill_name: str) -> pathlib.Path | None:
    """
    Locate SKILL.md for the given skill name.

    Searches SKILL_SEARCH_PATHS in order and returns the first match.

    Args:
        skill_name: The skill folder name, e.g. "it-onboarding-provisioner".

    Returns:
        A Path to the SKILL.md file, or None if not found.
    """
    for base in SKILL_SEARCH_PATHS:
        candidate = base / skill_name / "SKILL.md"
        if candidate.exists():
            logger.debug("Found skill '%s' at %s", skill_name, candidate)
            return candidate

    logger.warning("Skill '%s' not found in any search path.", skill_name)
    return None


def _build_prompt(skill_md: str, command_name: str, values: dict) -> str:
    """
    Build the user prompt sent to the AI model when running a skill.

    Combines the SKILL.md content (which describes the task and any special
    instructions) with the flat dict of form values the user submitted.

    Args:
        skill_md:     The raw text of the skill's SKILL.md file.
        command_name: The name of the command that triggered this skill run.
        values:       The flat dict of field_id → submitted value from the form.

    Returns:
        A formatted prompt string ready to send to the AI model.
    """
    # Format the values dict as a readable YAML-like block
    values_block = "\n".join(
        f"  {k}: {v}" for k, v in values.items()
    )

    return (
        f"# Task\n\n"
        f"You have been invoked via the `{command_name}` command with the following inputs:\n\n"
        f"```\n{values_block}\n```\n\n"
        f"# Skill Instructions\n\n"
        f"{skill_md}\n\n"
        f"# Instructions\n\n"
        f"Follow the skill instructions above using the provided inputs. "
        f"Report the outcome clearly and concisely. "
        f"If the skill involves taking actions (e.g. creating Jira tickets, "
        f"assigning hardware), do so now and summarise what was done. "
        f"If anything is missing or ambiguous, say so."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    skill_name: str,
    command_name: str,
    values: dict,
    user_id: str,
) -> str:
    """
    Execute a skill and return a human-readable result string.

    Loads the named skill's SKILL.md, builds a prompt incorporating the
    submitted form values, calls the AI model, and returns the reply text.

    Never raises — all errors are caught and returned as readable strings
    so handlers.py can post them directly to Slack without crashing.

    Args:
        skill_name:   The skill folder name from commands.yaml, e.g.
                      "it-onboarding-provisioner".
        command_name: The command that triggered this run. Used in the prompt
                      and log messages.
        values:       The flat dict of field values from the submitted form.
        user_id:      The Slack user ID of the submitter. Used in log messages.

    Returns:
        A human-readable result string from the AI model, or an error message
        if the skill could not be loaded or the API call failed.
    """
    skill_path = _find_skill_md(skill_name)

    if skill_path is None:
        msg = (
            f"❌ Skill `{skill_name}` not found. "
            f"Check that the skill folder exists at one of the configured skill paths."
        )
        logger.error(
            "Skill '%s' not found for command '%s' submitted by user %s.",
            skill_name, command_name, user_id,
        )
        return msg

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as error:
        logger.error("Failed to read skill '%s': %s", skill_name, error)
        return f"❌ Could not read skill `{skill_name}`: {error}"

    prompt = _build_prompt(skill_md, command_name, values)

    logger.info(
        "Running skill '%s' for command '%s' submitted by user %s.",
        skill_name, command_name, user_id,
    )

    try:
        api_response = litellm.completion(
            model=AI_MODEL,
            max_tokens=2000,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an IT operations assistant executing an automated skill. "
                        "Follow the skill instructions precisely. "
                        "Be concise and report clearly what was done or what information was found. "
                        "Do not include preamble or meta-commentary about the skill itself."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )

        reply = api_response.choices[0].message.content
        logger.info(
            "Skill '%s' completed for user %s. Response length: %d chars.",
            skill_name, user_id, len(reply),
        )
        return reply

    except Exception as error:
        logger.exception(
            "Skill '%s' API call failed for user %s: %s",
            skill_name, user_id, error,
        )
        return f"❌ Skill execution failed: {error}"
