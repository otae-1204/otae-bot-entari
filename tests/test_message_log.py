from __future__ import annotations

import unittest

from arclet.entari.logger import log
from loguru import logger

from otae_bot.adapters import message_log
from otae_bot.adapters.message_log import install_message_log_redaction, redact, sensitive_input


def entari_line(user_id: str, content: str, nick: str = "某人", scene: str = "[私聊]") -> str:
    # The exact format of Entari's record_message plugin.
    return f"{scene} {nick}({user_id}) -> {content!r}"


class RedactTests(unittest.TestCase):
    def test_phone_numbers_are_masked_in_every_message(self):
        self.assertEqual(redact("我的手机 13812345678，有事打"), "我的手机 138****5678，有事打")
        self.assertEqual(redact("13812345678"), "138****5678")
        # Not a mainland mobile number: other digits stay as they are.
        for text in ("1095714689", "123456", "/ef 档案 1234", "023-12345678", "138123456789", "UID 12812345678"):
            self.assertEqual(redact(text), text)

    def test_codes_and_tokens_are_masked_only_inside_a_credential_dialog(self):
        token = "FlJTn48gU1OwP9R7lQUpDFZJ"
        self.assertEqual(redact("123456", sensitive=True), "******")
        self.assertEqual(redact(token, sensitive=True), "<已隐藏>")
        self.assertEqual(redact('{"code":0,"data":{"content":"' + token + '"}}', sensitive=True),
                         '{"code":0,"data":{"content":"<已隐藏>"}}')
        self.assertEqual(redact("13812345678", sensitive=True), "138****5678")
        # Menu choices and role UIDs stay readable for diagnosis.
        for text in ("1", "2", "取消", "1、2", "1095714689", "1, 1095714689"):
            self.assertEqual(redact(text, sensitive=True), text)
        self.assertEqual(redact("123456"), "123456")
        self.assertEqual(redact(token), token)

    def test_sensitive_users_are_tracked_per_dialog(self):
        self.assertFalse(message_log.is_sensitive("239539748"))
        with sensitive_input("239539748"):
            with sensitive_input(239539748):
                self.assertTrue(message_log.is_sensitive("239539748"))
            self.assertTrue(message_log.is_sensitive("239539748"))
            self.assertFalse(message_log.is_sensitive("100"))
        self.assertFalse(message_log.is_sensitive("239539748"))
        with self.assertRaises(RuntimeError), sensitive_input("239539748"):
            raise RuntimeError
        self.assertFalse(message_log.is_sensitive("239539748"))


class EntariMessageLogTests(unittest.TestCase):
    def setUp(self):
        original = log.loggers["[message]"]

        def restore():
            log.loggers["[message]"] = original
            message_log._installed = None

        self.addCleanup(restore)
        self.lines = []
        sink = logger.add(lambda message: self.lines.append(message.record["message"]), format="{message}",
                          filter=lambda record: record["name"] == "[message]")
        self.addCleanup(logger.remove, sink)
        install_message_log_redaction()

    def test_the_message_log_masks_credentials_only_for_users_in_a_dialog(self):
        with sensitive_input("239539748"):
            log.message.info(entari_line("239539748", "13812345678"))
            log.message.info(entari_line("239539748", "654321"))
            log.message.info(entari_line("239539748", "1095714689"))
            log.message.info(entari_line("100", "654321", nick="群友(别名)", scene="[某群(977295722)]"))
        log.message.info(entari_line("239539748", "654321"))
        log.message.info(entari_line("100", "联系 13900001111"))
        self.assertEqual(self.lines, [
            "[私聊] 某人(239539748) -> '138****5678'",
            "[私聊] 某人(239539748) -> '******'",
            "[私聊] 某人(239539748) -> '1095714689'",
            "[某群(977295722)] 群友(别名)(100) -> '654321'",
            "[私聊] 某人(239539748) -> '654321'",
            "[私聊] 某人(100) -> '联系 139****1111'",
        ])

    def test_install_is_idempotent(self):
        patched = log.loggers["[message]"]
        install_message_log_redaction()
        self.assertIs(log.loggers["[message]"], patched)
        with sensitive_input("1"):
            log.message.info(entari_line("1", "1234"))
        self.assertEqual(self.lines, ["[私聊] 某人(1) -> '******'"])


if __name__ == "__main__":
    unittest.main()
