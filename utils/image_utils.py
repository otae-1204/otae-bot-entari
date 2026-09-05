"""Compatibility import for :mod:`otae_bot.infrastructure.rendering.browser`."""

from importlib import import_module
import sys

_browser = import_module("otae_bot.infrastructure.rendering.browser")
_browser.PILBuildImage = import_module("otae_bot.infrastructure.rendering.pillow").PILBuildImage
_browser.Cv2BuildImage = import_module("otae_bot.infrastructure.rendering.opencv").Cv2BuildImage
sys.modules[__name__] = _browser
