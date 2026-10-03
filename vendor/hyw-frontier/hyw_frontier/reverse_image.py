"""On-demand image upload followed by the existing anonymous Jina Reader."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from html import unescape
import json
import re
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from markdown_it import MarkdownIt

from .http_transport import PooledOpener
from .image_bridge import load_bridge, validate_bridge
from .jina import JinaError, public_url
from .media import discover

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Lens repeatedly returns verification pages through anonymous Reader (2026-09-20).
# Keep its parser for diagnostics, but do not spend a request on it in normal searches.
ENGINES = (("yandex", "Yandex"), ("tineye", "TinEye"))
REVERSE_IMAGE_CONFIG = {"enabled": True, "tool": "reverse_image_search",
                        "upload": "on_tool_call_only",
                        "cleanup_timezone": "Asia/Shanghai", "cleanup_time": "00:00",
                        "automatic_retries": False, "result_format": "reader_extract",
                        "engines": [name for name, _ in ENGINES], "reader_concurrency": len(ENGINES),
                        "disabled_engines": {"google_lens": "anonymous_reader_verification"},
                        "deduplication": "exact_image_url_or_source_url_preserving_provenance"}


def _safe_url(value):
    try:
        return public_url(unescape(value))
    except (JinaError, ValueError, TypeError, AttributeError):
        return None


def _image_url(value):
    return bool(re.search(r"\.(?:jpe?g|png|webp|gif)$", urlsplit(value).path, re.I))


def _engine_asset(value):
    host = (urlsplit(value).hostname or "").lower()
    return (host.endswith(("yandex.net", "yandex.ru", "yandex.com", "google.com", "gstatic.com"))
            or "favicon" in value)


def visual_matches(content: str, engine: str, target: str) -> list[dict]:
    """Read actual Markdown result cards; never treat navigation/recommendation images as matches.

    Lens cards may have blob thumbnails: retain their page/title without pretending
    that the thumbnail is downloadable. Yandex cards also expose full image URLs.
    """
    parser = MarkdownIt("commonmark")
    matches = []
    for line in content.splitlines():
        if "![" not in line:
            continue
        links, current = [], None
        for token in parser.parseInline(line)[0].children or []:
            if token.type == "link_open":
                current = {"url": _safe_url(token.attrGet("href")), "labels": [], "images": []}
            elif token.type == "link_close" and current is not None:
                links.append(current)
                current = None
            elif current is not None:
                if token.type == "image":
                    current["images"].append(token.attrGet("src") or "")
                    current["labels"].append(re.sub(r"^Image \d+:?\s*", "", token.content))
                elif token.type in ("text", "code_inline"):
                    current["labels"].append(token.content)
        pages = []
        for link in links:
            url = link["url"]
            if not url or _image_url(url):
                continue
            parts = urlsplit(url)
            is_lens_result = parts.hostname == "lens.google.com" and parts.path == "/goto"
            if (engine == "google_lens" and not is_lens_result) or (engine == "yandex" and _engine_asset(url)):
                continue
            if url not in [page["url"] for page in pages]:
                pages.append(link)
        for page in pages:
            title = " ".join(" ".join(page["labels"]).split()) or urlsplit(page["url"]).hostname
            if engine == "google_lens":
                images = [url for raw in page["images"] if (url := _safe_url(raw)) and "favicon" not in url]
            else:
                # Reader sometimes joins a closing Markdown link and a bare URL.
                separated = re.sub(r"\)(?=https?://)", ") ", line)
                images = [c.url for c in discover(separated, page["url"], title, 0) if "favicon" not in c.url]
                # Prefer a full-size image explicitly present in the response to its
                # thumbnail. This only orders observed URLs; it never constructs one.
                images.sort(key=lambda url: (_engine_asset(url), "/mx_" in url))
            image = images[0] if images else None
            matches.append({"url": page["url"], "title": title,
                            **({"image_url": image} if image else {}),
                            "image_status": "url_available" if image else "unavailable_thumbnail",
                            "source_url_status": "redirect_link" if engine == "google_lens" else "page_link",
                            "evidence_type": "visual_search_candidate"})
    return matches


def aggregate_matches(sources: list[dict]) -> tuple[list[dict], dict]:
    """Round-robin providers, then collapse identical URLs without losing associations."""
    merged, by_key = [], {}
    count = 0
    longest = max((len(source.get("matches", [])) for source in sources), default=0)
    for index in range(longest):
        for source in sources:
            rows = source.get("matches", [])
            if index >= len(rows):
                continue
            row = rows[index]
            count += 1
            key = ("image", row["image_url"]) if row.get("image_url") else ("page", row["url"])
            provenance = {"engine": source["engine"], "engine_name": source["engine_name"], **row}
            if key not in by_key:
                item = {**row, "match_id": f"reverse_match_{len(merged) + 1}",
                        "engines": [], "sources": [], "duplicate_count": 0}
                by_key[key] = item
                merged.append(item)
            item = by_key[key]
            if source["engine"] not in item["engines"]:
                item["engines"].append(source["engine"])
            if provenance not in item["sources"]:
                item["sources"].append(provenance)
            if item["match_id"] not in source["match_ids"]:
                source["match_ids"].append(item["match_id"])
            item["duplicate_count"] += 1
    for item in merged:
        item["duplicate_count"] -= 1
    return merged, {"method": "exact_image_url_or_source_url", "input_count": count,
                    "output_count": len(merged), "duplicates_merged": count - len(merged),
                    "visual_similarity_deduplication": False, "provenance_preserved": True}


def tineye_matches(content: str) -> list[dict]:
    """Extract image/backlink associations, retaining the complete Reader text separately."""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        raise JinaError("invalid_tineye_response", "TinEye 未返回有效 JSON；不把验证页或错误页当作搜图结果") from None
    if (not isinstance(payload, dict) or payload.get("status", "ok") != "ok"
            or payload.get("error") or payload.get("errors")):
        raise JinaError("tineye_error", "TinEye 返回错误状态；未自动重试，请说明本次搜图未成功")
    rows = payload.get("matches")
    # The live website endpoint returns matches + num_matches, without status.
    if (not isinstance(rows, list) or type(payload.get("num_matches")) is not int
            or payload["num_matches"] < 0):
        raise JinaError("invalid_tineye_response", "TinEye JSON 缺少有效 matches 列表或 num_matches 数量")
    matches, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        backlinks = list(row["backlinks"]) if isinstance(row.get("backlinks"), list) else []
        domains = row.get("domains")
        if isinstance(domains, list):
            for domain in domains:
                if isinstance(domain, dict) and isinstance(domain.get("backlinks"), list):
                    backlinks.extend(domain["backlinks"])
        for backlink in backlinks:
            if not isinstance(backlink, dict):
                continue
            try:
                source = public_url(backlink.get("backlink", ""))
                image = public_url(backlink.get("url", ""))
            except (JinaError, TypeError, AttributeError):
                continue
            if (source, image) in seen:
                continue
            seen.add((source, image))
            matches.append({"url": source, "image_url": image,
                            "title": str(row.get("domain") or urlsplit(source).hostname or source),
                            "score": row.get("score"), "crawl_date": backlink.get("crawl_date"),
                            "evidence_type": "reverse_image_match"})
    return matches


class ReverseImageSearch:
    def __init__(self, jina, originals=(), *, opener=None, bridge=None, image_lookup=None):
        self.jina = jina
        self.originals = tuple(originals)
        self.image_lookup = image_lookup
        self.opener = opener if opener is not None else PooledOpener()
        self._lock = Lock()
        self._uploads = {}
        self._bridge = validate_bridge(bridge) if bridge is not None else None

    def close(self):
        self.originals = ()
        self.image_lookup = None
        self._uploads.clear()
        self._bridge = None
        self.opener.close()

    def _valid_upload(self, upload):
        try:
            url = public_url(upload["url"])
            parts = urlsplit(url)
            expires = datetime.fromisoformat(upload["expiresAt"].replace("Z", "+00:00")).timestamp()
            return (parts.scheme == "https" and parts.netloc == urlsplit(self._bridge["url"]).netloc
                    and parts.path == "/images/" + upload["sha256"]
                    and len(upload["sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in upload["sha256"])
                    and not parts.query and expires > time.time() + 60)
        except (KeyError, ValueError, TypeError, AttributeError, JinaError):
            return False

    def _upload(self, data: bytes, content_type: str) -> dict:
        request = Request(self._bridge["url"] + "/upload", data=data, method="POST", headers={
            "Authorization": "Bearer " + self._bridge["api_key"], "Content-Type": content_type,
            "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = bytearray()
                while len(raw) <= 65536:
                    chunk = response.read(65537 - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError
            uploaded = json.loads(raw)
            if not self._valid_upload(uploaded):
                raise ValueError
            return uploaded
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise JinaError("image_upload_http_" + str(status),
                            f"图片上传 HTTP {status}；未调用搜图引擎，未自动重试") from None
        except (URLError, TimeoutError, OSError):
            raise JinaError("image_upload_failed", "图片上传连接失败或超时；未调用搜图引擎，未自动重试") from None
        except (ValueError, UnicodeError):
            raise JinaError("invalid_upload_response", "图片中转返回无效结果；未调用搜图引擎") from None

    def _read_engine(self, item):
        engine, name, target = item
        started = time.monotonic()
        source = {"engine": engine, "engine_name": name, "url": target,
                  "reader_url": "https://r.jina.ai/" + target, "ok": False,
                  "matches": [], "match_ids": [], "untrusted_content": True}
        try:
            source.update(self.jina.read_url({"url": target}))
            content = source["content"]
            if engine == "tineye":
                source["matches"] = tineye_matches(content)
                payload = json.loads(content)
                source["num_matches"] = payload["num_matches"]
                for key in ("num_pages", "num_unavailable_matches"):
                    if type(payload.get(key)) is int:
                        source[key] = payload[key]
                source.update(ok=True, status="matched" if payload["num_matches"] else "no_matches")
            else:
                source["matches"] = visual_matches(content, engine, target)
                if source["matches"]:
                    source.update(ok=True, status="matched")
                elif any(marker in content.lower() for marker in (
                        "unusual traffic", "performing security verification", "verify you are human",
                        "confirm that you", "not a robot", "showcaptcha")):
                    source.update(status="blocked", code="engine_verification", error="Reader 返回验证页，没有可用搜图结果")
                elif any(marker in content.lower() for marker in (
                        "no results found", "no matching images", "no relevant matches",
                        "couldn't find", "未找到相关", "没有找到相关")):
                    source.update(ok=True, status="no_matches")
                else:
                    source.update(status="unparsed", code="unrecognized_search_page",
                                  error="保留 Reader 正文，但未识别到结果卡片；不能把页面图片当作命中")
        except JinaError as exc:
            source.update(status="error", code=exc.code, error=str(exc))
        except Exception:
            source.update(status="error", code="engine_error", error="该来源处理失败；原始异常已隐藏，其他来源继续返回")
        source["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return source

    def search(self, args: dict) -> dict:
        original = None
        if "source_id" in args:
            original = (self.image_lookup(args["source_id"]) if self.image_lookup is not None else
                        next((block for block in self.originals
                              if block["_user_image"]["source_id"] == args["source_id"]), None))
            if original is None:
                raise JinaError("unknown_image", "请使用最近含图用户消息的原图 source_id，或成功裁剪返回的 crop_id；不要提前编造裁剪编号")
            raw = base64.b64decode(original["data"], validate=True)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise JinaError("image_too_large", "以图搜图单张图片上限为 5 MiB")
            identity = hashlib.sha256(raw).hexdigest()
            content_type = original["mimeType"]
        else:
            url = public_url(args["url"])
            if not url.startswith("https://"):
                raise JinaError("invalid_url", "图片 URL 上传仅支持公开 HTTPS 地址")
            identity = "url:" + url
            raw, content_type = json.dumps({"url": url}).encode(), "application/json"
        # Only explicit tool calls upload. Concurrent calls share the upload, and
        # original metadata carries the URL across follow-up user turns until expiry.
        with self._lock:
            if self._bridge is None:
                self._bridge = load_bridge(self.jina.home)
            upload = self._uploads.get(identity)
            if not self._valid_upload(upload) and original is not None:
                upload = original.get("_reverse_image_upload")
            reused = self._valid_upload(upload)
            if reused and original is not None and upload["sha256"] != identity:
                reused = False
            if not reused:
                upload = self._upload(raw, content_type)
                if original is not None and upload["sha256"] != identity:
                    raise JinaError("upload_mismatch", "中转图片指纹不一致；未调用搜图引擎")
            self._uploads[identity] = upload
            if original is not None:
                original["_reverse_image_upload"] = upload.copy()
        targets = [
            "https://yandex.com/images/search?" + urlencode({"url": upload["url"], "rpt": "imageview"}),
            "https://tineye.com/api/v1/result_json/?" + urlencode({"page": args.get("page", 1), "url": upload["url"]}),
        ]
        with ThreadPoolExecutor(max_workers=len(ENGINES)) as pool:
            sources = list(pool.map(self._read_engine, [(key, name, target) for (key, name), target in zip(ENGINES, targets)]))
        matches, deduplication = aggregate_matches(sources)
        for source in sources:
            source["result_count"] = len(source.pop("matches"))
        successes = sum(source["ok"] for source in sources)
        return {"ok": successes > 0, "partial": 0 < successes < len(sources),
                "title": " / ".join(name for _, name in ENGINES) + " 混合以图搜图",
                "content": "\n".join(f'{s["engine_name"]}: {s["status"]} ({s["result_count"]})' for s in sources),
                "sources": sources, "matches": matches, "deduplication": deduplication,
                "evidence_type": "reader_extract", "untrusted_content": True,
                "query_image_url": upload["url"], "upload_expires_at": upload["expiresAt"],
                "upload_reused": reused or upload.get("reused", False),
                "source_id": args.get("source_id"), "page": args.get("page", 1)}
