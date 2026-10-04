"""HTTP gateway protocol checked against adam91holt/grokbot-sdk c14347f.

The gateway accepts prompts asynchronously. The QQ receiver reads from its first
unique prompt; the standalone ask helper retains strict one-question matching.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import monotonic
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
from loguru import logger

from otae_bot.infrastructure.http.tls import shared_ssl_context

from .config import GatewayError, GrokConfig, GrokError
from .media import (
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_REPLY_BYTES,
    MAX_REPLY_FILES,
    REPLY_DOWNLOAD_TIMEOUT,
    Attachment,
    Reply,
    decode_data_url,
    download_url,
    filename,
    mime_type,
    remote_path,
)

PROTOCOL_ERROR = "网关响应数据结构异常，请管理员核实网关服务版本。"
AWAITING_USER = "当前任务正等待人工介入确认，请管理员前往 Grok Bot 客户端处理后再发起提问。"
CHUNK_BYTES = 1024 * 1024
# These POST RPCs only read state. Never retry a mutation on a lost response.
READ_ONLY_COMMANDS = frozenset({
    "health", "listAgents", "getAsyncTasks", "getSubagents", "promptAcceptanceStatus",
    "getAgentTranscriptTail", "readAttachmentChunk",
})
# Exponential backoff, 31 s in total: rides out a 20-30 s tailnet/DERP outage.
READ_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
# Writes repeated only when the host never answered. Each upload has its own file
# name, so a request the host did receive just stores the same file again.
UNANSWERED_RETRY_COMMANDS = frozenset({"uploadAttachment"})
UNANSWERED_RETRY_DELAYS = (1.0, 1.5, 2.0)
TRANSCRIPT_READ_TIMEOUT = 60
# Long tool transcripts and Bot creation can take longer than ordinary state RPCs.
READ_TIMEOUTS = {"getAgentTranscriptTail": TRANSCRIPT_READ_TIMEOUT, "createAgent": 60}
# A base64 image body takes longer to send than an ordinary RPC.
UPLOAD_TIMEOUT = httpx.Timeout(20, connect=10, write=60, read=30)
# The gateway drops idle keep-alive connections sooner than httpx's 5 s default.
KEEPALIVE_EXPIRY = 2.0
# Before asking whether a prompt lost in transit was recorded.
PROMPT_SETTLE = 2.0
UPLOAD_FAILED = "图片上传失败，问题尚未发送，请重发。"


def lost_in_transit(error: BaseException) -> bool:
    """A timeout or broken connection: the host may or may not have the request."""
    return (isinstance(error, GatewayError) and error.retryable
            and isinstance(error.__cause__, httpx.TransportError))


def unanswered(error: GatewayError) -> bool:
    """Refused, or closed before any response header arrived."""
    cause = error.__cause__
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return True
    return isinstance(cause, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)) and error.responded is False


def cause_chain(error: BaseException) -> list[BaseException]:
    links, link = [], error.__cause__
    while link is not None and all(link is not seen for seen in links):
        links.append(link)
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    return links


def describe(error: BaseException) -> str:
    kind = type(error)
    name = kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"
    return f"{name}: {error}" if str(error) else name


@dataclass
class DownloadBudget:
    remaining: int = MAX_REPLY_BYTES
    seconds: float = REPLY_DOWNLOAD_TIMEOUT


def entries_from(payload: object) -> list[dict]:
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise GrokError(PROTOCOL_ERROR)
    result = []
    for row in entries:
        if isinstance(row, dict):
            entry = row.get("entry") if "kind" not in row and "entry" in row else row
            if isinstance(entry, dict):
                result.append(entry)
    return result


def attachments_from(message: dict) -> list[Attachment]:
    """Only explicit outgoing media; never turn tool output into QQ files."""
    records = []
    images = message.get("images")
    if isinstance(images, list):
        records.extend((item, True) for item in images if isinstance(item, dict))
    if message.get("type") in {"attachment", "file", "image"}:
        nested = message.get("attachment")
        records.append(({**message, **nested} if isinstance(nested, dict) else message, message.get("type") == "image"))
    result = []
    for row, is_image in records:
        mime = mime_type(row.get("mime", row.get("mimeType")))
        is_image = is_image or mime.startswith("image/")
        paths = row.get("attachmentPaths")
        paths = paths if isinstance(paths, list) else [row.get("file_path") or row.get("filePath") or row.get("path") or row.get("url") or ""]
        names = row.get("attachmentNames")
        names = names if isinstance(names, list) else []
        for index, source in enumerate(paths):
            if not isinstance(source, str):
                continue
            name = names[index] if index < len(names) else row.get("fileName", row.get("file_name", row.get("alt", "")))
            name = name if isinstance(name, str) else ""
            if not name and source and not source.startswith("data:"):
                try:
                    name = unquote(urlsplit(source).path).rsplit("/", 1)[-1]
                except ValueError:
                    pass
            result.append(Attachment(filename(name, "image.png" if is_image else "attachment.bin"), source, mime, is_image))
    return result


def reply_from(entries: list[dict], marker: str, *, include_assistant: bool = True, limit_attachments: bool = True,
               allow_intervening: bool = False) -> Reply | None:
    """An old answer or another user's prompt must never satisfy this request."""
    anchor = None
    for index, row in enumerate(entries):
        if row.get("role") == "user" and marker in str(row.get("content", "")):
            anchor = index
    if anchor is None:
        return None
    outgoing, assistant, attachments = [], [], []
    for row in entries[anchor + 1:]:
        if row.get("role") == "user" and not allow_intervening:
            raise GrokError("会话中插入了其他提问，无法准确匹配本次生成结果，请在 Grok Bot 客户端查阅。")
        if row.get("streaming") is True:
            continue
        message = row.get("message")
        if row.get("kind") == "send-message" and isinstance(message, dict):
            if message.get("type") == "text":
                value = message.get("content")
                if isinstance(value, str) and value.strip():
                    outgoing.append(value.strip())
            for item in attachments_from(message):
                if item not in attachments:
                    attachments.append(item)
        elif row.get("kind") in (None, "message") and row.get("role") == "assistant":
            value = row.get("content")
            if isinstance(value, str) and value.strip():
                assistant.append(value.strip())
    text = "\n\n".join(outgoing) if outgoing else (assistant[-1] if include_assistant and assistant and not attachments else "")
    if limit_attachments and len(attachments) > MAX_REPLY_FILES:
        text += f"\n\n附件生成数量已达 {MAX_REPLY_FILES} 个上限，多余附件请前往 Grok Bot 客户端查阅。"
        attachments = attachments[:MAX_REPLY_FILES]
    return Reply(text, tuple(attachments)) if text or attachments else None


