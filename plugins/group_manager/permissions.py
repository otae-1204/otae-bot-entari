"""Compatibility entrypoint for shared current-group authorization."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("otae_bot.group_permissions")
