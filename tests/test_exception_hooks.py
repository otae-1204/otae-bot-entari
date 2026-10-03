"""Entari 的 loguru_exc_callback 在 Python 3.13 的 print_exc() 下会再抛 TypeError。"""

from __future__ import annotations

import sys
import traceback
import unittest
from unittest import mock

from otae_bot.adapters import exception_hooks


def _raised(message: str = "quote is empty") -> ValueError:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


class ExceptionHookTests(unittest.TestCase):
    def setUp(self):
        self.callback = mock.Mock()
        patcher = mock.patch.object(exception_hooks, "_entari_callback", return_value=self.callback)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_entari_callback_rejects_the_single_argument_form(self):
        # 复现生产问题：3.13 的 print_exc() 调用 print_exception(exc, limit=..., file=..., chain=...)
        from arclet.entari.logger import loguru_exc_callback

        with self.assertRaises(TypeError):
            loguru_exc_callback(_raised(), limit=None, file=None, chain=True)

    def test_single_argument_form_logs_the_original_exception(self):
        error = _raised()
        exception_hooks.print_exception(error, limit=None, file=None, chain=True)
        self.callback.assert_called_once_with(ValueError, error, error.__traceback__)

    def test_python_313_print_exc_inside_except_block(self):
        try:
            raise KeyError("quote")
        except KeyError:
            current = sys.exc_info()[1]
            # 与 3.13 traceback.print_exc() 完全相同的调用方式
            exception_hooks.print_exception(current, limit=None, file=None, chain=True)
        self.callback.assert_called_once()
        cls, value, tb = self.callback.call_args.args
        self.assertIs(cls, KeyError)
        self.assertIs(value, current)
        self.assertIsNotNone(tb)

    def test_no_active_exception_is_a_quiet_no_op(self):
        exception_hooks.print_exception(None, limit=None, file=None, chain=True)
        exception_hooks.print_exception(None, None, None)
        exception_hooks.print_exception(ValueError, None, None)
        exception_hooks.excepthook(None, None, None)
        self.callback.assert_not_called()

    def test_legacy_three_argument_form_still_works(self):
        error = _raised()
        exception_hooks.print_exception(type(error), error, error.__traceback__, None, sys.stderr)
        exception_hooks.excepthook(type(error), error, None)
        self.assertEqual(
            self.callback.call_args_list,
            [
                mock.call(ValueError, error, error.__traceback__),
                mock.call(ValueError, error, None),
            ],
        )

    def test_install_replaces_entari_hooks_and_print_exc_works(self):
        saved = (traceback.print_exception, sys.excepthook)
        try:
            exception_hooks.install_exception_hooks()
            self.assertIs(traceback.print_exception, exception_hooks.print_exception)
            self.assertIs(sys.excepthook, exception_hooks.excepthook)
            try:
                raise RuntimeError("event callback failed")
            except RuntimeError:
                traceback.print_exc()
            traceback.print_exc()  # 没有正在处理的异常：不再抛 TypeError
        finally:
            traceback.print_exception, sys.excepthook = saved
        self.assertEqual(self.callback.call_count, 1)
        self.assertIs(self.callback.call_args.args[0], RuntimeError)

    def test_create_app_installs_the_hooks(self):
        from otae_bot import application

        saved = (traceback.print_exception, sys.excepthook)
        try:
            with (
                mock.patch.object(application, "Entari"),
                mock.patch.object(application, "build_networks", return_value=[]),
                mock.patch.object(application, "install_quoted_command_mentions"),
                mock.patch.object(application, "listen", return_value=lambda handler: handler),
                mock.patch.object(application, "discover_plugins", return_value=[]),
                mock.patch.object(application, "install_group_feature_gates"),
            ):
                application.create_app()
            self.assertIs(traceback.print_exception, exception_hooks.print_exception)
            self.assertIs(sys.excepthook, exception_hooks.excepthook)
        finally:
            traceback.print_exception, sys.excepthook = saved


if __name__ == "__main__":
    unittest.main()
