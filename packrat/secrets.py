"""Loads secrets.toml (never logged or returned by the API)."""
import os
import tomllib

from .config import default_path


def load() -> dict:
    path = os.environ.get("PACKRAT_SECRETS",
                          os.path.join(os.path.dirname(os.path.abspath(default_path())), "secrets.toml"))
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def redact(text: str, secrets: dict) -> str:
    for value in secrets.values():
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "***")
    return text
