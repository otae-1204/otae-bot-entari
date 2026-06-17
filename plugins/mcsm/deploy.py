"""Docker deployment helpers for the MCSM plugin."""

from __future__ import annotations

import asyncio
import json
import random
import re
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import httpx
from loguru import logger

from configs.config import _env

try:
    from .client import redact_sensitive_text
except ImportError:  # pragma: no cover - tests load this module by path.
    from plugins.mcsm.client import redact_sensitive_text


SCRIPT_PRIORITY = (
    "start.sh",
    "启动.sh",
    "run.sh",
    "server.sh",
    "startserver.sh",
    "start.command",
)

JAR_PRIORITY = (
    "server.jar",
    "paper*.jar",
    "purpur*.jar",
    "fabric-server*.jar",
)

SUSPICIOUS_LOG_PATTERNS = (
    "error",
    "exception",
    "failed",
    "crash",
    "no such file",
    "permission denied",
    "eula",
)

EULA_REMEDIATION_TEXT = "eula=true\n"


@dataclass(slots=True)
class DeployOptions:
    alias: str
    url: str
    port: int
    node: str = ""
    image: str = ""
    command: str = ""
    memory_mb: int = 2048
    dry_run: bool = False


@dataclass(slots=True)
class DeployParseResult:
    options: Optional[DeployOptions]
    errors: list[str]


def parse_deploy_args(parts: list[str]) -> DeployParseResult:
    """Parse `/mcsm deploy` tail tokens after the subcommand."""
    errors: list[str] = []
    positional: list[str] = []
    values: dict[str, str | bool] = {}
    i = 0
    while i < len(parts):
        token = parts[i]
        if token == "--dry-run":
            values["dry_run"] = True
            i += 1
            continue
        if token in {"--port", "--node", "--image", "--cmd", "--mem"}:
            if i + 1 >= len(parts):
                errors.append(f"{token} 缺少参数")
                break
            values[token[2:].replace("-", "_")] = parts[i + 1]
            i += 2
            continue
        if token.startswith("--"):
            errors.append(f"未知参数: {token}")
            i += 1
            continue
        positional.append(token)
        i += 1

    if len(positional) < 2:
        errors.append("用法: /mcsm deploy <别名> <闪传URL> [--port 宿主端口] [--node 节点] [--image 镜像] [--cmd 启动命令]")

    port_text = str(values.get("port") or "")
    if not port_text:
        port = 0
    else:
        try:
            port = int(port_text)
        except ValueError:
            errors.append("--port 必须是数字")
            port = 0
        else:
            if not 1 <= port <= 65535:
                errors.append("--port 必须在 1-65535 之间")

    mem_text = str(values.get("mem") or "2048")
    try:
        memory_mb = int(mem_text)
    except ValueError:
        errors.append("--mem 必须是数字，单位 MB")
        memory_mb = 2048
    else:
        if memory_mb < 512:
            errors.append("--mem 不能小于 512 MB")

    url = positional[1] if len(positional) >= 2 else ""
    if url and not is_valid_download_url(url):
        errors.append("闪传URL必须是 http(s) 下载链接")

    if errors:
        return DeployParseResult(None, errors)

    return DeployParseResult(
        DeployOptions(
            alias=sanitize_alias(positional[0]),
            url=url,
            port=port,
            node=str(values.get("node") or ""),
            image=str(values.get("image") or ""),
            command=str(values.get("cmd") or ""),
            memory_mb=memory_mb,
            dry_run=bool(values.get("dry_run")),
        ),
        [],
    )


def sanitize_alias(value: str) -> str:
    alias = re.sub(r"\s+", "_", value.strip())[:64]
    return alias


def apply_auto_port_alias(alias: str, port: int) -> str:
    base = re.sub(r"^\d{1,5}-", "", sanitize_alias(alias), count=1)
    if not base:
        base = "instance"
    return sanitize_alias(f"{port}-{base}")


def _valid_port(value: Any) -> int | None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _ports_from_range_text(value: Any) -> set[int]:
    ports: set[int] = set()
    for chunk in re.split(r"[,;\s]+", str(value or "")):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            start = _valid_port(match.group(1))
            end = _valid_port(match.group(2))
            if start is not None and end is not None:
                ports.update(range(min(start, end), max(start, end) + 1))
            continue
        port = _valid_port(chunk)
        if port is not None:
            ports.add(port)
    return ports


