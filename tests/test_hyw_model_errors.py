"""safe_model_error：工具 schema / 请求格式类 400 给固定提示，其他 400 仍隐藏原文。"""
from __future__ import annotations

import unittest

import pytest

pytest.importorskip("hyw_frontier")

from hyw_frontier import model_backend
from hyw_frontier.model_backend import TOOL_SCHEMA_REJECTED_MESSAGE, safe_model_error

GENERIC = "模型请求失败；原始异常已隐藏以保护凭据。"
SECRETS = ("sk-live-SECRET", "AIzaSy-SECRET", "https://gw.example.test", "key=", "token", "Bearer")
LEAKY_TAIL = (" POST https://gw.example.test/v1/chat/completions?key=AIzaSy-SECRET-QUERY&alt=json"
              " Authorization: Bearer sk-live-SECRET-KEY token=tok-SECRET")


class _HTTPError(Exception):
    def __init__(self, message, status_code, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _assert_clean(case: unittest.TestCase, error):
    text = str(error)
    for secret in SECRETS:
        case.assertNotIn(secret, text)
    case.assertNotIn("SECRET", repr(error.diagnostics))


class SchemaRejectionTests(unittest.TestCase):
    def test_gemini_any_of_rejection_gets_the_fixed_hint(self):
        body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": (
            "Unable to submit request because 'reverse_image_search' functionDeclaration 'parameters' schema "
            "specified other fields alongside any_of. When using any_of, it must be the only field set.")}}
        error = safe_model_error(_HTTPError("Error code: 400" + LEAKY_TAIL, 400, body))
        self.assertEqual(str(error), TOOL_SCHEMA_REJECTED_MESSAGE)
        self.assertEqual(error.diagnostics, {"code": "tool_schema_rejected", "http_status": 400, "retryable": False})
        _assert_clean(self, error)

    def test_each_marker_matches_case_insensitively_in_message_body_or_cause(self):
        markers = ("FunctionDeclaration", "function_declarations[3]", "ANY_OF", "Parameters Schema",
                   'Invalid JSON payload received. Unknown name "oneOf"', "Request contains an Invalid Argument.",
                   "INVALID_ARGUMENT")
        for marker in markers:
            for where in ("message", "body", "cause"):
                with self.subTest(marker=marker, where=where):
                    if where == "message":
                        raw = _HTTPError(f"status_code: 400, body: {marker}" + LEAKY_TAIL, 400)
                    elif where == "body":
                        raw = _HTTPError("Bad Request" + LEAKY_TAIL, 400, {"error": {"message": marker}})
                    else:
                        raw = _HTTPError("Bad Request" + LEAKY_TAIL, 400)
                        raw.__cause__ = ValueError(marker)
                    error = safe_model_error(raw)
                    self.assertEqual(str(error), TOOL_SCHEMA_REJECTED_MESSAGE)
                    self.assertEqual(error.diagnostics["code"], "tool_schema_rejected")
                    _assert_clean(self, error)

    def test_pydantic_ai_model_http_error_is_recognized(self):
        from pydantic_ai.exceptions import ModelHTTPError

        raw = ModelHTTPError(400, "gemini-4-argon", body={"error": {"message": (
            "'jina_pageshot' functionDeclaration 'parameters' schema specified other fields alongside any_of")}})
        error = safe_model_error(raw)
        self.assertEqual(str(error), TOOL_SCHEMA_REJECTED_MESSAGE)
        self.assertNotIn("gemini-4-argon", str(error))

    def test_plain_400_keeps_the_generic_hidden_message(self):
        raw = _HTTPError("Error code: 400 - context length exceeded" + LEAKY_TAIL, 400,
                         {"error": {"message": "This request exceeds the context window" + LEAKY_TAIL}})
        error = safe_model_error(raw)
        self.assertEqual(str(error), GENERIC)
        self.assertEqual(error.diagnostics, {"code": "http_400", "http_status": 400, "retryable": False})
        _assert_clean(self, error)

    def test_markers_on_other_statuses_do_not_change_the_message(self):
        for status, expected in ((401, "模型认证失败，请检查 API Key 或服务账号凭据。"), (500, GENERIC)):
            with self.subTest(status=status):
                error = safe_model_error(_HTTPError("functionDeclaration any_of" + LEAKY_TAIL, status))
                self.assertEqual(str(error), expected)
                self.assertEqual(error.diagnostics["code"], f"http_{status}")
                _assert_clean(self, error)

    def test_a_400_schema_rejection_is_not_retried(self):
        raw = _HTTPError("functionDeclaration any_of", 400)
        self.assertTrue(model_backend.schema_rejected(raw))
        self.assertNotIn(400, model_backend.RETRYABLE_STATUSES)

    def test_a_broken_str_does_not_mask_the_failure(self):
        class Broken(_HTTPError):
            def __str__(self):
                raise RuntimeError("boom")

        error = safe_model_error(Broken("x", 400, {"error": "any_of"}))
        self.assertEqual(str(error), TOOL_SCHEMA_REJECTED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
