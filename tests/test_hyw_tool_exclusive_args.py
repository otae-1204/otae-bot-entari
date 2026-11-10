"""互斥参数在工具入口校验：schema 已平铺（Gemini 拒绝 anyOf 与其他字段并列），由运行时拦截。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hyw_frontier")

from hyw_frontier.tools import ToolRuntime, exclusive_arguments_error

SECRET_URL = "https://img.example.test/a.png?token=SECRET-TOKEN-123"


class _Runtime:
    def __init__(self):
        self.jina = MagicMock(name="jina")
        self.tools = ToolRuntime(self.jina, search_provider="jina")
        self.reverse = MagicMock(return_value={"matches": []})
        self.tools.reverse_image_search = self.reverse
        self.tools.pageshots.run = MagicMock(return_value=({"ok": True}, []))

    def call(self, name, arguments):
        message = self.tools.execute({"id": "c1", "name": name, "arguments": arguments})
        return message, json.loads(message["content"][0]["text"])


class ReverseImageSearchArgsTests(unittest.TestCase):
    def setUp(self):
        self.rt = _Runtime()

    def test_both_source_id_and_url_is_rejected_before_searching(self):
        message, result = self.rt.call("reverse_image_search", {"source_id": "img_1", "url": SECRET_URL})
        self.assertTrue(message["isError"])
        self.assertEqual(result["code"], "invalid_arguments")
        self.assertIn("不能同时传", result["error"])
        self.assertNotIn("SECRET", message["content"][0]["text"])
        self.rt.reverse.assert_not_called()

    def test_neither_source_id_nor_url_is_rejected_before_searching(self):
        for arguments in ({}, {"page": 2}):
            with self.subTest(arguments=arguments):
                message, result = self.rt.call("reverse_image_search", arguments)
                self.assertTrue(message["isError"])
                self.assertEqual(result["code"], "invalid_arguments")
                self.assertIn("恰好传一个", result["error"])
        self.rt.reverse.assert_not_called()

    def test_exactly_one_of_them_runs_the_search(self):
        for arguments in ({"source_id": "img_1"}, {"url": "https://img.example.test/a.png", "page": 2}):
            with self.subTest(arguments=arguments):
                self.rt.reverse.reset_mock()
                message, result = self.rt.call("reverse_image_search", arguments)
                self.assertFalse(message["isError"], result)
                self.rt.reverse.assert_called_once_with(arguments)

    def test_unknown_field_is_still_rejected_by_schema(self):
        _, result = self.rt.call("reverse_image_search", {"source_id": "img_1", "extra": 1})
        self.assertEqual(result["code"], "invalid_arguments")
        self.rt.reverse.assert_not_called()


class PageshotArgsTests(unittest.TestCase):
    def setUp(self):
        self.rt = _Runtime()

    def test_invalid_combinations_never_reach_jina(self):
        cases = {
            "url_with_crop_fields": ({"url": SECRET_URL, "pageshot_id": "pageshot_0123456789", "bbox": [0, 0, 5, 5]}, "不能与"),
            "url_with_bbox": ({"url": SECRET_URL, "bbox": [0, 0, 5, 5]}, "不能与"),
            "url_with_id": ({"url": SECRET_URL, "pageshot_id": "pageshot_0123456789"}, "不能与"),
            "id_without_bbox": ({"pageshot_id": "pageshot_0123456789"}, "同时传 pageshot_id 与 bbox"),
            "bbox_without_id": ({"bbox": [0, 0, 5, 5]}, "同时传 pageshot_id 与 bbox"),
            "nothing": ({}, "缺少参数"),
        }
        for label, (arguments, fragment) in cases.items():
            with self.subTest(label):
                message, result = self.rt.call("jina_pageshot", arguments)
                self.assertTrue(message["isError"])
                self.assertEqual(result["code"], "invalid_arguments")
                self.assertIn(fragment, result["error"])
                self.assertNotIn("SECRET", message["content"][0]["text"])
        self.rt.tools.pageshots.run.assert_not_called()
        self.rt.jina.pageshot_url.assert_not_called()

    def test_capture_and_crop_each_run(self):
        for arguments in ({"url": "https://example.test/page"},
                          {"pageshot_id": "pageshot_0123456789", "bbox": [0, 0, 5, 5]}):
            with self.subTest(arguments=arguments):
                self.rt.tools.pageshots.run.reset_mock()
                message, result = self.rt.call("jina_pageshot", arguments)
                self.assertFalse(message["isError"], result)
                self.rt.tools.pageshots.run.assert_called_once()
                self.assertEqual(self.rt.tools.pageshots.run.call_args.args[0], arguments)


class CompanionTests(unittest.TestCase):
    def test_invalid_exclusive_call_does_not_unlock_process_intro(self):
        rt = _Runtime()
        sent = []
        rt.tools.send = sent.append
        results = rt.tools.execute_many([
            {"id": "a", "name": "send_process_intro", "arguments": {"text": "我先以图搜图"}},
            {"id": "b", "name": "reverse_image_search", "arguments": {"source_id": "img_1", "url": SECRET_URL}},
        ])
        intro = json.loads(results[0]["content"][0]["text"])
        self.assertEqual(intro["code"], "companion_required")
        self.assertEqual(sent, [])
        rt.reverse.assert_not_called()


class ExclusiveArgumentsErrorTests(unittest.TestCase):
    def test_other_tools_and_non_dict_arguments_are_ignored(self):
        self.assertIsNone(exclusive_arguments_error("web_search", {}))
        self.assertIsNone(exclusive_arguments_error("reverse_image_search", None))
        self.assertIsNone(exclusive_arguments_error("jina_pageshot", "url"))


if __name__ == "__main__":
    unittest.main()
