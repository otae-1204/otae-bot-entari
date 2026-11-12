"""GeeTest (v3) check for Hypergryph passport SMS login, shared by ``/ef`` and ``/ak`` binding.

When ``/general/v1/send_phone_code`` is risk-controlled, the passport answers
``status=1`` (``msg=需要人机验证``) with ``data.captcha = {gt, challenge,
success, new_captcha}``.  The bot renders a page that runs the official GeeTest
widget for that challenge, uploads it to a temporary file host and sends the
link; the user solves the slider in a phone browser and pastes the one-line
result back.  The client then repeats the request with ``captcha =
{geetest_challenge, geetest_validate, geetest_seccode}``.

A person always solves the check: no solving service, no headless browser.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from string import Template
from typing import Any
from urllib.parse import unquote

import httpx
from loguru import logger

from otae_bot.infrastructure.http.tls import ashared_ssl_context

CAPTCHA_RESULT_KEYS = ("geetest_challenge", "geetest_validate", "geetest_seccode")
GEETEST_SCRIPT_URL = "https://static.geetest.com/static/js/gt.0.5.0.js"
GEETEST_API_SERVER = "api.geevisit.com"

# 第一次风控后再给一轮：第二轮还要验证就停下，不再连续发短信请求。
CAPTCHA_ROUNDS = 2
CAPTCHA_TIMEOUT_SECONDS = 180
CAPTCHA_GUIDE = (
    "鹰角触发了人机验证。请用手机浏览器打开下面的链接，完成滑动验证后，"
    "把页面显示的一整行结果复制发回本对话（勿发给他人）。"
)
CAPTCHA_GIVE_UP = "鹰角仍然要求人机验证，短信暂未发出。请稍后再试，或改用 Token 绑定。"
CAPTCHA_UPLOAD_FAILED = "鹰角触发了人机验证，但验证页面暂时无法上传，短信未发出。请稍后再试，或改用 Token 绑定。"

_ID = re.compile(r"[0-9A-Za-z_-]{1,128}")
_RESULT_VALUE = re.compile(r"[0-9A-Za-z|_.-]{1,512}")
_QUOTE = "\"'“”‘’"
_RESULT_PAIR = re.compile(
    rf"(?<![0-9A-Za-z_])(?:geetest_)?(?P<key>challenge|validate|seccode)[{_QUOTE}]?"
    rf"\s*[:=]\s*[{_QUOTE}]?(?P<value>[^\s{_QUOTE}&,;{{}}]+)",
    re.IGNORECASE,
)
_HTTPS_LINK = re.compile(r"https://[^\s\"'<>]{1,300}")

CAPTCHA_PAGE_FILENAME = "hypergryph-captcha.html"
_UPLOAD_TIMEOUT_SECONDS = 20.0
_UPLOAD_USER_AGENT = "otae-bot/1.0 (+captcha page upload)"


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    gt: str
    challenge: str
    new_captcha: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PageHost:
    name: str
    url: str
    file_field: str
    fields: tuple[tuple[str, str], ...]


# 0x0.st 优先；catbox 的临时盘 litterbox 兜底。两家都只留 1 小时。
_PAGE_HOSTS = (
    _PageHost("0x0.st", "https://0x0.st", "file", (("expires", "1"), ("secret", ""))),
    _PageHost(
        "litterbox",
        "https://litterbox.catbox.moe/resources/internals/api.php",
        "fileToUpload",
        (("reqtype", "fileupload"), ("time", "1h")),
    ),
)


class CaptchaPasteError(ValueError):
    """The pasted text is not a complete GeeTest result; the message is shown to the user."""


class CaptchaPageUploadError(RuntimeError):
    """No temporary file host accepted the captcha page."""


def captcha_challenge(payload: Mapping[str, Any]) -> CaptchaChallenge | None:
    """The GeeTest challenge in a ``status=1`` passport answer, or None for any other answer."""
    if str(payload.get("status")) != "1":
        return None
    data = payload.get("data")
    captcha = data.get("captcha") if isinstance(data, Mapping) else None
    if not isinstance(captcha, Mapping):
        return None
    gt = str(captcha.get("gt") or "").strip()
    challenge = str(captcha.get("challenge") or "").strip()
    if not _ID.fullmatch(gt) or not _ID.fullmatch(challenge):
        return None
    new_captcha = captcha.get("new_captcha")
    return CaptchaChallenge(
        gt=gt,
        challenge=challenge,
        new_captcha=new_captcha is True or str(new_captcha).strip().casefold() in {"1", "true"},
        raw=dict(captcha),
    )


def captcha_result_body(captcha: Mapping[str, str]) -> dict[str, str]:
    """The ``captcha`` field of the retried request: exactly the three GeeTest result keys."""
    body = {key: str(captcha.get(key) or "").strip() for key in CAPTCHA_RESULT_KEYS}
    missing = [key for key, value in body.items() if not value]
    if missing:
        raise ValueError(f"captcha result is missing {', '.join(missing)}")
    return body


def render_geetest_html(*, gt: str, challenge: str, new_captcha: bool) -> str:
    """A standalone page that runs the GeeTest widget and shows its result as one JSON line."""
    if not _ID.fullmatch(gt) or not _ID.fullmatch(challenge):
        raise ValueError("unexpected GeeTest gt/challenge format")
    config = json.dumps(
        {"gt": gt, "challenge": challenge, "new_captcha": bool(new_captcha)},
        separators=(",", ":"),
    )
    config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _PAGE.substitute(
        config=config, script_url=GEETEST_SCRIPT_URL, api_server=GEETEST_API_SERVER
    )


def parse_captcha_paste(text: str) -> dict[str, str]:
    """Read the three GeeTest result values from a pasted JSON line, query string or loose text."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    found: dict[str, str] = {}
    for match in _RESULT_PAIR.finditer(normalized):
        key = f"geetest_{match.group('key').casefold()}"
        found.setdefault(key, unquote(match.group("value")).strip())
    if not found:
        raise CaptchaPasteError(
            "没有识别到验证结果。请在验证页面完成滑动后，复制页面显示的那一整行"
            "（以 {\"geetest_challenge\" 开头）原样发回。"
        )
    missing = [key for key in CAPTCHA_RESULT_KEYS if not found.get(key)]
    if missing:
        raise CaptchaPasteError(f"验证结果不完整，缺少 {'、'.join(missing)}。请复制页面显示的一整行结果。")
    result = {key: found[key] for key in CAPTCHA_RESULT_KEYS}
    if not all(_RESULT_VALUE.fullmatch(value) for value in result.values()):
        raise CaptchaPasteError("验证结果里有无法识别的字符，请直接复制页面显示的一整行结果。")
    return result


