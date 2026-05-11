"""
db.py
-----
Database layer for persisting bot configuration (settings and commands) and
breakglass credentials in PostgreSQL.

WHY THIS MODULE EXISTS
----------------------
The original design loaded settings.yaml and commands.yaml from disk at
startup. This module replaces that file I/O with a PostgreSQL backend so
configuration survives redeploys, can be edited through the Scriptorium UI
without touching the filesystem, and supports multi-environment deployments
without file management.

WHAT THIS MODULE OWNS
----------------------
  - SQLAlchemy engine (created once, shared across all callers)
  - Schema creation (idempotent — safe to call on every startup)
  - load_settings()  / save_settings()   — full settings blob
  - load_commands()  / save_commands()   — full commands registry
  - save_breakglass()                    — hashed breakglass credentials
  - check_breakglass()                   — credential verification
  - test_connection()                    — connectivity probe for the wizard

SCHEMA
------
Three tables are created under the public schema:

  config_settings   — single-row JSONB blob for all settings.yaml values
  config_commands   — one row per command, JSONB for the full command config
  breakglass_users  — breakglass admin credentials (bcrypt-hashed passwords)

All tables are created with CREATE TABLE IF NOT EXISTS so startup is safe
on a pre-provisioned database and on a brand-new one alike.

THREAD SAFETY
-------------
SQLAlchemy's engine uses a connection pool that is safe for concurrent access
from multiple threads (the Bolt Socket Mode thread and the Flask callback
thread both call into this module). No additional locking is needed.

USAGE
-----
    from app.db import load_settings, save_settings, load_commands, save_commands

    raw = load_settings()          # dict | None
    save_settings(settings_dict)
    cmds = load_commands()         # dict | None
    save_commands(commands_dict)

ADDING A NEW TABLE
------------------
1. Add a CREATE TABLE IF NOT EXISTS block in _create_schema().
2. Add load/save helpers below.
3. Call any new migration logic from ensure_schema() if needed.
"""

import json
import logging

import bcrypt
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine — created lazily so import-time failures are loud and informative
# ---------------------------------------------------------------------------

_engine = None


