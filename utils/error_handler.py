"""Compatibility import for :mod:`otae_bot.infrastructure.errors`."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("otae_bot.infrastructure.errors")
