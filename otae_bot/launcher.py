"""Process entry: load entari.yml first, then import and run the application.

The configuration must be in place before ``otae_bot.application`` is imported:
plugins and some shared modules read their settings at import time.
"""

from __future__ import annotations

from otae_bot.config.settings import CONFIG_FILE, ConfigFileMissing, load_config


def run() -> int:
    try:
        load_config()
    except ConfigFileMissing:
        print(f"[ERROR] {CONFIG_FILE} not found in the working directory.")
        print("[ERROR] Copy entari.example.yml to entari.yml and edit it, or convert an old")
        print("[ERROR] deployment with: python scripts/convert_config.py --apply")
        return 2
    from otae_bot.application import main

    return main()
