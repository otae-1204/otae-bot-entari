"""Entari 的 loguru_exc_callback 在 Python 3.12/3.13 的 print_exc() 下会再抛 TypeError。"""

from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import sys
import traceback
import unittest
from pathlib import Path
from unittest import mock

from otae_bot.adapters import exception_hooks

ROOT = Path(__file__).resolve().parents[1]


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

    def test_python_313_builtin_display_convention(self):
        # 3.13 解释器显示异常时走 traceback._print_exception_bltin(exc)：
        # print_exception(exc, limit=BUILTIN_EXCEPTION_LIMIT, file=..., colorize=...)
        error = _raised()
        exception_hooks.print_exception(error, limit=object(), file=sys.stderr, colorize=True)
        self.callback.assert_called_once_with(ValueError, error, error.__traceback__)

    def test_logging_conventions_with_explicit_file(self):
        error = _raised()
        # logging.Handler.handleError（3.12）与 Formatter.formatException（3.13）
        exception_hooks.print_exception(ValueError, error, error.__traceback__, None, sys.stderr)
        exception_hooks.print_exception(ValueError, error, error.__traceback__, limit=None, file=io.StringIO())
        self.assertEqual(self.callback.call_count, 2)

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

    def test_create_app_installs_the_hooks_and_rechecks_them_on_startup(self):
        from otae_bot import application

        registered = []

        def listen(event):
            def register(handler):
                registered.append((event, handler))
                return handler
            return register

        saved = (traceback.print_exception, sys.excepthook)
        try:
            with (
                mock.patch.object(application, "Entari"),
                mock.patch.object(application, "build_networks", return_value=[]),
                mock.patch.object(application, "install_quote_fetch_fallback"),
                mock.patch.object(application, "install_quoted_command_mentions"),
                mock.patch.object(application, "listen", listen),
                mock.patch.object(application, "discover_plugins", return_value=[]),
                mock.patch.object(application, "install_group_feature_gates"),
            ):
                application.create_app()
            self.assertIs(traceback.print_exception, exception_hooks.print_exception)
            self.assertIs(sys.excepthook, exception_hooks.excepthook)
            self.assertIn((application.Startup, exception_hooks.reassert_exception_hooks), registered)
        finally:
            traceback.print_exception, sys.excepthook = saved

    def test_main_installs_the_hooks_before_startup_work(self):
        from otae_bot import application

        saved = (traceback.print_exception, sys.excepthook)
        order = []
        try:
            with (
                mock.patch.object(application, "acquire_run_lock", return_value=mock.Mock()),
                mock.patch.object(application, "install_exception_hooks", side_effect=lambda: order.append("hooks")),
                mock.patch.object(application, "prewarm_shared_ssl_context", side_effect=lambda: order.append("tls")),
                mock.patch.object(application, "create_app", side_effect=RuntimeError("stop")),
            ):
                with self.assertRaises(RuntimeError):
                    application.main()
        finally:
            traceback.print_exception, sys.excepthook = saved
        self.assertEqual(order, ["hooks", "tls"])


class ReassertTests(unittest.TestCase):
    def setUp(self):
        saved = (traceback.print_exception, sys.excepthook)
        self.addCleanup(lambda: (setattr(traceback, "print_exception", saved[0]), setattr(sys, "excepthook", saved[1])))
        patcher = mock.patch.object(exception_hooks, "logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_startup_check_reinstalls_after_entari_overrides_again(self):
        from arclet.entari.logger import loguru_exc_callback

        exception_hooks.install_exception_hooks()
        # entari/logger.py 第 217、219 行再次执行时做的事
        traceback.print_exception = loguru_exc_callback
        sys.excepthook = loguru_exc_callback
        self.assertFalse(exception_hooks.hooks_installed())
        asyncio.run(exception_hooks.reassert_exception_hooks())
        self.assertTrue(exception_hooks.hooks_installed())
        self.log.warning.assert_called_once()
        self.assertIn("arclet.entari.logger.loguru_exc_callback", self.log.warning.call_args.args)

    def test_startup_check_is_quiet_when_hooks_are_in_place(self):
        exception_hooks.install_exception_hooks()
        asyncio.run(exception_hooks.reassert_exception_hooks())
        self.log.warning.assert_not_called()


# 子进程里从干净的解释器开始，模拟真实导入顺序；不访问网络。
ORDER_SCRIPT = r'''
import importlib, io, sys, traceback
sys.path.insert(0, sys.argv[1])
assert 'arclet.entari.logger' not in sys.modules
from otae_bot.adapters import exception_hooks as hooks

# 1) 先装钩子、后导入 Entari：安装时已先导入 entari 的 logger，之后不会再被覆盖
hooks.install_exception_hooks()
import arclet.entari
assert hooks.hooks_installed(), (traceback.print_exception, sys.excepthook)

def raise_and(call):
    try:
        raise RuntimeError('quote is empty')
    except RuntimeError as error:
        call(error)

# 2) 3.12/3.13 的各种调用约定都不抛 TypeError，都写进 Entari 的错误日志
raise_and(lambda e: traceback.print_exc())                                             # print_exc：单参数 + limit/file/chain
raise_and(lambda e: traceback.print_exception(e, limit=object(), file=sys.stderr, colorize=True))  # 3.13 _print_exception_bltin
raise_and(lambda e: traceback.print_exception(type(e), e, e.__traceback__, None, sys.stderr))      # logging.Handler.handleError
raise_and(lambda e: traceback.print_exception(type(e), e, e.__traceback__, limit=None, file=io.StringIO()))  # Formatter.formatException
raise_and(lambda e: sys.excepthook(type(e), e, e.__traceback__))
if hasattr(traceback, '_print_exception_bltin'):
    raise_and(lambda e: traceback._print_exception_bltin(e))
traceback.print_exc()   # 没有正在处理的异常
print('CALLS-OK')

# 3) Entari 的 logger 再次执行覆盖后，启动核对把钩子装回来
importlib.reload(sys.modules['arclet.entari.logger'])
assert not hooks.hooks_installed()
import asyncio
asyncio.run(hooks.reassert_exception_hooks())
assert hooks.hooks_installed()
raise_and(lambda e: traceback.print_exc())
print('REASSERT-OK')
'''


class ImportOrderTests(unittest.TestCase):
    def test_hooks_hold_regardless_of_import_order_and_calling_convention(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", ORDER_SCRIPT, str(ROOT)], cwd=ROOT,
            capture_output=True, text=True, timeout=120, env=env,
        )
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn("CALLS-OK", output)
        self.assertIn("REASSERT-OK", output)
        self.assertNotIn("TypeError", output)
        # 每次调用都由 Entari 的回调记一条：5 种约定 + 3.13 的 _print_exception_bltin + 重装后的 print_exc
        expected = 6 + hasattr(traceback, "_print_exception_bltin")
        self.assertEqual(output.count("RuntimeError: quote is empty"), expected, output[-4000:])


if __name__ == "__main__":
    unittest.main()
