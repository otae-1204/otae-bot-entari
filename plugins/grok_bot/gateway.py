"""HTTP gateway protocol checked against adam91holt/grokbot-sdk c14347f.

The gateway accepts prompts asynchronously. Read only replies anchored to our
unique prompt, and keep the caller's per-agent lock until polling has finished.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx

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

PROTOCOL_ERROR = "Grok Bot 网关返回了无法识别的数据，请管理员检查网关版本。"
AWAITING_USER = "Grok Bot 正在等待人工确认，请管理员打开 Grok Bot 应用处理后再提问。"
CHUNK_BYTES = 1024 * 1024


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


def reply_from(entries: list[dict], marker: str, *, include_assistant: bool = True, limit_attachments: bool = True) -> Reply | None:
    """An old answer or another user's prompt must never satisfy this request."""
    anchor = None
    for index, row in enumerate(entries):
        if row.get("role") == "user" and marker in str(row.get("content", "")):
            anchor = index
    if anchor is None:
        return None
    outgoing, assistant, attachments = [], [], []
    for row in entries[anchor + 1:]:
        if row.get("role") == "user":
            raise GrokError("Grok Bot 会话中出现了另一条输入，无法可靠对应本次回答。请在应用中查看结果。")
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
        text += f"\n\n本次附件超过 {MAX_REPLY_FILES} 个，其余附件请在 Grok Bot 应用中查看。"
        attachments = attachments[:MAX_REPLY_FILES]
    return Reply(text, tuple(attachments)) if text or attachments else None


