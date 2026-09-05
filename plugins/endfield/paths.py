"""Stable Endfield resource locations, independent of feature-module depth."""

from pathlib import Path

from otae_bot.paths import PROJECT_ROOT


PLUGIN_ROOT = Path(__file__).resolve().parent
IMAGE_DIR = PROJECT_ROOT / "assets" / "image" / "endfield"
HELP_IMAGE_PATH = PROJECT_ROOT / "assets" / "image" / "help" / "endfield.png"
UI_DIR = PLUGIN_ROOT / "assets" / "ui"
CALENDAR_DIR = PLUGIN_ROOT / "assets" / "calendar"
ALIAS_DATA_PATH = PLUGIN_ROOT / "alias_data.json"