async def upload_captcha_page(html: str, *, http: httpx.AsyncClient | None = None) -> str:
    """Upload the page to a temporary file host and return its https link."""
    if http is not None:
        return await _upload(http, html.encode("utf-8"))
    async with httpx.AsyncClient(
        timeout=_UPLOAD_TIMEOUT_SECONDS,
        trust_env=False,
        verify=await ashared_ssl_context(trust_env=False),
    ) as client:
        return await _upload(client, html.encode("utf-8"))


async def _upload(http: httpx.AsyncClient, page: bytes) -> str:
    failures: list[str] = []
    for host in _PAGE_HOSTS:
        try:
            response = await http.post(
                host.url,
                data=dict(host.fields),
                files={host.file_field: (CAPTCHA_PAGE_FILENAME, page, "text/html")},
                headers={"User-Agent": _UPLOAD_USER_AGENT},
            )
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
        else:
            link = response.text.strip()
            if response.status_code == 200 and _HTTPS_LINK.fullmatch(link):
                # The link is the user's to open; it is never logged.
                logger.info(f"[hg-captcha] page uploaded host={host.name}")
                return link
            reason = f"http_{response.status_code}"
        logger.warning(f"[hg-captcha] page upload failed host={host.name} reason={reason}")
        failures.append(f"{host.name}:{reason}")
    raise CaptchaPageUploadError(", ".join(failures))


