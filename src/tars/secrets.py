"""API keys and tokens live in the operating system's credential store.

On Windows this is Credential Manager (via ``keyring``). An environment variable
of the same name overrides the store, which is handy for tests and first runs.
"""

from __future__ import annotations

import os

SERVICE = "TARS"

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
DEEPGRAM_API_KEY = "DEEPGRAM_API_KEY"
ELEVENLABS_API_KEY = "ELEVENLABS_API_KEY"
TODOIST_API_TOKEN = "TODOIST_API_TOKEN"
GOOGLE_MAPS_API_KEY = "GOOGLE_MAPS_API_KEY"
GOOGLE_OAUTH_TOKEN = "GOOGLE_OAUTH_TOKEN"  # JSON blob written by `tars google-auth`
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

ALL_KEYS = [
    ANTHROPIC_API_KEY,
    DEEPGRAM_API_KEY,
    ELEVENLABS_API_KEY,
    TODOIST_API_TOKEN,
    GOOGLE_MAPS_API_KEY,
    GOOGLE_OAUTH_TOKEN,
    TELEGRAM_BOT_TOKEN,
]


def get_secret(name: str) -> str | None:
    if value := os.environ.get(name):
        return value
    try:
        import keyring

        return keyring.get_password(SERVICE, name)
    except Exception:
        # No usable backend (e.g. a headless Linux box); env vars still work.
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    import keyring

    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