def _ports_from_template_ranges(text: str) -> set[int]:
    ports: set[int] = set()
    pattern = re.compile(r"\bparseNumberRangePair\b\s+((?:[\"'][^\"']+[\"']\s*)+)", re.IGNORECASE)
    for match in pattern.finditer(text):
        ranges = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
        if not ranges:
            continue
        # parseNumberRangePair maps local ports to remote ports; the second
        # range is the externally reachable remotePort range when present.
        ports.update(_ports_from_range_text(ranges[1] if len(ranges) >= 2 else ranges[0]))
    return ports


def is_valid_download_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def daemon_id(daemon: dict[str, Any]) -> str:
    return str(daemon.get("uuid") or daemon.get("id") or "")


def daemon_name(daemon: dict[str, Any]) -> str:
    return str(
        daemon.get("remarks")
        or daemon.get("name")
        or daemon.get("ip")
        or daemon_id(daemon)[:8]
    )


def _instance_text_values(inst: dict[str, Any]) -> list[str]:
    values: list[str] = []
    config = inst.get("config", {}) if isinstance(inst, dict) else {}
    if isinstance(config, dict):
        for key in ("nickname", "name", "instanceName", "remarks", "description"):
            value = str(config.get(key) or "").strip()
            if value:
                values.append(value)
    for key in ("nickname", "name", "instanceName", "remarks", "description"):
        value = str(inst.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def is_frp_instance(inst: dict[str, Any]) -> bool:
    return any("frp" in value.lower() for value in _instance_text_values(inst))


def find_daemon(daemons: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query = query.strip().lower()
    if not query:
        return []
    matches: list[dict[str, Any]] = []
    for daemon in daemons:
        values = {
            daemon_id(daemon).lower(),
            daemon_id(daemon)[:8].lower(),
            daemon_name(daemon).lower(),
            str(daemon.get("ip") or "").lower(),
        }
        if query in values or any(query in value for value in values if value):
            matches.append(daemon)
    return matches


def docker_image_tags(image: dict[str, Any]) -> list[str]:
    raw = (
        image.get("RepoTags")
        or image.get("repoTags")
        or image.get("tags")
        or image.get("name")
        or image.get("Names")
        or []
    )
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    return []


def java_runtime_image_label(image: dict[str, Any]) -> str:
    for tag in docker_image_tags(image):
        lowered = tag.lower()
        if not any(token in lowered for token in ("java", "jdk", "jre", "openjdk", "temurin")):
            continue
        version_match = re.search(r"(?:^|[^0-9])(8|11|17|21|22|23|24|25)(?:[^0-9]|$)", lowered)
        if not version_match:
            continue
        kind = "jre" if "jre" in lowered and "jdk" not in lowered else "jdk"
        return f"{kind}{version_match.group(1)}"
    return ""


def java_runtime_image_labels(images: Iterable[dict[str, Any]], limit: int = 4) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for image in images:
        label = java_runtime_image_label(image)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _collect_frp_ports(value: Any, *, in_allow_ports: bool = False) -> set[int]:
    ports: set[int] = set()
    if isinstance(value, dict):
        if in_allow_ports and "start" in value and "end" in value:
            start = _valid_port(value.get("start"))
            end = _valid_port(value.get("end"))
            if start is not None and end is not None:
                ports.update(range(min(start, end), max(start, end) + 1))
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized == "remoteport":
                port = _valid_port(item)
                if port is not None:
                    ports.add(port)
            elif normalized in {"allowports", "allowport"}:
                ports.update(_collect_frp_ports(item, in_allow_ports=True))
            ports.update(_collect_frp_ports(item, in_allow_ports=in_allow_ports))
    elif isinstance(value, list):
        for item in value:
            ports.update(_collect_frp_ports(item, in_allow_ports=in_allow_ports))
    elif in_allow_ports:
        ports.update(_ports_from_range_text(value))
    return ports


def extract_frp_candidate_ports(toml_text: str) -> set[int]:
    ports: set[int] = set()
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError:
        data = {}
    if data:
        ports.update(_collect_frp_ports(data))

    for match in re.finditer(r"\bremote_?port\b\s*=\s*(?:\"|')?(\d+)", toml_text, re.IGNORECASE):
        port = _valid_port(match.group(1))
        if port is not None:
            ports.add(port)
    ports.update(_ports_from_template_ranges(toml_text))
    return ports


def instance_status_code(inst: dict[str, Any]) -> int:
    status = inst.get("status", inst.get("state", -1))
    try:
        return int(status)
    except (TypeError, ValueError):
        return -1


def extract_instance_host_ports(inst: dict[str, Any]) -> set[int]:
    cfg = inst.get("config", {}) or {}
    docker = cfg.get("docker", {}) if isinstance(cfg, dict) else {}
    raw_ports = docker.get("ports") if isinstance(docker, dict) else []
    if isinstance(raw_ports, dict):
        raw_ports = list(raw_ports.values())
    if not isinstance(raw_ports, list):
        raw_ports = [raw_ports]

    ports: set[int] = set()
    for item in raw_ports:
        if isinstance(item, dict):
            for key in ("hostPort", "publicPort", "host_port", "published", "port"):
                port = _valid_port(item.get(key))
                if port is not None:
                    ports.add(port)
                    break
            continue
        text = str(item or "").split("/", 1)[0]
        parts = text.split(":")
        host_part = parts[-2] if len(parts) >= 2 else parts[0]
        port = _valid_port(host_part)
        if port is not None:
            ports.add(port)
    return ports


def running_instance_host_ports(instances: Iterable[dict[str, Any]]) -> set[int]:
    occupied: set[int] = set()
    for inst in instances:
        if instance_status_code(inst) == 3:
            occupied.update(extract_instance_host_ports(inst))
    return occupied


def choose_deploy_port(candidates: Iterable[int], occupied: Iterable[int]) -> int:
    available = sorted(set(candidates) - set(occupied))
    if not available:
        return 0
    return random.choice(available)


def find_images(images: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query = normalize_java_query(query)
    if not query:
        return []
    matches: list[dict[str, Any]] = []
    for image in images:
        tags = [tag.lower() for tag in docker_image_tags(image)]
        if any(query in tag for tag in tags):
            matches.append(image)
    return matches


def choose_default_java_images(images: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    image_list = list(images)
    for needle in ("21", "jdk21", "java21"):
        matches = [
            image for image in image_list
            if any(("java" in tag.lower() or "temurin" in tag.lower() or "openjdk" in tag.lower()) and needle in tag.lower()
                   for tag in docker_image_tags(image))
        ]
        if matches:
            return matches
    for needle in ("17", "jdk17", "java17"):
        matches = [
            image for image in image_list
            if any(("java" in tag.lower() or "temurin" in tag.lower() or "openjdk" in tag.lower()) and needle in tag.lower()
                   for tag in docker_image_tags(image))
        ]
        if matches:
            return matches
    return []


def normalize_java_query(query: str) -> str:
    lowered = query.strip().lower()
    aliases = {
        "java21": "21",
        "jdk21": "21",
        "jre21": "21",
        "java17": "17",
        "jdk17": "17",
        "jre17": "17",
    }
    return aliases.get(lowered, lowered)


def image_display_name(image: dict[str, Any]) -> str:
    tags = docker_image_tags(image)
    return tags[0] if tags else str(image.get("Id") or image.get("id") or "unknown-image")


def option_lines(items: Iterable[dict[str, Any]], label_func) -> list[str]:
    return [f"{index}. {label_func(item)}" for index, item in enumerate(items, 1)]


def parse_selection_index(text: Any, count: int) -> Optional[int]:
    if hasattr(text, "extract_plain_text"):
        text = text.extract_plain_text()
    raw = str(text or "").strip()
    match = re.search(r"\d+", raw)
    if not match:
        return None
    index = int(match.group(0))
    if 1 <= index <= count:
        return index - 1
    return None


def file_name(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip().replace("\\", "/").split("/")[-1]
    if not isinstance(entry, dict):
        return ""
    return str(
        entry.get("name")
        or entry.get("fileName")
        or entry.get("filename")
        or entry.get("file_name")
        or entry.get("basename")
        or entry.get("title")
        or entry.get("path")
        or entry.get("target")
        or entry.get("absolutePath")
        or entry.get("realName")
        or entry.get("real_name")
        or entry.get("displayName")
        or ""
    ).strip().replace("\\", "/").split("/")[-1]


def file_path(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip().replace("\\", "/")
    if not isinstance(entry, dict):
        return ""
    value = str(
        entry.get("path")
        or entry.get("target")
        or entry.get("absolutePath")
        or entry.get("name")
        or entry.get("fileName")
        or entry.get("filename")
        or entry.get("file_name")
        or entry.get("basename")
        or entry.get("realName")
        or entry.get("real_name")
        or entry.get("displayName")
        or ""
    ).strip().replace("\\", "/")
    return value


def _entry_instance_path(entry: Any, target: str) -> str:
    name = file_name(entry)
    raw = file_path(entry).strip("/")
    base = str(target or "/").strip("/")
    if not raw or raw == name:
        raw = f"{base}/{name}" if base else name
    elif base and "/" not in raw and raw != base:
        raw = f"{base}/{raw}"
    return "/" + raw.strip("/")


def is_directory(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in ("isDirectory", "isDir", "directory", "is_dir"):
        if key in entry:
            return bool(entry.get(key))
    if entry.get("type") == 0:
        return True
    value = str(entry.get("type") if entry.get("type") is not None else entry.get("mime") or "").strip().lower()
    return value in {"directory", "dir", "folder"}


async def find_instance_toml_paths(
    client: Any,
    uuid: str,
    daemon_id_value: str,
    *,
    max_depth: int = 4,
    max_dirs: int = 80,
    max_toml: int = 80,
    pages: int = 5,
    page_size: int = 200,
) -> list[str]:
    """Find TOML files in an MCSM instance with bounded breadth-first search."""
    targets: list[str] = []
    seen_targets: set[str] = set()
    seen_dirs: set[str] = set()
    queue: list[tuple[str, int]] = [("/", 0)]

    while queue and len(seen_dirs) < max_dirs and len(targets) < max_toml:
        target, depth = queue.pop(0)
        target = "/" + target.strip("/")
        if target == "//":
            target = "/"
        if target in seen_dirs:
            continue
        seen_dirs.add(target)

        for page in range(max(1, pages)):
            try:
                files = await client.list_instance_files(
                    uuid,
                    daemon_id_value,
                    target=target,
                    page=page,
                    page_size=page_size,
                )
            except Exception as exc:
                logger.debug(f"[MCSM] 读取实例目录 {target} 第 {page} 页失败: {type(exc).__name__}: {exc}")
                break
            if not files:
                break

            for entry in files:
                name = file_name(entry)
                if not name:
                    continue
                entry_path = _entry_instance_path(entry, target)
                if is_directory(entry):
                    if depth < max_depth and len(seen_dirs) + len(queue) < max_dirs:
                        queue.append((entry_path, depth + 1))
                    continue
                if name.lower().endswith(".toml") and entry_path not in seen_targets:
                    targets.append(entry_path)
                    seen_targets.add(entry_path)
                    if len(targets) >= max_toml:
                        break
            if len(targets) >= max_toml:
                break

    return targets


def summarize_file_entries(files: Iterable[Any], limit: int = 30) -> str:
    names = [file_name(entry) for entry in files]
    names = [name for name in names if name]
    if not names:
        return "未返回文件列表"
    shown = names[:limit]
    suffix = f" 等 {len(names)} 项" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def detect_start_command(
    files: Iterable[dict[str, Any]],
    memory_mb: int = 2048,
    explicit_command: str = "",
    base_path: str = "",
) -> tuple[str, str]:
    if explicit_command.strip():
        return explicit_command.strip(), "用户指定"

    base_path = base_path.strip().strip("/")

    def with_base(name: str) -> str:
        return f"{base_path}/{name}" if base_path else name

    names = [file_name(entry) for entry in files]
    lowered = {name.lower(): name for name in names if name}
    for script in SCRIPT_PRIORITY:
        found = lowered.get(script.lower())
        if found:
            return f"sh ./{shell_quote(with_base(found))}", f"启动脚本 {with_base(found)}"

    for pattern in JAR_PRIORITY:
        regex = re.compile("^" + re.escape(pattern).replace("\\*", ".*") + "$", re.IGNORECASE)
        for name in names:
            if regex.match(name):
                return f"java -Xms1G -Xmx{int(memory_mb)}M -jar {shell_quote(with_base(name))} nogui", f"Jar 文件 {with_base(name)}"

    summary = summarize_file_entries(files)
    jsr_files = [name for name in names if name.lower().endswith(".jsr")]
    if jsr_files:
        return "", f"未找到启动脚本或 Jar 文件；发现 {jsr_files[0]}，是否应为 server.jar；已扫描: {summary}"

    bat_files = [name for name in names if name.lower().endswith(".bat")]
    if bat_files:
        return "", f"发现 Windows 启动脚本 {bat_files[0]}，Docker Linux 环境不能直接执行 .bat；请提供 --cmd 或上传 start.sh/server.jar；已扫描: {summary}"

    return "", f"未找到启动脚本或 Jar 文件；已扫描: {summary}"


def _archive_entries_from_zip(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename.strip().replace("\\", "/").strip("/")
            if not name or name.startswith("__MACOSX/"):
                continue
            entries.append({"path": name, "name": name.split("/")[-1], "isDirectory": info.is_dir()})
    return entries


def _archive_entries_from_tar(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            name = member.name.strip().replace("\\", "/").strip("/")
            if not name:
                continue
            entries.append({"path": name, "name": name.split("/")[-1], "isDirectory": member.isdir()})
    return entries


def _archive_entries(path: Path) -> list[dict[str, Any]]:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if path.suffix.lower() == ".zip":
        return _archive_entries_from_zip(path)
    if path.suffix.lower() in {".tar", ".tgz"} or suffixes[-2:] in ([".tar", ".gz"], [".tar", ".xz"], [".tar", ".bz2"]):
        return _archive_entries_from_tar(path)
    return []


def _entries_at_depth(entries: Iterable[dict[str, Any]], depth: int, prefix: str = "") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    prefix = prefix.strip("/")
    for entry in entries:
        path = file_path(entry).strip("/")
        if not path:
            continue
        if prefix:
            if not path.startswith(prefix + "/"):
                continue
            relative = path[len(prefix) + 1:]
        else:
            relative = path
        if not relative or "/" in relative.strip("/"):
            continue
        result.append({**entry, "name": relative.strip("/")})
    return result


def detect_archive_start_command(
    archive_path: str | Path,
    memory_mb: int = 2048,
    explicit_command: str = "",
) -> tuple[str, str]:
    """Detect the startup command from a local archive's file list."""
    if explicit_command.strip():
        return explicit_command.strip(), "用户指定"

    path = Path(archive_path)
    try:
        entries = _archive_entries(path)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        logger.warning("[MCSM] 读取压缩包目录失败: {}: {}", type(exc).__name__, exc)
        return "", f"压缩包目录读取失败: {type(exc).__name__}"
    if not entries:
        return "", "压缩包格式暂不支持本地识别"

    root_files = _entries_at_depth(entries, 1)
    command, source = detect_start_command(root_files, memory_mb, "")
    if command:
        return command, f"压缩包根目录 {source}"

    directories: list[str] = []
    for entry in entries:
        path_text = file_path(entry).strip("/")
        if not path_text or "/" not in path_text:
            continue
        directory = path_text.split("/", 1)[0]
        if directory not in directories:
            directories.append(directory)
    for directory in directories[:20]:
        child_files = _entries_at_depth(entries, 1, prefix=directory)
        command, source = detect_start_command(child_files, memory_mb, "", base_path=directory)
        if command:
            return command, f"压缩包子目录 {source}"

    return "", f"压缩包中未找到启动脚本或 Jar 文件；已扫描: {summarize_file_entries(root_files)}"


def apply_archive_start_fallback(
    start_command: str,
    start_source: str,
    archive_start_command: str,
    archive_start_source: str,
    api_label: str = "API 扫描结果",
) -> tuple[str, str, bool]:
    """Use archive-detected command when API scanning did not produce one."""
    if start_command or not archive_start_command:
        return start_command, start_source, False
    return archive_start_command, f"{archive_start_source}；{api_label}: {start_source}", True


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._/@+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def extract_created_instance_uuid(data: dict[str, Any]) -> str:
    candidates: list[Any] = [data]
    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
        for key in ("instance", "config", "info"):
            if isinstance(nested.get(key), dict):
                candidates.append(nested[key])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("instanceUuid", "uuid", "id"):
            value = item.get(key)
            if value:
                return str(value)
    return ""


def redact_deploy_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): redact_deploy_summary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_deploy_summary(item) for item in value]
    if isinstance(value, tuple):
        return [redact_deploy_summary(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def log_looks_suspicious(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in SUSPICIOUS_LOG_PATTERNS)


def needs_eula_remediation(error: str = "", log_text: str = "") -> bool:
    """Return True when startup failure is likely caused by Minecraft EULA."""
    text = f"{error}\n{log_text}".lower()
    if "eula" in text:
        return True
    return "handle stopped" in text and not log_text.strip()


def remediation_summary(actions: Iterable[str]) -> str:
    items = [str(action).strip() for action in actions if str(action).strip()]
    return "；".join(items)


def is_extract_gateway_timeout_error(error: Any) -> bool:
    text = str(error or "").lower()
    return (
        "http 504" in text
        or "gateway time-out" in text
        or "gateway timeout" in text
        or "504 gateway" in text
    )


def is_upload_permission_error(error: Any) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "access denied",
            "permission denied",
            "no file found",
            "eacces",
            "eperm",
        )
    )


def is_permission_repair_instance(instance: Any) -> bool:
    if not isinstance(instance, dict):
        return False
    names = [
        str(instance.get("nickname") or ""),
        str(instance.get("instanceName") or ""),
        str(instance.get("name") or ""),
        str(instance.get("remarks") or ""),
    ]
    config = instance.get("config")
    if isinstance(config, dict):
        names.extend(
            [
                str(config.get("nickname") or ""),
                str(config.get("name") or ""),
                str(config.get("remarks") or ""),
            ]
        )
    lowered = " ".join(names).lower()
    return "0-aaa" in lowered and ("权限" in lowered or "permission" in lowered)


async def detect_deploy_start_command(
    client: Any,
    uuid: str,
    daemon_id_value: str,
    memory_mb: int,
    explicit_command: str,
) -> tuple[str, str]:
    files = await client.list_instance_files(uuid, daemon_id_value, target="/", page=0, page_size=200)
    command, source = detect_start_command(files, memory_mb, explicit_command)
    if command or explicit_command.strip():
        return command, source

    directories: list[tuple[str, str]] = []
    for entry in files:
        if not is_directory(entry):
            continue
        name = file_name(entry)
        path = file_path(entry) or name
        path = "/" + path.strip("/")
        if name and path not in {"/.", "/.."}:
            directories.append((name, path))

    for directory_name, directory_path in directories[:20]:
        child_files = await client.list_instance_files(uuid, daemon_id_value, target=directory_path, page=0, page_size=200)
        command, source = detect_start_command(child_files, memory_mb, "", base_path=directory_name)
        if command:
            return command, f"{source} ({directory_name}/)"

    root_summary = summarize_file_entries(files)
    directory_summary = ", ".join(name for name, _path in directories[:20]) or "无子目录"
    return "", f"未找到启动脚本或 Jar 文件；根目录已扫描: {root_summary}；子目录: {directory_summary}"


async def wait_for_deploy_start_command(
    client: Any,
    uuid: str,
    daemon_id_value: str,
    memory_mb: int,
    explicit_command: str,
    wait_seconds: int = 180,
    interval_seconds: int = 5,
    sleep_func: Any = None,
) -> tuple[str, str]:
    sleep = sleep_func or asyncio.sleep
    interval = max(1, int(interval_seconds or 5))
    attempts = max(1, int(wait_seconds or 0) // interval + 1)
    last_source = "后台解压尚未完成"
    for index in range(attempts):
        command, source = await detect_deploy_start_command(
            client,
            uuid,
            daemon_id_value,
            memory_mb,
            explicit_command,
        )
        if command:
            return command, source
        last_source = source
        if index + 1 < attempts:
            await sleep(interval)
    return "", f"后台解压等待超时: {last_source}"


def llm_config() -> Optional[dict[str, str]]:
    api_key = str(_env("LLM_API_KEY", "") or _env("STEAM_LLM_API_KEY", "") or "")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": str(_env("LLM_BASE_URL", "") or _env("STEAM_LLM_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com"),
        "model": str(_env("LLM_MODEL", "") or _env("STEAM_LLM_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash"),
    }


async def diagnose_deploy_failure(
    stage: str,
    error: str,
    log_text: str = "",
    summary: Optional[dict[str, Any]] = None,
) -> str:
    config = llm_config()
    if not config:
        return ""

    payload = {
        "task": "Diagnose a Minecraft Java Docker deployment failure on MCSManager.",
        "rules": [
            "Answer in Chinese.",
            "Give 3 to 5 concrete checks or fixes.",
            "Do not suggest deleting the instance unless the user explicitly asks.",
            "Do not reveal or ask for API keys.",
        ],
        "stage": stage,
        "error": redact_sensitive_text(error)[:1000],
        "log": redact_sensitive_text(log_text)[:3000],
        "summary": redact_deploy_summary(summary or {}),
    }
    body = {
        "model": config["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You diagnose MCSManager Docker Minecraft server deployment failures."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            resp = await client.post(
                f"{config['base_url'].rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[MCSM] LLM deploy diagnosis failed: {type(exc).__name__}: {redact_sensitive_text(exc)}")
        return ""
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
