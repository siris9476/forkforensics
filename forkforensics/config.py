"""GitHub token resolution and shared data paths. The token is NEVER
logged in plaintext; resolution order: env var > value saved in
settings (SQLite)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _resolve_data_dir() -> Path:
    """In a PyInstaller --onefile executable, __file__ points to the
    temporary extraction folder (_MEIxxxxxx) - not only is it a different
    path on every launch, but the bootloader DELETES it when the process
    exits. Verified live: the database, the log, everything under data/
    disappeared on every close of the executable, starting empty every
    time. From frozen (the attribute PyInstaller sets on the packaged
    executable) we instead use a stable user folder, which survives
    restarts and does not depend on where the executable was
    placed/moved."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(Path.home())
        # matches the branding casing used everywhere else this path is
        # documented (README, SECURITY.md) - was lowercase "forkforensics"
        # here, silently mismatched, though harmless on case-insensitive
        # Windows filesystems
        return Path(base) / "ForkForensics"
    return Path(__file__).resolve().parent.parent / "data"


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "forkforensics.db"
RAW_ARCHIVE_DIR = DATA_DIR / "raw_archive"


@dataclass
class Settings:
    cache_dir: Path = DATA_DIR
    db_path: Path = DB_PATH
    raw_archive_dir: Path = RAW_ARCHIVE_DIR


def resolve_token(cache=None) -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if cache is not None:
        return cache.get_setting("github_token")
    return None


SETTINGS = Settings()