def get_engine():
    """
    Return the shared SQLAlchemy engine, creating it on first call.

    The engine is configured for a long-running server process:
      - pool_pre_ping=True: validates connections before use so stale
        connections from pool don't cause cryptic errors mid-request.
      - pool_size / max_overflow: modest defaults for a single-process bot.
        Increase if you add heavy concurrent usage.

    Raises:
        RuntimeError: If DATABASE_URL is not set in the environment.

    Returns:
        A SQLAlchemy Engine instance.
    """
    global _engine
    if _engine is None:
        from app.config import DATABASE_URL  # lazy to avoid circular import at startup

        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. "
                "Add it to your .env file or deployment environment.\n"
                "Format: postgresql://user:password@host:5432/dbname?sslmode=require"
            )

        logger.info("Creating database engine (host redacted from log).")
        _engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 10},
        )

    return _engine


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_settings (
    id          SERIAL PRIMARY KEY,
    data        JSONB  NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS config_commands (
    name        TEXT   PRIMARY KEY,
    data        JSONB  NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS breakglass_users (
    username     TEXT NOT NULL PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_schema() -> None:
    """
    Create all required tables if they don't already exist.

    Safe to call on every startup — all statements use IF NOT EXISTS.
    Logs a warning and raises on failure so the caller can decide whether
    to abort or fall back to YAML.

    Raises:
        SQLAlchemyError: If the schema creation query fails.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(_SCHEMA_SQL))
        logger.info("Database schema verified.")
    except SQLAlchemyError as exc:
        logger.error("Failed to create schema: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Connectivity probe — used by the Scriptorium wizard's "Test Connection"
# ---------------------------------------------------------------------------

def test_connection(database_url: str) -> tuple[bool, str]:
    """
    Attempt a lightweight connection to a PostgreSQL database and return
    whether it succeeded.

    Creates a temporary engine (not the shared one) so that an incorrect URL
    from the wizard doesn't poison the module-level engine.

    Args:
        database_url: A full PostgreSQL DSN, e.g.
            "postgresql://user:pass@host:5432/dbname?sslmode=require"

    Returns:
        (True, "")           on success.
        (False, error_msg)   on failure, with a human-readable error message
                             safe to return to the UI (no credentials in it).
    """
    try:
        probe_engine = create_engine(
            database_url,
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=False,
            connect_args={"connect_timeout": 5},
        )
        with probe_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe_engine.dispose()
        logger.info("Database connection test succeeded.")
        return True, ""
    except OperationalError as exc:
        # OperationalError messages from psycopg2 include the host and port
        # but not the password, so they are safe to surface.
        short = str(exc.orig).splitlines()[0] if exc.orig else str(exc)
        logger.warning("Database connection test failed: %s", short)
        return False, short
    except Exception as exc:  # noqa: BLE001
        logger.warning("Database connection test failed (unexpected): %s", exc)
        return False, "Unexpected error — check server logs."


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict | None:
    """
    Load the settings blob from the database.

    Returns the most recently written settings dict, or None if no settings
    have been persisted yet (e.g. on a fresh database before the wizard runs).

    Returns:
        A dict matching the structure of settings.yaml, or None.

    Raises:
        SQLAlchemyError: On database errors (caller should fall back to YAML).
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM config_settings ORDER BY id DESC LIMIT 1")
        ).fetchone()

    if row is None:
        logger.info("No settings found in database.")
        return None

    logger.info("Settings loaded from database.")
    return dict(row[0])


def save_settings(data: dict) -> None:
    """
    Persist a settings dict to the database.

    Uses an upsert pattern: inserts a new row if the table is empty, or
    updates the single canonical row (id=1) if one exists. This keeps the
    table to one active row and avoids unbounded growth.

    Args:
        data: A settings dict matching the structure of settings.yaml.

    Raises:
        SQLAlchemyError: On database errors.
    """
    engine = get_engine()
    payload = json.dumps(data)

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO config_settings (id, data, updated_at)
                VALUES (1, :data::jsonb, now())
                ON CONFLICT (id) DO UPDATE
                    SET data       = EXCLUDED.data,
                        updated_at = EXCLUDED.updated_at
            """),
            {"data": payload},
        )

    logger.info("Settings saved to database.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def load_commands() -> dict | None:
    """
    Load the full command registry from the database.

    Returns a dict keyed by command name (matching the structure of
    commands.yaml → commands), or None if no commands have been persisted.

    Returns:
        A dict of { command_name: command_config_dict } or None.

    Raises:
        SQLAlchemyError: On database errors (caller should fall back to YAML).
    """
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, data FROM config_commands ORDER BY name")
        ).fetchall()

    if not rows:
        logger.info("No commands found in database.")
        return None

    commands = {row[0]: dict(row[1]) for row in rows}
    logger.info("Loaded %d command(s) from database.", len(commands))
    return commands


def save_commands(commands: dict) -> None:
    """
    Persist the full command registry to the database.

    Replaces the entire commands table with the provided registry using a
    delete-then-insert approach inside a single transaction. This keeps the
    table in sync with whatever the wizard saves — including deletions.

    Args:
        commands: A dict of { command_name: command_config_dict }.

    Raises:
        SQLAlchemyError: On database errors.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM config_commands"))
        for name, config in commands.items():
            conn.execute(
                text("""
                    INSERT INTO config_commands (name, data, updated_at)
                    VALUES (:name, :data::jsonb, now())
                """),
                {"name": name, "data": json.dumps(config)},
            )

    logger.info("Saved %d command(s) to database.", len(commands))


# ---------------------------------------------------------------------------
# Breakglass credentials
# ---------------------------------------------------------------------------

def save_breakglass(username: str, password: str) -> None:
    """
    Hash and persist a breakglass admin credential.

    Uses bcrypt with a work factor of 12 — high enough to be resistant to
    brute force but fast enough not to block startup. The plain-text password
    is never stored and is not logged.

    If a row for the username already exists it is replaced, so the wizard
    can re-run and reset credentials safely.

    Args:
        username: The breakglass admin username (plain text).
        password: The breakglass admin password (plain text — hashed here).

    Raises:
        SQLAlchemyError: On database errors.
    """
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO breakglass_users (username, password_hash)
                VALUES (:username, :password_hash)
                ON CONFLICT (username) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash
            """),
            {"username": username, "password_hash": password_hash},
        )

    logger.info("Breakglass credential saved for user %r.", username)


def check_breakglass(username: str, password: str) -> bool:
    """
    Verify a breakglass credential against the stored bcrypt hash.

    Performs a constant-time comparison via bcrypt.checkpw to prevent
    timing-based enumeration of valid usernames.

    Args:
        username: The breakglass admin username.
        password: The plain-text password to verify.

    Returns:
        True if the credentials are valid, False otherwise.
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT password_hash FROM breakglass_users WHERE username = :u"),
            {"u": username},
        ).fetchone()

    if row is None:
        # Perform a dummy hash check to avoid leaking whether the user exists
        # via timing differences.
        bcrypt.checkpw(b"dummy", bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12)))
        return False

    return bcrypt.checkpw(password.encode("utf-8"), row[0].encode("utf-8"))
