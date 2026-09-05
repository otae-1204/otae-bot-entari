"""Compatibility import for :mod:`otae_bot.infrastructure.storage.plugin_data`."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("otae_bot.infrastructure.storage.plugin_data")