_PAGE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>鹰角账号人机验证</title>
<style>
body { margin: 0; padding: 20px 16px 32px; background: #f4f5f7; color: #1f2328;
  font: 16px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 420px; margin: 0 auto; }
h1 { margin: 0 0 8px; font-size: 20px; }
ol { margin: 0 0 16px; padding-left: 1.3em; }
#captcha { min-height: 48px; margin: 16px 0; }
#status { color: #57606a; }
#status.error { color: #cf222e; }
textarea { box-sizing: border-box; width: 100%; padding: 8px; border: 1px solid #d0d7de;
  border-radius: 6px; resize: none; word-break: break-all;
  font: 13px/1.5 ui-monospace, Menlo, Consolas, monospace; }
button { width: 100%; margin-top: 8px; padding: 10px; border: 0; border-radius: 6px;
  background: #1f2328; color: #fff; font-size: 16px; }
.note { color: #57606a; font-size: 13px; }
</style>
</head>
<body>
<main>
<h1>鹰角账号人机验证</h1>
<ol>
<li>点击下方按钮，按提示完成滑动验证。</li>
<li>验证通过后，这里会出现一整行结果，点「复制结果」。</li>
<li>回到 QQ，把这一整行原样发回给机器人（勿发给他人）。</li>
</ol>
<div id="captcha"></div>
<p id="status">正在加载验证组件……</p>
<section id="done" hidden>
<p>验证完成，请复制下面这一整行，发回机器人：</p>
<textarea id="result" rows="4" readonly></textarea>
<button id="copy" type="button">复制结果</button>
</section>
<p class="note">本页不包含你的手机号或账号信息，只用于完成这一次验证。机器人只等待 3 分钟，超时请重新发起绑定。</p>
</main>
<script>
var CONFIG = $config;
function setStatus(text, isError) {
  var status = document.getElementById("status");
  status.textContent = text;
  status.className = isError ? "error" : "";
}
function failed() {
  setStatus("验证组件加载失败。请检查网络后刷新本页；仍然不行请回 QQ 回复“取消”，改用 Token 绑定。", true);
}
function showResult(result) {
  var box = document.getElementById("result");
  box.value = JSON.stringify({
    geetest_challenge: result.geetest_challenge,
    geetest_validate: result.geetest_validate,
    geetest_seccode: result.geetest_seccode
  });
  document.getElementById("done").hidden = false;
  setStatus("验证已通过。", false);
  box.focus();
  box.select();
}
function copyResult() {
  var box = document.getElementById("result");
  function copied() { setStatus("已复制，请回到 QQ 粘贴发送。", false); }
  function fallback() {
    box.focus();
    box.select();
    try {
      if (document.execCommand("copy")) { copied(); return; }
    } catch (error) {}
    setStatus("无法自动复制，请长按上面的文字全选后复制。", true);
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(box.value).then(copied, fallback);
  } else {
    fallback();
  }
}
function start() {
  if (typeof window.initGeetest !== "function") { failed(); return; }
  window.initGeetest({
    gt: CONFIG.gt,
    challenge: CONFIG.challenge,
    offline: false,
    new_captcha: CONFIG.new_captcha,
    api_server: "$api_server",
    product: "float",
    width: "100%"
  }, function (captchaObj) {
    captchaObj.appendTo("#captcha");
    captchaObj.onReady(function () { setStatus("请点击上方按钮完成验证。", false); });
    captchaObj.onSuccess(function () {
      var result = captchaObj.getValidate();
      if (!result) { setStatus("没有拿到验证结果，请重新验证。", true); return; }
      showResult(result);
    });
    captchaObj.onError(function () {
      setStatus("验证出错或已过期。请回 QQ 回复“取消”，再重新发起绑定。", true);
    });
  });
}
document.getElementById("copy").addEventListener("click", copyResult);
</script>
<script src="$script_url" onload="start()" onerror="failed()"></script>
</body>
</html>
""")
