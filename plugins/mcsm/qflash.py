"""QQ flash transfer link resolver for MCSM Docker deploy."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx


API_BASE = "https://qfile.qq.com/http2rpc/gotrpc/noauth/"
SIGN_KEY = b"9EB18BB9ED457684"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

GET_FILESET_ID_BY_CODE = "trpc.file.flashtransfer.FlashTransferService/GetFilesetIDByCode"
GET_FILESET = "trpc.file.FileFlashTrans/GetFileset"
GET_FILE_LIST = "trpc.file.FileFlashTrans/GetFileList"
BATCH_DOWNLOAD = "trpc.qqntv2.richmedia.InnerProxy/BatchDownload"

OIDB = {
    GET_FILESET_ID_BY_CODE: "0x93eb_2",
    GET_FILESET: "0x93d3_1",
    GET_FILE_LIST: "0x93d4_1",
    BATCH_DOWNLOAD: "0x9248_4",
}

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar", ".7z", ".rar")


class QFlashError(RuntimeError):
    """Raised when a QQ flash transfer link cannot be resolved."""


@dataclass(slots=True)
class QFlashArchive:
    name: str
    size: int
    fileset_id: str
    expired_time: int
    physical_id: str
    cli_fileid: str
    download_url: str


def extract_qflash_code(url: str) -> str:
    """Extract the code from https://qfile.qq.com/q/<code>."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = parsed.netloc.lower()
    if host != "qfile.qq.com" and not host.endswith(".qfile.qq.com"):
        return ""
    match = re.fullmatch(r"/q/([^/?#]+)", parsed.path.rstrip("/"))
    return match.group(1) if match else ""


def is_qflash_url(url: str) -> bool:
    return bool(extract_qflash_code(url))


def is_archive_name(name: str) -> bool:
    lowered = name.strip().lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def format_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def qflash_archive_label(item: QFlashArchive) -> str:
    return f"{item.name} ({format_size(item.size)})"


def qflash_download_url(item: QFlashArchive) -> str:
    """Return the direct download URL with a filename query for downloaders that need it."""
    parsed = urlsplit(item.download_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["filename"] = item.name
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment))


def qflash_download_url_candidates(item: QFlashArchive) -> list[str]:
    """Return direct URLs to try with daemon-side downloaders."""
    candidates: list[str] = []
    for url in (qflash_download_url(item), item.download_url):
        if url and url not in candidates:
            candidates.append(url)
    return candidates


def qflash_archive_same_file(left: QFlashArchive, right: QFlashArchive) -> bool:
    """Return whether two resolved archive entries point to the same shared file."""
    if left.physical_id and right.physical_id and left.physical_id == right.physical_id:
        return True
    if left.cli_fileid and right.cli_fileid and left.cli_fileid == right.cli_fileid:
        return True
    return left.name == right.name and int(left.size) == int(right.size)


async def refresh_qflash_archive(source_url: str, selected: QFlashArchive) -> QFlashArchive:
    """Resolve the share again and return a fresh direct download URL for the selected archive."""
    archives = await resolve_qflash_archives(source_url)
    for archive in archives:
        if qflash_archive_same_file(selected, archive):
            return archive
    raise QFlashError(f"重新解析闪传直链失败: 未找到原压缩包 {selected.name} ({format_size(selected.size)})")


def safe_archive_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", name.strip())
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or "server-package.zip"


async def preflight_qflash_archive(item: QFlashArchive) -> None:
    """Verify that the resolved direct URL is currently downloadable."""
    url = qflash_download_url(item)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status_code != 200:
                    raise QFlashError(f"闪传压缩包预检失败: HTTP {resp.status_code}")
                content_type = str(resp.headers.get("content-type") or "").lower()
                first = b""
                async for chunk in resp.aiter_bytes(4096):
                    first += chunk
                    if len(first) >= 8:
                        break
    except httpx.HTTPError as exc:
        raise QFlashError(f"连接闪传下载地址失败: {exc}") from exc
    if not first:
        raise QFlashError("闪传压缩包预检失败: 文件为空")
    if "text/html" in content_type:
        raise QFlashError("闪传压缩包预检失败: 下载地址返回 HTML 页面")
    if item.name.lower().endswith(".zip") and not first.startswith(b"PK"):
        raise QFlashError("闪传压缩包预检失败: ZIP 文件头不正确")


