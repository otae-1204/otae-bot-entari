"""HTTP gateway protocol checked against adam91holt/grokbot-sdk c14347f.

The gateway accepts prompts asynchronously. Read only replies anchored to our
unique prompt, and keep the caller's per-agent lock until polling has finished.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx

from .config import GrokConfig, GrokError

PROTOCOL_ERROR = "Grok Bot 网关返回了无法识别的数据，请管理员检查网关版本。"
AWAITING_USER = "Grok Bot 正在等待人工确认，请管理员打开 Grok Bot 应用处理后再提问。"


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


def reply_from(entries: list[dict], marker: str) -> str | None:
    """An old answer or another user's prompt must never satisfy this request."""
    anchor = None
    for index, row in enumerate(entries):
        if row.get("role") == "user" and marker in str(row.get("content", "")):
            anchor = index
    if anchor is None:
        return None
    outgoing, assistant = [], []
    for row in entries[anchor + 1:]:
        if row.get("role") == "user":
            raise GrokError("Grok Bot 会话中出现了另一条输入，无法可靠对应本次回答。请在应用中查看结果。")
        if row.get("streaming") is True:
            continue
        message = row.get("message")
        if row.get("kind") == "send-message" and isinstance(message, dict) and message.get("type") == "text":
            value = message.get("content")
            if isinstance(value, str) and value.strip():
                outgoing.append(value.strip())
        elif row.get("kind") in (None, "message") and row.get("role") == "assistant":
            value = row.get("content")
            if isinstance(value, str) and value.strip():
                assistant.append(value.strip())
    return "\n\n".join(outgoing) if outgoing else (assistant[-1] if assistant else None)


class Gateway:
    def __init__(self, config: GrokConfig, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def request(self, command: str, body: dict | None = None):
        health = command == "health"
        headers = {"x-sand-request-id": str(uuid4()), "x-sand-slim-avatars": "1"}
        if not health:
            headers["Authorization"] = "Bearer " + self.config.token
        try:
            response = await self.client.request(
                "GET" if health else "POST",
                self.config.base_url + ("/health" if health else "/api/" + command),
                headers=headers, **({} if health else {"json": body or {}}),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise GrokError("连接或读取 Grok Bot 网关超时，请检查 Tailscale 和云端后台服务；已提交的任务可能仍在运行。") from None
        except httpx.HTTPError:
            raise GrokError("无法连接 Grok Bot 网关，请检查两端 Tailscale、网关地址及云端后台服务。") from None
        if response.status_code in {401, 403}:
            raise GrokError("Grok Bot 网关认证失败，请管理员检查 GROKBOT_GATEWAY_TOKEN。")
        if response.status_code == 404:
            raise GrokError(f"Grok Bot 网关接口 {command} 返回 404，请检查基址和网关版本。")
        if response.status_code == 429:
            raise GrokError("Grok Bot 网关请求过于频繁，请稍后重试。")
        if not 200 <= response.status_code < 300:
            raise GrokError(f"Grok Bot 网关请求失败（HTTP {response.status_code}）。")
        try:
            data = response.json()
        except ValueError:
            raise GrokError(PROTOCOL_ERROR) from None
        if isinstance(data, dict) and data.get("error"):
            raise GrokError(f"Grok Bot 网关未能完成 {command}，请在应用中查看详情。")
        return data

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

    async def transcript(self, marker: str) -> str | None:
        rows, before = [], None
        # Limit memory and RPCs while still finding the prompt behind tool events.
        for _ in range(10):
            body = {"id": self.config.agent_id, "limit": 200}
            if before is not None:
                body["beforeSeq"] = before
            payload = await self.request("getAgentTranscriptTail", body)
            rows = entries_from(payload) + rows
            if any(row.get("role") == "user" and marker in str(row.get("content", "")) for row in rows):
                return reply_from(rows, marker)
            cursor = payload.get("nextBeforeSeq") if isinstance(payload, dict) else None
            if not isinstance(cursor, int) or cursor == before:
                return None
            before = cursor
        raise GrokError("Grok Bot 任务记录过长，无法可靠定位本次回答，请在应用中查看结果。")

    async def ask(self, prompt: str) -> str:
        # Wait for app-initiated or previously timed-out work before adding input.
        while True:
            _, busy = await self.state()
            if not busy:
                break
            await asyncio.sleep(self.config.poll_interval)
        nonce = str(uuid4())
        marker = f"[QQ请求编号:{nonce}]"
        content = f"{marker}\n{prompt}\n\n请直接回答本次问题，回复中无需包含请求编号。"
        accepted = await self.request("sendPrompt", {"agentId": self.config.agent_id, "prompt": content, "clientNonce": nonce})
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
            _, busy = await self.state()
            if busy or pending:
                previous_reply = None
            else:
                reply = await self.transcript(marker)
                # Two idle snapshots avoid returning a transient intermediate text.
                if reply and reply == previous_reply:
                    return reply
                previous_reply = reply
            await asyncio.sleep(self.config.poll_interval)


def make_client() -> httpx.AsyncClient:
    # Tailnet requests must bypass global model/search/system proxies.
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(20, connect=10), follow_redirects=False)


async def ask(config: GrokConfig, prompt: str) -> str:
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
