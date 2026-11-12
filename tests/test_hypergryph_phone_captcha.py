"""Hypergryph SMS login under risk control: GeeTest challenge, retried body, paste parsing."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

import httpx
from loguru import logger

from otae_bot.hypergryph import captcha as captcha_module

ROOT = Path(__file__).resolve().parents[1]


def _load_client(package_name: str, plugin_dir: str, relative_path: str):
    """Load a plugin client without running the plugin entrypoint (no commands registered)."""
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT / plugin_dir)]
        sys.modules[package_name] = package
    name = f"{package_name}.{relative_path.removesuffix('.py').replace('/', '.')}"
    if "." in name.removeprefix(package_name + "."):
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / plugin_dir / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


endfield_client = _load_client("hg_captcha_endfield_for_test", "plugins/endfield", "account/client.py")
arknights_client = _load_client("hg_captcha_arknights_for_test", "plugins/arknights", "client.py")

PHONE = "13812345678"
GT, CHALLENGE = "c9c4facd1a6feeb80802222a1a0b6a5e", "1d8c0f9a2b3c4d5e6f7a8b9c0d1e2f3a"
RESULT = {
    "geetest_challenge": CHALLENGE + "k3",
    "geetest_validate": "5f0e3c2b1a09d8e7f6a5b4c3d2e1f0a9",
    "geetest_seccode": "5f0e3c2b1a09d8e7f6a5b4c3d2e1f0a9|jordan",
}
RAW_CAPTCHA = {"gt": GT, "challenge": CHALLENGE, "success": 1, "new_captcha": True}
# What as.hypergryph.com answers when risk control wants a GeeTest check first.
RISK_RESPONSE = {"status": 1, "msg": "需要人机验证", "data": {"captcha": RAW_CAPTCHA}}


class _Recorder:
    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


class _SendPhoneCodeCases:
    """Shared by both clients: they call the same passport endpoint."""

    client_module = None
    api_error = None
    plain_failure_text = ""

    def client(self, *responses: httpx.Response):
        self.recorder = _Recorder(*responses)
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.recorder))
        self.addAsyncCleanup(http.aclose)
        return self.make_client(http)

    def make_client(self, http):
        raise NotImplementedError

    async def test_risk_control_raises_captcha_required_with_the_geetest_fields(self):
        client = self.client(httpx.Response(200, json=RISK_RESPONSE))
        with self.assertRaises(self.client_module.HypergryphCaptchaRequired) as caught:
            await client.send_phone_code(PHONE)
        required = caught.exception
        self.assertIsInstance(required, self.api_error)
        self.assertEqual((required.gt, required.challenge, required.new_captcha), (GT, CHALLENGE, True))
        self.assertEqual(required.captcha, RAW_CAPTCHA)
        self.assertEqual(required.code, "1")
        self.assertNotIn(PHONE, str(required))
        self.assertNotIn(PHONE[3:7], str(required))

    async def test_the_first_request_body_is_unchanged(self):
        client = self.client(httpx.Response(200, json={"status": 0, "msg": "OK"}))
        self.assertIsNone(await client.send_phone_code(PHONE))
        self.assertEqual(self.recorder.bodies(), [{"phone": PHONE, "type": 1}])
        self.assertEqual(str(self.recorder.requests[0].url), "https://as.hypergryph.com/general/v1/send_phone_code")

    async def test_the_retry_carries_only_the_three_geetest_keys(self):
        client = self.client(httpx.Response(200, json={"status": 0, "msg": "OK"}))
        await client.send_phone_code(PHONE, captcha={**RESULT, "gt": GT, "extra": "dropped"})
        self.assertEqual(self.recorder.bodies(), [{"phone": PHONE, "type": 1, "captcha": RESULT}])

    async def test_an_incomplete_captcha_result_is_refused_before_any_request(self):
        client = self.client()
        with self.assertRaises(ValueError):
            await client.send_phone_code(PHONE, captcha={**RESULT, "geetest_seccode": " "})
        self.assertEqual(self.recorder.requests, [])

    async def test_other_failures_stay_ordinary_api_errors(self):
        cases = (
            {"status": 1, "msg": "发送过于频繁"},
            {"status": 1, "msg": "需要人机验证", "data": {"captcha": {"gt": GT}}},
            {"status": 2, "msg": "发送过于频繁"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                client = self.client(httpx.Response(200, json=payload))
                with self.assertRaises(self.api_error) as caught:
                    await client.send_phone_code(PHONE)
                self.assertNotIsInstance(caught.exception, self.client_module.HypergryphCaptchaRequired)
                self.assertEqual(caught.exception.code, str(payload["status"]))
                self.assertEqual(str(caught.exception), self.plain_failure_text.format(**payload))


class EndfieldSendPhoneCodeTests(_SendPhoneCodeCases, unittest.IsolatedAsyncioTestCase):
    client_module = endfield_client
    api_error = endfield_client.EndfieldAPIError
    # Same wording as before the captcha change: the passport's own message.
    plain_failure_text = "发送验证码失败（{status}）：{msg}"

    def make_client(self, http):
        return endfield_client.EndfieldOfficialClient(http)


class ArknightsSendPhoneCodeTests(_SendPhoneCodeCases, unittest.IsolatedAsyncioTestCase):
    client_module = arknights_client
    api_error = arknights_client.ArknightsAPIError
    # The Arknights client never echoes the server's text.
    plain_failure_text = "发送验证码失败（{status}）。"

    def make_client(self, http):
        return arknights_client.ArknightsClient(http)


class CaptchaChallengeTests(unittest.TestCase):
    def test_only_a_status_1_answer_with_gt_and_challenge_is_a_challenge(self):
        challenge = captcha_module.captcha_challenge(RISK_RESPONSE)
        self.assertEqual(challenge, captcha_module.CaptchaChallenge(GT, CHALLENGE, True, RAW_CAPTCHA))
        for payload in (
            {"status": 0, "data": {"captcha": RAW_CAPTCHA}},
            {"status": 1, "msg": "需要人机验证"},
            {"status": 1, "data": {"captcha": {"gt": GT}}},
            {"status": 1, "data": {"captcha": {"gt": GT, "challenge": "<script>"}}},
            {"status": 1, "data": "captcha"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(captcha_module.captcha_challenge(payload))

    def test_new_captcha_flag_is_read_from_bool_number_or_text(self):
        for raw, expected in ((True, True), (1, True), ("true", True), (False, False), (0, False), (None, False)):
            with self.subTest(raw=raw):
                payload = {"status": "1", "data": {"captcha": {**RAW_CAPTCHA, "new_captcha": raw}}}
                self.assertIs(captcha_module.captcha_challenge(payload).new_captcha, expected)


class GeetestPageTests(unittest.TestCase):
    def test_page_runs_the_official_widget_for_this_challenge(self):
        page = captcha_module.render_geetest_html(gt=GT, challenge=CHALLENGE, new_captcha=True)
        self.assertIn(f'"gt":"{GT}"', page)
        self.assertIn(f'"challenge":"{CHALLENGE}"', page)
        self.assertIn('"new_captcha":true', page)
        self.assertIn('src="https://static.geetest.com/static/js/gt.0.5.0.js"', page)
        self.assertIn('api_server: "api.geevisit.com"', page)
        self.assertIn("offline: false", page)
        self.assertIn("getValidate()", page)
        for key in captcha_module.CAPTCHA_RESULT_KEYS:
            self.assertIn(f"{key}: result.{key}", page)
        self.assertIn("一整行", page)
        self.assertNotIn("$", page)
        self.assertIn('"new_captcha":false', captcha_module.render_geetest_html(gt=GT, challenge=CHALLENGE, new_captcha=False))

    def test_unexpected_ids_are_never_written_into_the_page(self):
        for gt, challenge in (('"</script><script>alert(1)//', CHALLENGE), (GT, ""), (GT, "a b")):
            with self.subTest(gt=gt, challenge=challenge), self.assertRaises(ValueError):
                captcha_module.render_geetest_html(gt=gt, challenge=challenge, new_captcha=True)


class CaptchaPasteTests(unittest.TestCase):
    parse = staticmethod(captcha_module.parse_captcha_paste)

    def test_the_page_line_and_its_common_mangles_are_accepted(self):
        challenge, validate, seccode = RESULT.values()
        pastes = {
            "page line": json.dumps(RESULT, separators=(",", ":")),
            "spaced json with chatter": "结果如下：\n" + json.dumps(RESULT, indent=2) + "\n谢谢",
            "query string": f"geetest_challenge={challenge}&geetest_validate={validate}"
                            f"&geetest_seccode={seccode.replace('|', '%7C')}",
            "loose lines": f" geetest_challenge: {challenge}\ngeetest_validate：{validate}\n\tgeetest_seccode = {seccode} ",
            "smart quotes": f"{{“geetest_challenge”:“{challenge}”，“geetest_validate”:“{validate}”，"
                            f"“geetest_seccode”:“{seccode}”}}",
            "short keys": f"challenge={challenge} validate={validate} seccode={seccode}",
            "full-width": "ｇｅｅｔｅｓｔ＿ｃｈａｌｌｅｎｇｅ＝" + challenge
                          + f" geetest_validate={validate} geetest_seccode={seccode}",
        }
        for label, paste in pastes.items():
            with self.subTest(label):
                self.assertEqual(self.parse(paste), RESULT)

    def test_a_result_missing_keys_names_what_is_missing(self):
        with self.assertRaises(captcha_module.CaptchaPasteError) as caught:
            self.parse(json.dumps({"geetest_challenge": RESULT["geetest_challenge"]}))
        self.assertIn("geetest_validate", str(caught.exception))
        self.assertIn("geetest_seccode", str(caught.exception))
        self.assertNotIn("geetest_challenge、", str(caught.exception))

        with self.assertRaises(captcha_module.CaptchaPasteError) as caught:
            self.parse(json.dumps({**RESULT, "geetest_validate": ""}))
        self.assertIn("缺少 geetest_validate", str(caught.exception))

    def test_anything_else_is_refused_with_a_readable_message(self):
        for paste in ("", "   ", "好了", "验证完成了", "{}"):
            with self.subTest(paste=paste), self.assertRaises(captcha_module.CaptchaPasteError) as caught:
                self.parse(paste)
            self.assertIn("没有识别到验证结果", str(caught.exception))
        with self.assertRaises(captcha_module.CaptchaPasteError) as caught:
            self.parse(json.dumps({**RESULT, "geetest_seccode": "abc<img>"}))
        self.assertIn("无法识别的字符", str(caught.exception))
        self.assertTrue(issubclass(captcha_module.CaptchaPasteError, ValueError))


class CaptchaPageUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs: list[str] = []
        sink = logger.add(lambda message: self.logs.append(message.record["message"]), format="{message}")
        self.addCleanup(logger.remove, sink)

    async def upload(self, handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return await captcha_module.upload_captcha_page("<html>page</html>", http=http)

    async def test_the_page_goes_to_0x0_st_as_an_html_file(self):
        seen: list[httpx.Request] = []

        async def handler(request):
            seen.append(request)
            return httpx.Response(200, text="https://0x0.st/Hgcaptcha.html\n")

        self.assertEqual(await self.upload(handler), "https://0x0.st/Hgcaptcha.html")
        self.assertEqual([request.url.host for request in seen], ["0x0.st"])
        body = seen[0].read()
        self.assertIn(b'name="file"; filename="hypergryph-captcha.html"', body)
        self.assertIn(b"Content-Type: text/html", body)
        self.assertIn(b"<html>page</html>", body)
        self.assertIn(b'name="expires"', body)
        self.assertFalse(any("Hgcaptcha" in line for line in self.logs))

    async def test_litterbox_takes_over_when_0x0_st_refuses(self):
        seen: list[httpx.Request] = []

        async def handler(request):
            seen.append(request)
            if request.url.host == "0x0.st":
                return httpx.Response(503, text="maintenance")
            return httpx.Response(200, text="https://litter.catbox.moe/abc123.html")

        self.assertEqual(await self.upload(handler), "https://litter.catbox.moe/abc123.html")
        self.assertEqual([request.url.host for request in seen], ["0x0.st", "litterbox.catbox.moe"])
        body = seen[1].read()
        self.assertIn(b'name="fileToUpload"; filename="hypergryph-captcha.html"', body)
        self.assertIn(b'name="reqtype"', body)
        self.assertIn(b"fileupload", body)
        self.assertIn("[hg-captcha] page upload failed host=0x0.st reason=http_503", self.logs)

    async def test_no_https_link_from_any_host_is_an_upload_error(self):
        async def handler(request):
            if request.url.host == "0x0.st":
                raise httpx.ConnectError("offline")
            return httpx.Response(200, text="http://litter.catbox.moe/abc123.html")

        with self.assertRaises(captcha_module.CaptchaPageUploadError) as caught:
            await self.upload(handler)
        self.assertEqual(str(caught.exception), "0x0.st:ConnectError, litterbox:http_200")


if __name__ == "__main__":
    unittest.main()
