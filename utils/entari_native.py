"""Compatibility import for :mod:`otae_bot.adapters.entari`."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("otae_bot.adapters.entari")
