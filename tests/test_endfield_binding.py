from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import plugins.endfield.handlers as endfield
from plugins.endfield.account.client import ACCOUNT_PROVIDER_CN
from plugins.endfield.account.store import RoleCandidate
from plugins.endfield.catalog import commands as commands_module

USER = "239539748"
PHONE, CODE, TOKEN = "13812345678", "654321", "sms-account-token-secret"
ROLES = [
    RoleCandidate("b1", "1095714689", "1", "甲", "China"),
    RoleCandidate("b2", "1770431888", "1", "乙", "China"),
    RoleCandidate("b3", "1770431999", "2", "丙", "Asia"),
]


def reply(text):
    return SimpleNamespace(extract_plain_text=lambda: text)


class SelectionParsingTests(unittest.TestCase):
    def test_numbers_and_listed_uids_mix_with_any_separator(self):
        parse = endfield.parse_binding_selection
        self.assertEqual(parse("1095714689", ROLES), ROLES[:1])  # The reply that used to cancel binding.
        self.assertEqual(parse("1、1770431888", ROLES), ROLES[:2])
        self.assertEqual(parse("3, 1095714689", ROLES), [ROLES[0], ROLES[2]])
        self.assertEqual(parse("1 2，3", ROLES), ROLES)
        self.assertEqual(parse("UID 1770431999", ROLES), ROLES[2:])
        self.assertEqual(parse("uid:1770431888；1号", ROLES), ROLES[:2])
        self.assertEqual(parse("２", ROLES), ROLES[1:2])  # Full-width digits from IME.
        self.assertEqual(parse("全部", ROLES), ROLES)
        self.assertEqual(parse("2 2", ROLES), ROLES[1:2])

    def test_anything_not_in_the_list_is_invalid(self):
        for answer in ("4", "0", "1095714680", "甲", "1 x", "", "、", "1.5"):
            with self.subTest(answer=answer):
                self.assertIsNone(endfield.parse_binding_selection(answer, ROLES))


class BindingFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = mock.Mock()
        self.store.list_roles.return_value = []
        self.store.bind_roles.side_effect = lambda _user, _token, selected, _cipher: list(selected)
        self.client = mock.Mock(
            send_phone_code=mock.AsyncMock(), token_by_phone_code=mock.AsyncMock(return_value=TOKEN),
            discover_roles=mock.AsyncMock(return_value=list(ROLES)),
        )
        self.matcher = mock.Mock(send=mock.AsyncMock(), finish=mock.AsyncMock())
        self.prompts = []
        for target in (
            mock.patch.object(endfield, "account_store", self.store),
            mock.patch.object(endfield, "official_client", self.client),
            mock.patch.object(endfield.ownership_stats_service, "refresh_roles", mock.AsyncMock()),
        ):
            self.enterContext(target)

    async def run_binding(self, *answers):
        answers = list(answers)

        async def prompt(message, timeout):
            self.prompts.append(message)
            answer = answers.pop(0)
            return None if answer is None else reply(answer)

        event = SimpleNamespace()
        command = commands_module.ParsedEndfieldCommand("bind")
        with mock.patch.object(endfield, "prompt", prompt), mock.patch.object(endfield, "is_group", return_value=False), \
             mock.patch.object(endfield, "event_user_id", return_value=USER), \
             mock.patch.object(endfield.CredentialCipher, "from_env", return_value=mock.Mock()):
            await endfield._handle_personal_command(self.matcher, event, command)
        self.assertEqual(answers, [])
        return self.matcher.finish.await_args.args[0]

    async def test_sms_binding_accepts_a_uid(self):
        finished = await self.run_binding("1", "2", PHONE, CODE, "1095714689")
        self.assertIn("国服绑定完成：新增 1 个账号", finished)
        self.assertIn("UID 1095714689", finished)
        self.store.bind_roles.assert_called_once()
        self.assertEqual(self.store.bind_roles.call_args.args[2], ROLES[:1])
        self.client.send_phone_code.assert_awaited_once_with(PHONE)
        self.client.token_by_phone_code.assert_awaited_once_with(PHONE, CODE)

    async def test_invalid_selection_asks_again_and_numbers_still_work(self):
        finished = await self.run_binding("1", "2", PHONE, CODE, "9", "UID 1770431888、3")
        self.assertIn("新增 2 个账号", finished)
        self.assertEqual(self.store.bind_roles.call_args.args[2], ROLES[1:])
        retry = self.prompts[-1]
        self.assertTrue(retry.startswith("编号无效，请回复列表前面的序号"))
        self.assertIn("1. 甲", retry)

    async def test_repeated_invalid_selection_gives_up_after_three_prompts(self):
        finished = await self.run_binding("1", "2", PHONE, CODE, "9", "甲", "1095714680")
        self.assertEqual(finished, "连续 3 次未选中有效角色，绑定已取消；需要时请重新发送 /ef 绑定。")
        self.store.bind_roles.assert_not_called()
        self.assertEqual(sum(message.startswith("编号无效") for message in self.prompts), 2)

    async def test_cancel_and_timeout_are_told_apart(self):
        cases = [
            (("取消",), "已取消绑定。"),
            ((None,), "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。"),
            (("1", "2", PHONE, "cancel"), "已取消绑定。"),
            (("1", "2", PHONE, None), "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。"),
            (("1", "2", PHONE, CODE, "取消"), "已取消绑定。"),
            (("1", "2", PHONE, CODE, None), "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。"),
            (("2", None), "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                self.assertEqual(await self.run_binding(*answers), message)
        self.store.bind_roles.assert_not_called()

    async def test_token_binding(self):
        finished = await self.run_binding("1", "1", TOKEN, "全部")
        self.assertIn("新增 3 个账号", finished)
        self.assertEqual(self.client.discover_roles.await_args.args[0],
                         endfield.encode_account_credential(TOKEN, ACCOUNT_PROVIDER_CN))

    async def test_a_single_role_needs_no_selection(self):
        self.client.discover_roles.return_value = ROLES[:1]
        finished = await self.run_binding("1", "1", TOKEN)
        self.assertIn("新增 1 个账号", finished)


if __name__ == "__main__":
    unittest.main()