class Gateway:
    def __init__(self, config: GrokConfig, client: httpx.AsyncClient, *,
                 connect: Callable[[], httpx.AsyncClient] | None = None):
        self.config, self.client = config, client
        # Opens a one-off client, so a request after a broken connection never
        # picks another pooled keep-alive connection the host may have dropped.
        self.connect = connect
        self.reconnect = False

    async def request(self, command: str, body: dict | None = None, *, response_limit: int | None = None):
        read_only = command in READ_ONLY_COMMANDS
        delays = (READ_RETRY_DELAYS if read_only
                  else UNANSWERED_RETRY_DELAYS if command in UNANSWERED_RETRY_COMMANDS else ())
        attempts = 1 + len(delays)
        payload = b"" if command == "health" else json.dumps(
            body or {}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        broken = False
        for attempt in range(attempts):
            started, request_id = monotonic(), str(uuid4())
            # Another request may already have cleared self.reconnect; this retry still avoids the pool.
            fresh = (broken or self.reconnect) and self.connect is not None
            try:
                if fresh:
                    async with self.connect() as client:
                        data, received, answered_id = await self._request_once(
                            command, payload, request_id, client, response_limit=response_limit)
                    self.reconnect = False
                else:
                    data, received, answered_id = await self._request_once(
                        command, payload, request_id, self.client, response_limit=response_limit)
            except GatewayError as error:
                broken = isinstance(error.__cause__, httpx.TransportError)
                if broken:
                    self.reconnect = True
                retry = attempt + 1 < attempts and (error.retryable if read_only else unanswered(error))
                links = cause_chain(error)
                # GatewayError text is generated locally. The original exception is
                # logged as type, message and stack, without frame variables, response
                # bodies, headers, prompts or the token.
                logger.warning(
                    "[grok_bot] gateway command={} attempt={}/{} elapsed={:.2f}s sent={}B request_id={} connection={} "
                    "retry={} error={} cause={}{}",
                    command, attempt + 1, attempts, monotonic() - started, len(payload), request_id,
                    "new" if fresh else "pooled", retry, error,
                    self._redact(" <- ".join(map(describe, links)) or "-"),
                    "" if retry or not links else "\n" + self._redact("".join(traceback.format_exception(links[0]))),
                )
                # Keep __cause__ but drop its frames: entari's loguru renders frame
                # variables (diagnose=True), and the request line holds the token.
                for link in links:
                    link.__traceback__ = None
                if not retry:
                    raise
                # Cancellation / the caller's total task timeout also bounds retries.
                await asyncio.sleep(delays[attempt])
                continue
            # Status polls run every second; keep them out of the INFO log unless
            # they recovered from a failure. The id is the one the host answered
            # with, else the one sent.
            logger.log(
                "DEBUG" if read_only and not attempt else "INFO",
                "[grok_bot] gateway command={} attempt={}/{} elapsed={:.2f}s sent={}B received={}B request_id={} connection={} ok",
                command, attempt + 1, attempts, monotonic() - started, len(payload), received,
                answered_id or request_id, "new" if fresh else "pooled",
            )
            return data

    def _redact(self, text: str) -> str:
        return text.replace(self.config.token, "<redacted>") if self.config.token else text

    async def _request_once(self, command: str, payload: bytes, request_id: str, client: httpx.AsyncClient, *,
                            response_limit: int | None = None) -> tuple[object, int, str]:
        """The decoded JSON, the received byte count and the response's x-sand-request-id."""
        health = command == "health"
        headers = {"x-sand-request-id": request_id, "x-sand-slim-avatars": "1"}
        if not health:
            headers["Authorization"] = "Bearer " + self.config.token
            # Sent as encoded bytes, so the log can state the exact request size.
            headers["Content-Type"] = "application/json"
        # Only a streamed response shows whether headers arrived before a failure.
        responded = None if response_limit is None else False
        try:
            args = ("GET" if health else "POST", self.config.base_url + ("/health" if health else "/api/" + command))
            kwargs = {"headers": headers, "follow_redirects": False, **({} if health else {"content": payload})}
            if command == "uploadAttachment":
                kwargs["timeout"] = UPLOAD_TIMEOUT
            elif command in READ_TIMEOUTS:
                kwargs["timeout"] = httpx.Timeout(20, connect=10, read=READ_TIMEOUTS[command])
            if response_limit is None:
                response = await client.request(*args, **kwargs)
                answered_id = response.headers.get("x-sand-request-id", "")
            else:
                async with client.stream(*args, **kwargs) as streamed:
                    responded = True
                    answered_id = streamed.headers.get("x-sand-request-id", "")
                    content = bytearray()
                    async for chunk in streamed.aiter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > response_limit:
                            raise GrokError("附件接口下发数据量超出安全阈值，已中止数据接收。")
                        content.extend(chunk)
                    response = httpx.Response(streamed.status_code, content=bytes(content))
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as error:
            raise GatewayError(
                f"网关连接建立失败（{command} / {type(error).__name__}），提问尚未提交，请检查 Tailscale 网络及云端服务状态。",
                not_submitted=True, retryable=True, responded=False,
            ) from error
        except httpx.TimeoutException as error:
            raise GatewayError(
                f"网关接口请求超时（{command} / {type(error).__name__}），已提交的任务可能仍在云端处理。",
                retryable=True, responded=responded,
            ) from error
        except httpx.HTTPError as error:
            raise GatewayError(
                f"网关数据传输异常（{command} / {type(error).__name__}），请排查 Tailscale 连通性、网关与后台服务；已提交任务可能仍在云端处理。",
                retryable=isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)), responded=responded,
            ) from error
        if response.status_code in {401, 403}:
            raise GatewayError("网关鉴权未通过，请管理员检查 GROKBOT_GATEWAY_TOKEN 配置。", not_submitted=True)
        if response.status_code in {400, 422}:
            raise GatewayError(
                f"网关拒绝了当前请求（{command} / HTTP {response.status_code}），请核对网关版本、提交参数与 Bot 实例配额限制。",
                not_submitted=True,
            )
        if response.status_code == 404:
            raise GatewayError(f"网关接口 {command} 未找到（HTTP 404），请检查基础地址与网关服务版本。", not_submitted=True)
        if response.status_code == 429:
            raise GatewayError("网关请求速率达到限制，请稍候重试。", not_submitted=True, retryable=True)
        if not 200 <= response.status_code < 300:
            raise GatewayError(f"网关接口返回错误（{command} / HTTP {response.status_code}）。",
                               retryable=response.status_code in {408, 500, 502, 503, 504})
        try:
            data = response.json()
        except ValueError as error:
            raise GatewayError(PROTOCOL_ERROR) from error
        if isinstance(data, dict) and data.get("error"):
            raise GatewayError(f"网关未能正常执行指令 {command}，详细原因请在客户端查看。")
        return data, len(response.content), answered_id

    async def upload_images(self, images: tuple[Attachment, ...]) -> tuple[Attachment, ...]:
        """Desktop 0.30.0: uploadAttachment({agentId?, filename, bytesBase64})."""
        uploaded = []
        for item in images:
            if not item.data or len(item.data) > MAX_IMAGE_BYTES:
                raise GrokError("图片数据损坏或单张体积超限，提问尚未发出。")
            try:
                # Streamed, so a connection closed before any response is told apart.
                result = await self.request("uploadAttachment", {
                    "agentId": self.config.agent_id, "filename": f"{uuid4().hex}-{item.name}",
                    "bytesBase64": base64.b64encode(item.data).decode("ascii"),
                }, response_limit=65536)
            except GatewayError:
                # Logged by request(). No prompt references this image yet.
                raise GrokError(UPLOAD_FAILED) from None
            if not isinstance(result, dict) or not isinstance(result.get("path"), str):
                raise GrokError("图片上传未获取到有效资源标识，提问尚未发出，请检查网关服务版本。")
            uploaded.append(replace(item, source=remote_path(result["path"]), data=None))
        return tuple(uploaded)

    async def read_attachment(self, item: Attachment, limit: int) -> tuple[bytes, str]:
        if item.source.startswith("data:"):
            return decode_data_url(item.source, limit)
        if item.source.startswith(("https://", "http://")):
            return await download_url(item.source, limit=limit)
        path = remote_path(item.source)

        async def chunk_at(offset: int, length: int):
            result = await self.request("readAttachmentChunk", {
                "agentId": self.config.agent_id, "path": path, "offset": offset, "length": length,
            }, response_limit=2 * CHUNK_BYTES)
            if not isinstance(result, dict) or type(result.get("totalSize")) is not int or result["totalSize"] < 0:
                raise GrokError("云端附件数据不可用或协议不兼容，请在 Grok Bot 客户端查阅。")
            return result

        # A zero-length read gives the size before allocating/downloading bytes.
        head = await chunk_at(0, 0)
        size = head["totalSize"]
        if size > limit:
            raise GrokError("附件体积超过单文件 20 MB 或总计 50 MB 上限，未执行下载。")
        data = bytearray()
        mime = mime_type(head.get("mime"))
        while len(data) < size:
            length = min(CHUNK_BYTES, size - len(data))
            part = await chunk_at(len(data), length)
            encoded = part.get("bytesBase64")
            if part["totalSize"] != size or not isinstance(encoded, str) or len(encoded) > ((length + 2) // 3) * 4:
                raise GrokError("云端附件在下载过程中被修改或传输残缺，请重新生成后再次获取。")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise GrokError("云端附件编码解析异常，已中止转发残缺内容。") from None
            if not chunk or len(chunk) > length:
                raise GrokError("云端附件未能完整获取，请在 Grok Bot 客户端查阅。")
            data.extend(chunk)
            if mime == "application/octet-stream":
                mime = mime_type(part.get("mime"))
        return bytes(data), mime

    async def collect_attachment(self, item: Attachment, budget: DownloadBudget) -> Attachment:
        started = asyncio.get_running_loop().time()
        try:
            if budget.remaining <= 0:
                raise GrokError("本次附件传输累计已达 50 MB 上限，其余附件请在 Grok Bot 客户端查阅。")
            if budget.seconds <= 0:
                raise GrokError("本次附件传输超时，其余附件请在 Grok Bot 客户端查阅。")
            data, mime = await asyncio.wait_for(self.read_attachment(item, min(MAX_FILE_BYTES, budget.remaining)), budget.seconds)
            budget.remaining -= len(data)
            mime = mime_type(mime) if mime != "application/octet-stream" else item.mime
            image = mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
            image = image or (item.image and mime == "application/octet-stream")
            # QQ cannot display SVG/TIFF as pictures; preserve them as files.
            return replace(item, source="", data=data, mime=mime, image=image)
        except asyncio.TimeoutError:
            return replace(item, source="", error="附件拉取超时，请在 Grok Bot 客户端查阅。")
        except GrokError as error:
            return replace(item, source="", error=str(error))
        finally:
            # Count download time, excluding model waits and QQ uploads.
            budget.seconds -= asyncio.get_running_loop().time() - started

    async def collect_reply(self, reply: Reply) -> Reply:
        """Copy files while holding the conversation lock; retain partial success."""
        budget = DownloadBudget(remaining=MAX_REPLY_BYTES)
        result = [await self.collect_attachment(item, budget) for item in reply.attachments]
        return replace(reply, attachments=tuple(result))

    async def agent(self) -> dict:
        rows = await self.request("listAgents")
        if not isinstance(rows, list):
            raise GrokError(PROTOCOL_ERROR)
        for row in rows:
            if isinstance(row, dict) and row.get("id") == self.config.agent_id:
                if row.get("isGroup"):
                    raise GrokError("GROKBOT_AGENT_ID 必须配置为单个 Bot 实例，暂不支持指向群组 Bot。")
                if not all(isinstance(row.get(key), bool) for key in ("isRunning", "isComposingMessage")):
                    raise GrokError(PROTOCOL_ERROR)
                return row
        raise GrokError("未找到目标 Grok Bot 实例，请管理员检查 GROKBOT_AGENT_ID 配置。")

    async def state(self) -> tuple[dict, bool]:
        calls = [asyncio.create_task(call) for call in (
            self.agent(), self.request("getAsyncTasks", {"id": self.config.agent_id}),
            self.request("getSubagents", {"id": self.config.agent_id}),
        )]
        try:
            row, tasks, subagents = await asyncio.gather(*calls)
        finally:
            # gather does not cancel siblings when one RPC fails. Join them before
            # closing the HTTP client, including any sibling retry backoff.
            for call in calls:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
        if not isinstance(tasks, list) or not isinstance(subagents, list) or any(not isinstance(item, dict) for item in subagents):
            raise GrokError(PROTOCOL_ERROR)
        waiting = row.get("awaitingUserResponse")
        if waiting is not None and waiting is not False:
            raise GrokError(AWAITING_USER)
        busy = bool(row["isRunning"] or row["isComposingMessage"] or tasks or any(item.get("status") == "running" for item in subagents))
        return row, busy

    async def transcript_entries(self, marker: str) -> list[dict]:
        rows, before = [], None
        # Limit memory and RPCs while still finding the prompt behind tool events.
        for _ in range(10):
            body = {"id": self.config.agent_id, "limit": 200}
            if before is not None:
                body["beforeSeq"] = before
            payload = await self.request("getAgentTranscriptTail", body)
            rows = entries_from(payload) + rows
            if any(row.get("role") == "user" and marker in str(row.get("content", "")) for row in rows):
                return rows
            cursor = payload.get("nextBeforeSeq") if isinstance(payload, dict) else None
            if not isinstance(cursor, int) or cursor == before:
                return []
            before = cursor
        raise GrokError("对话上下文记录超出检索深度，无法准确关联本次回答，请在 Grok Bot 客户端查阅。")

    async def transcript(self, marker: str) -> Reply | None:
        return reply_from(await self.transcript_entries(marker), marker)

    async def submit(self, prompt: str, nonce: str, attachments: tuple[Attachment, ...] = ()) -> None:
        content = f"[QQ请求编号:{nonce}]\n{prompt}\n\n请直接回答本次问题，回复中无需包含请求编号。"
        body = {"agentId": self.config.agent_id, "prompt": content, "clientNonce": nonce}
        if attachments:
            body["attachmentPaths"] = [item.source for item in attachments]
            body["attachmentNames"] = [item.name for item in attachments]
        if not await self.send_prompt(body):
            raise GrokError("云端未确认接收本次提问，请在 Grok Bot 客户端核对状态；本次未重复提交。")

    async def send_prompt(self, body: dict) -> bool:
        """Resend a lost prompt, with the same clientNonce, only if the host never recorded it."""
        nonce, resent = body["clientNonce"], False
        while True:
            try:
                accepted = await self.request("sendPrompt", body)
                return isinstance(accepted, dict) and accepted.get("accepted") is True
            except GatewayError as error:
                if not lost_in_transit(error) or (resent and error.not_submitted):
                    raise
                cause = type(error.__cause__).__name__
                # TCP may still deliver a timed-out request once the link returns, so
                # "not-found" proves nothing then. A refused or closed connection cannot.
                resendable = not resent and (error.not_submitted or not isinstance(error.__cause__, httpx.TimeoutException))
                if not error.not_submitted:
                    await asyncio.sleep(PROMPT_SETTLE)
                try:
                    # Read-only, so retried with backoff; a failed lookup never resends.
                    outcome = await self.acceptance_outcome(nonce)
                except GrokError as lookup:
                    logger.warning("[grok_bot] sendPrompt nonce={} lost ({}); acceptance lookup failed ({}), not resending",
                                   nonce, cause, type(lookup).__name__)
                    raise error
                resend = resendable and outcome == "not-found"
                logger.warning("[grok_bot] sendPrompt nonce={} lost ({}); acceptance={} resend={}", nonce, cause, outcome, resend)
                if outcome not in {"not-found", "unknown-durability"}:
                    return True  # Recorded; the receiver follows its acceptance status.
                if not resend:
                    raise
                resent = True

    async def acceptance(self, nonce: str) -> str:
        status = await self.acceptance_outcome(nonce)
        # Absence may only mean the record is not written yet; never a rejection.
        return "pending" if status == "not-found" else status

    async def acceptance_outcome(self, nonce: str) -> str:
        """The recorded status, or "not-found" / "unknown-durability"."""
        payload = await self.request("promptAcceptanceStatus", {"accountSlot": "host", "clientNonce": nonce})
        if not isinstance(payload, dict):
            raise GrokError(PROTOCOL_ERROR)
        outcome = payload.get("outcome")
        if outcome == "found":
            record = payload.get("record")
            if not isinstance(record, dict) or record.get("clientNonce") != nonce or record.get("agentId") != self.config.agent_id:
                raise GrokError(PROTOCOL_ERROR)
            if record.get("status") not in {"pending", "accepted", "rejected"}:
                raise GrokError(PROTOCOL_ERROR)
            return record["status"]
        if outcome in {"not-found", "unknown-durability"}:
            return outcome
        raise GrokError(PROTOCOL_ERROR)

    async def ask(self, prompt: str, attachments: tuple[Attachment, ...] = (), *,
                  on_reply: Callable[[Reply], Awaitable[None]] | None = None) -> Reply:
        # Wait for app-initiated or previously timed-out work before adding input.
        while True:
            _, busy = await self.state()
            if not busy:
                break
            await asyncio.sleep(self.config.poll_interval)
        nonce = str(uuid4())
        marker = f"[QQ请求编号:{nonce}]"
        content = f"{marker}\n{prompt}\n\n请直接回答本次问题，回复中无需包含请求编号。"
        body = {"agentId": self.config.agent_id, "prompt": content, "clientNonce": nonce}
        if attachments:
            body["attachmentPaths"] = [item.source for item in attachments]
            body["attachmentNames"] = [item.name for item in attachments]
        if not await self.send_prompt(body):
            raise GrokError("云端未能接收本次提问，请在 Grok Bot 客户端核实当前状态。")
        previous_reply = None
        while True:
            acceptance = await self.request("promptAcceptanceStatus", {"accountSlot": "host", "clientNonce": nonce})
            if not isinstance(acceptance, dict):
                raise GrokError(PROTOCOL_ERROR)
            outcome, pending = acceptance.get("outcome"), False
            if outcome == "found":
                record = acceptance.get("record")
                if not isinstance(record, dict) or record.get("clientNonce") != nonce or record.get("agentId") != self.config.agent_id:
                    raise GrokError(PROTOCOL_ERROR)
                status = record.get("status")
                if status == "rejected":
                    raise GrokError("Grok Bot 拒绝执行本次生成任务，具体原因请在客户端查阅。")
                if status not in {"pending", "accepted"}:
                    raise GrokError(PROTOCOL_ERROR)
                pending = status == "pending"
            elif outcome == "not-found":
                pending = True
            elif outcome != "unknown-durability":
                raise GrokError(PROTOCOL_ERROR)
            entries = None
            if on_reply is not None and not pending:
                entries = await self.transcript_entries(marker)
                outgoing = reply_from(entries, marker, include_assistant=False, limit_attachments=False)
                if outgoing:
                    # A completed send-message is already visible in the app.
                    # Forward it before waiting on background/subagent state.
                    await on_reply(outgoing)
            _, busy = await self.state()
            if busy or pending:
                previous_reply = None
            else:
                reply = (reply_from(entries, marker, limit_attachments=False)
                         if entries is not None else await self.transcript(marker))
                # Two idle snapshots avoid returning a transient intermediate text.
                if reply and reply == previous_reply:
                    return reply
                previous_reply = reply
            await asyncio.sleep(self.config.poll_interval)


def make_client() -> httpx.AsyncClient:
    # Tailnet requests must bypass global model/search/system proxies.
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(20, connect=10), follow_redirects=False,
                             limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=KEEPALIVE_EXPIRY),
                             verify=shared_ssl_context(trust_env=False))


async def ask(config: GrokConfig, prompt: str) -> Reply:
    async with make_client() as client:
        try:
            return await asyncio.wait_for(Gateway(config, client, connect=make_client).ask(prompt), timeout=config.timeout)
        except asyncio.TimeoutError:
            raise GrokError(f"请求等待已超时（超过 {config.timeout:g} 秒），任务可能仍在云端处理，请在 Grok Bot 客户端查阅。") from None


async def check(config: GrokConfig) -> str:
    async with make_client() as client:
        gateway = Gateway(config, client, connect=make_client)
        health = await gateway.request("health")
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise GrokError("Grok Bot 网关运行状态检测异常。")
        config.persona()
        if not config.agent_id:
            rows = await gateway.request("listAgents")
            if not isinstance(rows, list):
                raise GrokError(PROTOCOL_ERROR)
            return "网关服务、Token 与本地人设配置检测正常，各会话首次提问将自动建立独立 Bot 实例（本检测未创建新实例）。"
        _, busy = await gateway.state()
        return "网关连通性、Token 认证、人设模板及基准 Bot 状态校验通过，" + ("基准 Bot 当前忙碌。" if busy else "基准 Bot 当前空闲。") + "实际提问将分派至各会话独立的专属 Bot。"