class Gateway:
    def __init__(self, config: GrokConfig, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def request(self, command: str, body: dict | None = None, *, response_limit: int | None = None):
        health = command == "health"
        headers = {"x-sand-request-id": str(uuid4()), "x-sand-slim-avatars": "1"}
        if not health:
            headers["Authorization"] = "Bearer " + self.config.token
        try:
            args = ("GET" if health else "POST", self.config.base_url + ("/health" if health else "/api/" + command))
            kwargs = {"headers": headers, "follow_redirects": False, **({} if health else {"json": body or {}})}
            if response_limit is None:
                response = await self.client.request(*args, **kwargs)
            else:
                async with self.client.stream(*args, **kwargs) as streamed:
                    content = bytearray()
                    async for chunk in streamed.aiter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > response_limit:
                            raise GrokError("Grok Bot 附件接口响应过大，已停止读取。")
                        content.extend(chunk)
                    response = httpx.Response(streamed.status_code, content=bytes(content))
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
            raise GatewayError(f"连接 Grok Bot 网关失败（{command}），请求尚未提交，请检查 Tailscale 和云端服务。", not_submitted=True) from None
        except httpx.TimeoutException:
            raise GatewayError(f"读取 Grok Bot 网关超时（{command}），已提交的任务可能仍在运行，请检查 Tailscale 和云端服务。") from None
        except httpx.HTTPError:
            raise GatewayError(f"Grok Bot 网关通信失败（{command}），请检查两端 Tailscale、网关地址及云端后台服务。") from None
        if response.status_code in {401, 403}:
            raise GatewayError("Grok Bot 网关认证失败，请管理员检查 GROKBOT_GATEWAY_TOKEN。", not_submitted=True)
        if response.status_code in {400, 422}:
            raise GatewayError(
                f"Grok Bot 网关拒绝请求（{command} / HTTP {response.status_code}）。请检查网关版本、请求参数和 Bot 数量限制。",
                not_submitted=True,
            )
        if response.status_code == 404:
            raise GatewayError(f"Grok Bot 网关接口 {command} 返回 404，请检查基址和网关版本。", not_submitted=True)
        if response.status_code == 429:
            raise GatewayError("Grok Bot 网关请求过于频繁，请稍后重试。", not_submitted=True)
        if not 200 <= response.status_code < 300:
            raise GatewayError(f"Grok Bot 网关请求失败（{command} / HTTP {response.status_code}）。")
        try:
            data = response.json()
        except ValueError:
            raise GatewayError(PROTOCOL_ERROR) from None
        if isinstance(data, dict) and data.get("error"):
            raise GatewayError(f"Grok Bot 网关未能完成 {command}，请在应用中查看详情。")
        return data

    async def upload_images(self, images: tuple[Attachment, ...]) -> tuple[Attachment, ...]:
        """Desktop 0.30.0: uploadAttachment({agentId?, filename, bytesBase64})."""
        uploaded = []
        for item in images:
            if not item.data or len(item.data) > MAX_IMAGE_BYTES:
                raise GrokError("图片内容无效或超过大小限制，问题尚未发送。")
            result = await self.request("uploadAttachment", {
                "agentId": self.config.agent_id, "filename": f"{uuid4().hex}-{item.name}",
                "bytesBase64": base64.b64encode(item.data).decode("ascii"),
            }, response_limit=65536)
            if not isinstance(result, dict) or not isinstance(result.get("path"), str):
                raise GrokError("Grok Bot 图片上传未返回有效路径，问题尚未发送，请检查网关版本。")
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
                raise GrokError("云端附件不可用或返回格式不兼容，请在 Grok Bot 应用中查看。")
            return result

        # A zero-length read gives the size before allocating/downloading bytes.
        head = await chunk_at(0, 0)
        size = head["totalSize"]
        if size > limit:
            raise GrokError("附件超过单个 20 MB 或本次合计 50 MB 的限制，未下载。")
        data = bytearray()
        mime = mime_type(head.get("mime"))
        while len(data) < size:
            length = min(CHUNK_BYTES, size - len(data))
            part = await chunk_at(len(data), length)
            encoded = part.get("bytesBase64")
            if part["totalSize"] != size or not isinstance(encoded, str) or len(encoded) > ((length + 2) // 3) * 4:
                raise GrokError("云端附件在下载时发生变化或返回不完整，请重新生成后再试。")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise GrokError("云端附件编码无效，未转发不完整文件。") from None
            if not chunk or len(chunk) > length:
                raise GrokError("云端附件下载不完整，请在 Grok Bot 应用中查看。")
            data.extend(chunk)
            if mime == "application/octet-stream":
                mime = mime_type(part.get("mime"))
        return bytes(data), mime

    async def collect_attachment(self, item: Attachment, budget: DownloadBudget) -> Attachment:
        started = asyncio.get_running_loop().time()
        try:
            if budget.remaining <= 0:
                raise GrokError("本次附件合计已达 50 MB，其余附件请在 Grok Bot 应用中查看。")
            if budget.seconds <= 0:
                raise GrokError("本次附件下载超时，其余附件请在 Grok Bot 应用中查看。")
            data, mime = await asyncio.wait_for(self.read_attachment(item, min(MAX_FILE_BYTES, budget.remaining)), budget.seconds)
            budget.remaining -= len(data)
            mime = mime_type(mime) if mime != "application/octet-stream" else item.mime
            image = mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
            image = image or (item.image and mime == "application/octet-stream")
            # QQ cannot display SVG/TIFF as pictures; preserve them as files.
            return replace(item, source="", data=data, mime=mime, image=image)
        except asyncio.TimeoutError:
            return replace(item, source="", error="附件下载超时，请在 Grok Bot 应用中查看。")
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
                    raise GrokError("GROKBOT_AGENT_ID 应指向单个 Bot，当前不支持 Grok Bot 群聊。")
                if not all(isinstance(row.get(key), bool) for key in ("isRunning", "isComposingMessage")):
                    raise GrokError(PROTOCOL_ERROR)
                return row
        raise GrokError("找不到配置的 Grok Bot，请管理员检查 GROKBOT_AGENT_ID。")

    async def state(self) -> tuple[dict, bool]:
        row, tasks, subagents = await asyncio.gather(
            self.agent(), self.request("getAsyncTasks", {"id": self.config.agent_id}),
            self.request("getSubagents", {"id": self.config.agent_id}),
        )
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
        raise GrokError("Grok Bot 任务记录过长，无法可靠定位本次回答，请在应用中查看结果。")

    async def transcript(self, marker: str) -> Reply | None:
        return reply_from(await self.transcript_entries(marker), marker)

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
        accepted = await self.request("sendPrompt", body)
        if not isinstance(accepted, dict) or accepted.get("accepted") is not True:
            raise GrokError("Grok Bot 未接受本次问题，请在应用中检查状态。")
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
                    raise GrokError("Grok Bot 拒绝了本次任务，请在应用中查看原因。")
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
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(20, connect=10), follow_redirects=False)


async def ask(config: GrokConfig, prompt: str) -> Reply:
    async with make_client() as client:
        try:
            return await asyncio.wait_for(Gateway(config, client).ask(prompt), timeout=config.timeout)
        except asyncio.TimeoutError:
            raise GrokError(f"Grok Bot 本次等待超过 {config.timeout:g} 秒，任务可能仍在云端运行，请在应用中查看。") from None


async def check(config: GrokConfig) -> str:
    async with make_client() as client:
        gateway = Gateway(config, client)
        health = await gateway.request("health")
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise GrokError("Grok Bot 网关健康检查未通过。")
        config.persona()
        if not config.agent_id:
            rows = await gateway.request("listAgents")
            if not isinstance(rows, list):
                raise GrokError(PROTOCOL_ERROR)
            return "Grok Bot 网关、Token 和本地人设模板检查通过。各会话首次提问时创建独立 Bot；本检查未创建 Bot。"
        _, busy = await gateway.state()
        return "Grok Bot 网关、Token、人设和参考 Bot 检查通过。" + ("参考 Bot 正在处理任务。" if busy else "参考 Bot 当前空闲。") + "问答会使用各会话独立的 Bot。"