async def download_qflash_archive(item: QFlashArchive, target: Path) -> None:
    """Stream the resolved QQ flash transfer archive to a local temporary file."""
    url = qflash_download_url(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status_code != 200:
                    raise QFlashError(f"下载闪传压缩包失败: HTTP {resp.status_code}")
                with target.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        if chunk:
                            fp.write(chunk)
    except httpx.HTTPError as exc:
        raise QFlashError(f"下载闪传压缩包失败: {exc}") from exc
    if target.stat().st_size <= 0:
        raise QFlashError("下载闪传压缩包失败: 文件为空")


def _json_body(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def build_qflash_headers(api: str, body: dict[str, Any], referer: str = "https://qfile.qq.com/") -> dict[str, str]:
    oidb = OIDB[api]
    command, service_type = oidb.split("_", 1)
    body_text = _json_body(body)
    nonce = str(random.randint(1, 10000))
    timestamp = str(int(time.time()))
    signature = hmac.new(SIGN_KEY, (body_text + nonce + timestamp).encode("utf-8"), hashlib.sha1).hexdigest()
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": "https://qfile.qq.com",
        "Referer": referer,
        "x-oidb": f'{{"uint32_command":"{command}", "uint32_service_type":"{service_type}"}}',
        "cookie": "uin=9000002;p_uin=9000002;",
        "x-device-id": "",
        "x-qq-ar-nonce": nonce,
        "x-qq-ar-timestamp": timestamp,
        "x-qq-ar-signature": signature,
    }


class QFlashResolver:
    """Resolve QQ flash transfer share pages into direct download URLs."""

    def __init__(self, timeout: float = 20) -> None:
        self.timeout = timeout

    async def resolve_archives(self, url: str) -> list[QFlashArchive]:
        code = extract_qflash_code(url)
        if not code:
            raise QFlashError("不是有效的 QQ 闪传链接")

        fileset_id = await self._fileset_id_by_code(code, url)
        fileset = await self._get_fileset(fileset_id, url)
        expired_time = _to_int(fileset.get("expired_time") or fileset.get("expiredTime"))
        files = await self._get_file_list(fileset_id, url)
        archive_entries = [entry for entry in files if _entry_name(entry) and is_archive_name(_entry_name(entry))]
        if not archive_entries:
            raise QFlashError("闪传内没有可部署的压缩包文件")

        urls = await self._batch_download(archive_entries, url)
        archives: list[QFlashArchive] = []
        for entry, download_url in zip(archive_entries, urls):
            if not download_url:
                continue
            archives.append(
                QFlashArchive(
                    name=_entry_name(entry),
                    size=_to_int(entry.get("file_size") or entry.get("fileSize") or entry.get("file_physical_size")),
                    fileset_id=fileset_id,
                    expired_time=expired_time,
                    physical_id=_physical_id(entry),
                    cli_fileid=str(entry.get("cli_fileid") or entry.get("cliFileid") or ""),
                    download_url=download_url,
                )
            )
        if not archives:
            raise QFlashError("闪传压缩包没有返回可用下载地址")
        return archives

    async def _fileset_id_by_code(self, code: str, referer: str) -> str:
        data = await self._rpc(GET_FILESET_ID_BY_CODE, {"code": code}, referer)
        fileset_id = str(_nested(data, "data", "fileset_id") or _nested(data, "data", "filesetId") or "")
        if not fileset_id:
            raise QFlashError(_api_message(data) or "闪传链接失效或无法读取 fileset")
        return fileset_id

    async def _get_fileset(self, fileset_id: str, referer: str) -> dict[str, Any]:
        data = await self._rpc(GET_FILESET, {"fileset_id": fileset_id}, referer)
        fileset = _nested(data, "data", "fileset")
        if not isinstance(fileset, dict):
            raise QFlashError(_api_message(data) or "无法读取闪传文件集信息")
        return fileset

    async def _get_file_list(self, fileset_id: str, referer: str) -> list[dict[str, Any]]:
        body = {
            "fileset_id": fileset_id,
            "req_infos": [
                {
                    "parent_id": "",
                    "req_depth": 1,
                    "count": 100,
                    "pagination_info": None,
                    "filter_condition": {"file_category": 0},
                    "sort_conditions": [{"sort_field": 0, "sort_order": 0}],
                }
            ],
            "support_folder_status": True,
        }
        data = await self._rpc(GET_FILE_LIST, body, referer)
        file_lists = _nested(data, "data", "file_lists") or _nested(data, "data", "fileLists") or []
        files: list[dict[str, Any]] = []
        if isinstance(file_lists, list):
            for item in file_lists:
                if not isinstance(item, dict):
                    continue
                raw = item.get("file_list") or item.get("fileList") or []
                if isinstance(raw, list):
                    files.extend(entry for entry in raw if isinstance(entry, dict) and not entry.get("is_dir"))
        if not files:
            raise QFlashError(_api_message(data) or "闪传内没有可下载文件")
        return files

    async def _batch_download(self, files: list[dict[str, Any]], referer: str) -> list[str]:
        download_info = []
        for entry in files:
            physical_id = _physical_id(entry)
            if not physical_id:
                continue
            download_info.append(
                {
                    "batch_id": physical_id,
                    "scene": {"business_type": 4, "app_type": 22, "scene_type": 5},
                    "index_node": {"file_uuid": physical_id},
                    "url_type": 2,
                    "download_scene": 1,
                }
            )
        if not download_info:
            raise QFlashError("闪传文件缺少 physical id，无法换取下载地址")
        data = await self._rpc(BATCH_DOWNLOAD, {"req_head": {"agent": 8}, "download_info": download_info}, referer)
        rows = _nested(data, "data", "download_rsp") or _nested(data, "data", "downloadRsp") or []
        if not isinstance(rows, list):
            raise QFlashError(_api_message(data) or "闪传下载接口返回格式异常")
        return [str(row.get("url") or "") for row in rows if isinstance(row, dict)]

    async def _rpc(self, api: str, body: dict[str, Any], referer: str) -> dict[str, Any]:
        payload = dict(body)
        payload.setdefault("scene_type", 0)
        headers = build_qflash_headers(api, payload, referer)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(API_BASE + api, content=_json_body(payload).encode("utf-8"), headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise QFlashError(f"连接 QQ 闪传接口失败: {exc}") from exc
        except ValueError as exc:
            raise QFlashError("QQ 闪传接口返回的不是 JSON") from exc
        if data.get("retcode") not in (0, "0", None):
            raise QFlashError(_api_message(data) or "QQ 闪传接口返回错误")
        return data


async def resolve_qflash_archives(url: str) -> list[QFlashArchive]:
    return await QFlashResolver().resolve_archives(url)


def _entry_name(entry: dict[str, Any]) -> str:
    return str(entry.get("name") or entry.get("filename") or "").strip()


def _physical_id(entry: dict[str, Any]) -> str:
    physical = entry.get("physical")
    if isinstance(physical, dict):
        value = physical.get("id") or physical.get("file_uuid") or physical.get("fileUuid")
        if value:
            return str(value)
    return str(entry.get("physical_id") or entry.get("physicalId") or "")


def _nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _api_message(data: dict[str, Any]) -> str:
    message = data.get("message") or data.get("msg")
    error = data.get("error")
    if isinstance(error, dict):
        message = message or error.get("message")
    return str(message or "").strip()
