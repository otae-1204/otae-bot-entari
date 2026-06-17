"""MCSManager v10.x API 异步客户端."""

from __future__ import annotations

import re
import asyncio
import logging
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from loguru import logger

# ANSI 终端转义码正则（颜色/光标/DEC 私有模式/键盘模式等）
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── MCSM 实例状态常量 ──
STATUS_MAP: Dict[int, str] = {
    -1: "BUSY",
    0: "STOPPED",
    1: "STOPPING",
    2: "STARTING",
    3: "RUNNING",
}

STATUS_EMOJI: Dict[int, str] = {
    -1: "⏳",   # ⏳
    0: "⬛",    # ⬛
    1: "⭕",    # ⭕
    2: "⏳",    # ⏳
    3: "✅",    # ✅
}

OPERATION_NAMES: Dict[str, str] = {
    "open": "启动",
    "stop": "停止",
    "restart": "重启",
    "kill": "强制结束",
}


class MCSMAPIError(RuntimeError):
    """MCSM API 请求失败，带可直接展示给用户的错误信息."""


def redact_sensitive_text(text: Any) -> str:
    """Remove secrets and short-lived URLs before logging or showing errors."""
    redacted = str(text or "")
    redacted = re.sub(r"(https?://)[^\s\"'<>]+", r"\1<redacted-url>", redacted)
    redacted = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(rkey=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(token=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(password=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1<redacted>", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", redacted)
    redacted = re.sub(r"([A-Fa-f0-9]{24,})", "<redacted-token>", redacted)
    return redacted


class MCSMClient:
    """MCSManager 面板 API 客户端."""

    def __init__(self, panel_url: str, api_key: str) -> None:
        base = panel_url.rstrip("/")
        # 若 URL 不含协议，默认追加 http://
        if not base.startswith("http"):
            base = f"http://{base}"
        self.base_url: str = base
        self.api_key: str = api_key
        self._headers: Dict[str, str] = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=utf-8",
        }
        # 缓存 daemon 列表，避免每次都查
        self._daemon_cache: Optional[List[Dict[str, Any]]] = None

    @staticmethod
    def _api_error_message(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("error", "message", "msg"):
                value = data.get(key)
                if value:
                    return redact_sensitive_text(value)
            nested = data.get("data")
            if isinstance(nested, dict):
                for key in ("error", "message", "msg"):
                    value = nested.get(key)
                    if value:
                        return redact_sensitive_text(value)
            elif nested:
                return redact_sensitive_text(nested)[:200]
            status = data.get("status")
            if status is not None:
                return f"status={status}"
        text = str(data).strip()
        return redact_sensitive_text(text)[:200] if text else "unknown error"

    @staticmethod
    def _debug_instance_shape(source: str, inst: Dict[str, Any]) -> None:
        def keys_of(value: Any) -> List[str]:
            return sorted(value.keys()) if isinstance(value, dict) else []

        summary = {
            "top": sorted(inst.keys()),
            "config": keys_of(inst.get("config")),
            "info": keys_of(inst.get("info")),
            "processInfo": keys_of(inst.get("processInfo")),
            "resource": keys_of(inst.get("resource")),
            "resources": keys_of(inst.get("resources")),
        }
        logger.debug(f"[MCSM] {source} instance shape: {summary}")

    # ── 内部请求方法 ──

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: float = 15,
    ) -> Dict[str, Any]:
        """发送 MCSM API 请求."""
        if params is None:
            params = {}
        params.setdefault("apikey", self.api_key)

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(headers=self._headers, trust_env=False, timeout=timeout) as client:
                resp = await client.request(method, url, params=params, json=json_data)
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MCSMAPIError("连接面板超时，请检查面板地址或网络") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = redact_sensitive_text(exc.response.text[:200].strip())
            raise MCSMAPIError(f"面板返回 HTTP {status}: {body or '无响应内容'}") from exc
        except httpx.HTTPError as exc:
            raise MCSMAPIError(f"连接面板失败: {redact_sensitive_text(exc)}") from exc

        try:
            return resp.json()
        except ValueError as exc:
            text = redact_sensitive_text(resp.text[:200].strip())
            raise MCSMAPIError(f"面板返回的不是 JSON: {text or '空响应'}") from exc

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送 GET 请求."""
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """发送 POST 请求."""
        return await self._request("POST", path, params=params, json_data=json_data, timeout=timeout)

    async def _put(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """发送 PUT 请求."""
        return await self._request("PUT", path, params=params, json_data=json_data, timeout=timeout)

    async def _delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """发送 DELETE 请求."""
        return await self._request("DELETE", path, params=params, json_data=json_data, timeout=timeout)

    def _daemon_host(self, daemon_id: str) -> str:
        daemons = self._daemon_cache or []
        for daemon in daemons:
            if str(daemon.get("uuid") or daemon.get("id") or "") != str(daemon_id):
                continue
            for key in ("ip", "host", "hostname", "address", "addr"):
                value = str(daemon.get(key) or "").strip()
                if value:
                    return value.split(":")[0]
            remote = daemon.get("remote") or daemon.get("config") or {}
            if isinstance(remote, dict):
                for key in ("ip", "host", "hostname", "address", "addr"):
                    value = str(remote.get(key) or "").strip()
                    if value:
                        return value.split(":")[0]
        return ""

    def _normalize_upload_url(self, raw_url: str, daemon_id: str) -> str:
        """Replace daemon-local upload host with the selected daemon host when possible."""
        url = str(raw_url or "").strip()
        if not url:
            raise MCSMAPIError("获取上传地址失败: 面板未返回上传地址")
        parsed = urlsplit(url if re.match(r"^https?://", url, re.I) else f"http://{url}")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return urlunsplit(parsed)
        daemon_host = self._daemon_host(daemon_id)
        if not daemon_host:
            return urlunsplit(parsed)
        netloc = daemon_host
        if parsed.port:
            netloc = f"{daemon_host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    def _build_upload_url(self, config: Dict[str, Any], daemon_id: str) -> str:
        """Build the daemon upload URL from MCSM v10 upload config."""
        direct_url = config.get("url") or config.get("uploadUrl") or config.get("upload_url")
        if direct_url:
            return self._normalize_upload_url(str(direct_url), daemon_id)

        addr = config.get("addr") or config.get("address")
        password = config.get("password")
        if not addr:
            raise MCSMAPIError("获取上传地址失败: 面板未返回 daemon 地址")
        if not password:
            raise MCSMAPIError("获取上传地址失败: 面板未返回上传密码")

        base = self._normalize_upload_url(str(addr), daemon_id).rstrip("/")
        return f"{base}/upload/{quote(str(password), safe='')}"

    @staticmethod
    def _instance_file_path(name: str) -> str:
        """Normalize a file path inside the MCSM instance root."""
        value = str(name or "").strip().replace("\\", "/")
        value = value.lstrip("/")
        parts = [
            part
            for part in PurePosixPath(value).parts
            if part not in {"", ".", "..", "/"}
        ]
        if not parts:
            raise MCSMAPIError("实例文件路径不能为空")
        return "/" + "/".join(parts)

    # ── Daemon 相关 ──

    async def get_daemon_list(self) -> List[Dict[str, Any]]:
        """获取远程节点列表（基础信息）."""
        data = await self._get("/api/service/remote_services_list")
        if data.get("status") != 200:
            err = self._api_error_message(data)
            logger.error(f"[MCSM] 获取节点列表失败: {data}")
            raise MCSMAPIError(f"获取节点列表失败: {err}")
        daemons = data.get("data", [])
        self._daemon_cache = daemons
        return daemons

    def clear_daemon_cache(self) -> None:
        """Clear daemon cache so the next request reloads panel nodes."""
        self._daemon_cache = None

    async def refresh_daemon_list(self) -> List[Dict[str, Any]]:
        """Force reload daemon list from panel."""
        self.clear_daemon_cache()
        return await self.get_daemon_list()

    async def _ensure_daemons(self) -> List[Dict[str, Any]]:
        """确保 daemon 缓存可用."""
        if not self._daemon_cache:
            await self.get_daemon_list()
        return self._daemon_cache or []

    async def get_daemon_ids(self) -> List[str]:
        """获取所有 daemon UUID 列表."""
        daemons = await self._ensure_daemons()
        return [d["uuid"] for d in daemons]

    async def list_docker_images(self, daemon_id: str) -> List[Dict[str, Any]]:
        """获取指定节点的 Docker 镜像列表."""
        data = await self._get("/api/environment/image", {"daemonId": daemon_id})
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"获取 Docker 镜像失败: {err}")
        images = data.get("data", [])
        return images if isinstance(images, list) else []

    async def list_supported_docker_daemons(self) -> List[Dict[str, Any]]:
        """返回在线且可查询 Docker 镜像的节点."""
        daemons = await self.get_daemon_list()
        supported: List[Dict[str, Any]] = []
        for daemon in daemons:
            daemon_id = str(daemon.get("uuid") or daemon.get("id") or "")
            if not daemon_id:
                continue
            try:
                images = await self.list_docker_images(daemon_id)
            except MCSMAPIError as exc:
                logger.debug(f"[MCSM] daemon {daemon_id} 不支持 Docker 镜像查询: {exc}")
                continue
            item = dict(daemon)
            item["_dockerImages"] = images
            supported.append(item)
        return supported

    # ── 实例列表 ──

    async def get_all_instances(self) -> List[Dict[str, Any]]:
        """获取所有节点的所有实例."""
        daemons = await self._ensure_daemons()
        semaphore = asyncio.Semaphore(5)

        async def load_daemon_instances(daemon: Dict[str, Any]) -> List[Dict[str, Any]]:
            daemon_id = daemon["uuid"]
            async with semaphore:
                try:
                    instances = await self._get_instances_by_daemon(daemon_id)
                except MCSMAPIError as exc:
                    logger.warning(f"[MCSM] 获取 daemon {daemon_id} 实例列表失败: {exc}")
                    return []
            for inst in instances:
                inst["_daemonId"] = daemon_id
                inst["_daemonName"] = daemon.get("remarks", daemon_id[:8])
            return instances

        results = await asyncio.gather(*(load_daemon_instances(daemon) for daemon in daemons))
        return [inst for instances in results for inst in instances]

    async def get_daemon_instances(self, daemon_id: str) -> List[Dict[str, Any]]:
        """获取指定节点下的所有实例（公开接口）."""
        return await self._get_instances_by_daemon(daemon_id)

    async def find_instance_daemon(self, uuid: str) -> Optional[str]:
        """Find the daemon that owns an instance UUID without exposing probe errors."""
        target_uuid = str(uuid).lower()
        daemons = await self.get_daemon_list()

        for daemon in daemons:
            daemon_id = str(daemon.get("uuid") or daemon.get("id") or "")
            if not daemon_id:
                continue
            try:
                instances = await self.get_daemon_instances(daemon_id)
            except Exception as exc:
                logger.warning(f"[MCSM] 探测 daemon {daemon_id} 实例列表失败，继续探测其他节点: {exc}")
                continue
            for inst in instances:
                inst_uuid = str(inst.get("instanceUuid") or inst.get("uuid") or "").lower()
                if inst_uuid == target_uuid:
                    return daemon_id

        for daemon in daemons:
            daemon_id = str(daemon.get("uuid") or daemon.get("id") or "")
            if not daemon_id:
                continue
            try:
                detail = await self.get_instance_detail(uuid, daemon_id)
            except Exception as exc:
                logger.debug(f"[MCSM] 探测实例 {uuid} 在 daemon {daemon_id} 详情失败，按未命中处理: {exc}")
                continue
            if detail is not None:
                return daemon_id

        return None

    async def _get_instances_by_daemon(
        self, daemon_id: str, page: int = 1, page_size: int = 50
    ) -> List[Dict[str, Any]]:
        """获取指定节点下的实例列表."""
        params = {
            "daemonId": daemon_id,
            "page": page,
            "page_size": page_size,
            "instance_name": "",
            "status": "",
        }
        data = await self._get("/api/service/remote_service_instances", params)
        if data.get("status") != 200:
            err = self._api_error_message(data)
            logger.warning(f"[MCSM] 获取 daemon {daemon_id} 实例列表失败: {err}")
            raise MCSMAPIError(f"获取节点实例失败: {err}")
        result = data.get("data", {})
        instances = result.get("data", [])
        if instances:
            self._debug_instance_shape(f"daemon {daemon_id} list", instances[0])
        return instances

    # ── 实例详情 ──

    async def get_instance_detail(self, uuid: str, daemon_id: str) -> Optional[Dict[str, Any]]:
        """获取单个实例的详细信息."""
        data = await self._get("/api/instance", {"uuid": uuid, "daemonId": daemon_id})
        if data.get("status") != 200:
            logger.debug(f"[MCSM] 获取实例 {uuid} 在节点 {daemon_id} 详情未找到 (探测中正常现象): {data}")
            return None
        detail = data.get("data")
        if isinstance(detail, dict):
            self._debug_instance_shape(f"instance {uuid} detail", detail)
        return detail

    async def create_docker_instance(
        self,
        daemon_id: str,
        name: str,
        image: str,
        start_command: str,
        port: int,
        memory_mb: int = 2048,
    ) -> Dict[str, Any]:
        """创建 Docker 类型实例."""
        daemon_id = str(daemon_id or "").strip()
        if not daemon_id:
            raise MCSMAPIError("创建 Docker 实例失败: 缺少 daemonId，请重新选择节点")
        payload = {
            "nickname": name,
            "startCommand": start_command,
            "stopCommand": "stop",
            "cwd": "/data",
            "ie": "utf-8",
            "oe": "utf-8",
            "type": "minecraft/java",
            "processType": "docker",
            "docker": {
                "image": image,
                "ports": [f"{int(port)}:25565/tcp"],
                "networkMode": "bridge",
                "memory": int(memory_mb),
            },
        }
        data = await self._post("/api/instance", {"daemonId": daemon_id}, payload)
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"创建 Docker 实例失败: {err}")
        return data

    async def delete_instance(
        self,
        uuid: str,
        daemon_id: str,
        *,
        delete_files: bool = False,
    ) -> Dict[str, Any]:
        """删除 MCSM 实例，默认仅删除实例配置，不删除文件."""
        daemon_id = str(daemon_id or "").strip()
        uuid = str(uuid or "").strip()
        if not daemon_id:
            raise MCSMAPIError("删除实例失败: 缺少 daemonId")
        if not uuid:
            raise MCSMAPIError("删除实例失败: 缺少实例 UUID")
        data = await self._delete(
            "/api/instance",
            {"daemonId": daemon_id},
            {"uuids": [uuid], "deleteFile": bool(delete_files)},
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"删除实例失败: {err}")
        return data

    async def install_instance_from_url(
        self,
        uuid: str,
        daemon_id: str,
        url: str,
        title: str = "QQ flash transfer package",
        description: str = "Installed by QQ bot deploy command",
    ) -> Dict[str, Any]:
        """让 daemon 从 URL 下载并安装实例文件."""
        payload = {
            "targetUrl": url,
            "title": title,
            "description": description,
        }
        data = await self._post(
            "/api/protected_instance/install_instance",
            {"uuid": uuid, "daemonId": daemon_id},
            payload,
            timeout=60,
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"安装实例文件失败: {err}")
        return data

    async def get_upload_config(self, uuid: str, daemon_id: str, upload_dir: str = "/") -> Dict[str, Any]:
        """获取 daemon 文件上传地址配置."""
        data = await self._post(
            "/api/files/upload",
            {"uuid": uuid, "daemonId": daemon_id, "upload_dir": upload_dir},
            {},
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"获取上传地址失败: {err}")
        config = data.get("data")
        if not isinstance(config, dict):
            raise MCSMAPIError("获取上传地址失败: 面板返回格式不正确")
        return config

    async def upload_file_to_instance(
        self,
        uuid: str,
        daemon_id: str,
        file_path: str | Path,
        upload_dir: str = "/",
    ) -> str:
        """上传本地文件到实例目录，返回实例内文件名."""
        path = Path(file_path)
        if not path.is_file():
            raise MCSMAPIError(f"上传文件失败: 本地文件不存在 {path}")
        config = await self.get_upload_config(uuid, daemon_id, upload_dir)
        upload_url = self._build_upload_url(config, daemon_id)
        params = dict(config.get("params") or {})
        headers = {"X-Requested-With": "XMLHttpRequest"}
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                with path.open("rb") as fp:
                    files = {"file": (path.name, fp, "application/octet-stream")}
                    resp = await client.post(upload_url, params=params or None, files=files, headers=headers)
                    resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = redact_sensitive_text(exc.response.text[:200].strip())
            raise MCSMAPIError(f"上传到 daemon 失败: HTTP {exc.response.status_code}: {body or '无响应内容'}") from exc
        except httpx.HTTPError as exc:
            raise MCSMAPIError(f"上传到 daemon 失败: {redact_sensitive_text(exc)}") from exc
        return path.name

    async def extract_instance_archive(self, uuid: str, daemon_id: str, archive_name: str, target: str = "/") -> Dict[str, Any]:
        """解压实例目录中的压缩包."""
        source = self._instance_file_path(archive_name)
        payload = {
            "type": 2,
            "source": source,
            "targets": target or "/",
            "code": "utf-8",
        }
        data = await self._post(
            "/api/files/compress",
            {"uuid": uuid, "daemonId": daemon_id},
            payload,
            timeout=120,
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"解压压缩包失败: {err}")
        return data

    async def delete_instance_file(self, uuid: str, daemon_id: str, target: str) -> Dict[str, Any]:
        """删除实例目录中的文件."""
        target_path = self._instance_file_path(target)
        data = await self._delete(
            "/api/files",
            {"uuid": uuid, "daemonId": daemon_id},
            {"targets": [target_path]},
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"删除临时文件失败: {err}")
        return data

    async def read_instance_file(self, uuid: str, daemon_id: str, target: str) -> str:
        """读取实例目录中的文本文件."""
        target_path = self._instance_file_path(target)
        data = await self._put(
            "/api/files/",
            {"uuid": uuid, "daemonId": daemon_id},
            {"target": target_path},
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"读取实例文件失败: {err}")
        content = data.get("data")
        if isinstance(content, dict):
            for key in ("text", "content", "value", "data"):
                if key in content:
                    return str(content.get(key) or "")
        return str(content or "")

    async def write_instance_file(self, uuid: str, daemon_id: str, target: str, text: str) -> Dict[str, Any]:
        """写入实例目录中的文本文件."""
        target_path = self._instance_file_path(target)
        data = await self._put(
            "/api/files/",
            {"uuid": uuid, "daemonId": daemon_id},
            {"target": target_path, "text": text},
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"写入实例文件失败: {err}")
        return data

    async def update_instance_start_command(
        self,
        uuid: str,
        daemon_id: str,
        command: str,
    ) -> Dict[str, Any]:
        """更新实例启动命令."""
        daemon_id = str(daemon_id or "").strip()
        uuid = str(uuid or "").strip()
        if not daemon_id:
            raise MCSMAPIError("更新启动命令失败: 缺少 daemonId")
        if not uuid:
            raise MCSMAPIError("更新启动命令失败: 缺少实例 UUID")
        detail = await self.get_instance_detail(uuid, daemon_id)
        if not detail:
            raise MCSMAPIError("实例创建后无法读取详情，不能更新启动命令")
        config = dict(detail.get("config") or detail)
        config["startCommand"] = command
        data = await self._put("/api/instance", {"uuid": uuid, "daemonId": daemon_id}, config)
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"更新启动命令失败: {err}")
        return data

    @staticmethod
    def _looks_like_file_entry(entry: Any) -> bool:
        if isinstance(entry, str):
            return bool(entry.strip())
        if not isinstance(entry, dict):
            return False
        return any(
            key in entry
            for key in (
                "name",
                "fileName",
                "filename",
                "file_name",
                "basename",
                "title",
                "path",
                "target",
                "absolutePath",
                "realName",
                "real_name",
                "displayName",
                "type",
                "isDirectory",
                "isDir",
                "directory",
                "is_dir",
            )
        )

    @classmethod
    def _extract_file_entries(cls, payload: Any) -> List[Any]:
        """Extract file entries from common MCSM file-list response shapes."""
        if isinstance(payload, list):
            if not payload or any(cls._looks_like_file_entry(item) for item in payload):
                return payload
            return []
        if not isinstance(payload, dict):
            return []

        preferred_keys = (
            "data",
            "items",
            "files",
            "fileList",
            "file_list",
            "list",
            "rows",
            "children",
            "objects",
        )
        for key in preferred_keys:
            if key not in payload:
                continue
            entries = cls._extract_file_entries(payload.get(key))
            if entries:
                return entries

        for value in payload.values():
            entries = cls._extract_file_entries(value)
            if entries:
                return entries
        return []

    async def list_instance_files(
        self,
        uuid: str,
        daemon_id: str,
        target: str = "/",
        page: int = 0,
        page_size: int = 200,
    ) -> List[Any]:
        """列出实例工作目录文件."""
        target = str(target or "").strip() or "/"
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = 200
        page = max(0, page)
        page_size = max(1, page_size)
        data = await self._get(
            "/api/files/list",
            {
                "uuid": uuid,
                "daemonId": daemon_id,
                "target": target,
                "page": page,
                "page_size": page_size,
                "file_name": "",
            },
        )
        if data.get("status") != 200:
            err = self._api_error_message(data)
            raise MCSMAPIError(f"读取实例文件列表失败: {err}")
        files = self._extract_file_entries(data.get("data", []))
        if not files and isinstance(data.get("data"), dict):
            total = data["data"].get("total")
            if str(total or "").strip() not in {"", "0"}:
                logger.warning(
                    "MCSM file list returned no entries but total={}; data keys={}",
                    total,
                    list(data["data"].keys()),
                )
            else:
                logger.debug("MCSM file list returned no entries; data keys={}", list(data["data"].keys()))
        return files

    # ── 实例操作 ──

    async def _instance_action(self, action: str, uuid: str, daemon_id: str) -> Dict[str, Any]:
        """执行实例操作 (open/stop/restart/kill)."""
        data = await self._get(
            f"/api/protected_instance/{action}",
            {"uuid": uuid, "daemonId": daemon_id},
        )
        return data

    async def start_instance(self, uuid: str, daemon_id: str) -> Dict[str, Any]:
        return await self._instance_action("open", uuid, daemon_id)

    async def stop_instance(self, uuid: str, daemon_id: str) -> Dict[str, Any]:
        return await self._instance_action("stop", uuid, daemon_id)

    async def restart_instance(self, uuid: str, daemon_id: str) -> Dict[str, Any]:
        return await self._instance_action("restart", uuid, daemon_id)

    async def kill_instance(self, uuid: str, daemon_id: str) -> Dict[str, Any]:
        return await self._instance_action("kill", uuid, daemon_id)

    async def send_command(self, uuid: str, daemon_id: str, command: str) -> Dict[str, Any]:
        """向实例控制台发送命令."""
        data = await self._get(
            "/api/protected_instance/command",
            {"uuid": uuid, "daemonId": daemon_id, "command": command},
        )
        return data

    async def get_instance_output(
        self, uuid: str, daemon_id: str, size: int = 50
    ) -> str:
        """获取实例控制台输出日志. size 单位为 KB, 范围 1-2048."""
        data = await self._get(
            "/api/protected_instance/outputlog",
            {"uuid": uuid, "daemonId": daemon_id, "size": size},
        )
        if data.get("status") != 200:
            logger.debug(f"[MCSM] 获取实例 {uuid} 输出日志失败: {data}")
            return ""
        raw = data.get("data", "") or ""
        return _ANSI_RE.sub("", raw)

    # ── 实例查找辅助 ──

    async def find_instances(self, name: str) -> List[Dict[str, Any]]:
        """按名称模糊查找实例（在所有节点中搜）. 返回匹配的列表."""
        all_instances = await self.get_all_instances()
        name_lower = name.lower()
        matches: List[Dict[str, Any]] = []
        for inst in all_instances:
            inst_name = (inst.get("config", {}).get("nickname", "") or inst.get("name", "") or inst.get("instanceName", "")).lower()
            inst_id = (inst.get("instanceUuid") or inst.get("uuid") or "").lower()
            if name_lower in inst_name or name_lower in inst_id:
                matches.append(inst)
        return matches

    @staticmethod
    def instance_name(inst: Dict[str, Any]) -> str:
        """从实例数据中提取显示名称."""
        cfg = inst.get("config", {}) or {}
        return cfg.get("nickname", "") or inst.get("instanceName", "") or inst.get("name", "") or (inst.get("instanceUuid") or inst.get("uuid") or "")[:8]

    @staticmethod
    def instance_status(inst: Dict[str, Any]) -> int:
        """从实例数据中提取状态码."""
        return inst.get("status", inst.get("state", -1))

    @staticmethod
    def format_status(status: int) -> str:
        """格式化状态码为可读文本."""
        name = STATUS_MAP.get(status, f"UNKNOWN({status})")
        emoji = STATUS_EMOJI.get(status, "❓")
        return f"{emoji} {name}"
