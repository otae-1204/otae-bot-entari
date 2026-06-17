"""MCSManager 面板插件 - 每群独立面板绑定 + 实例管理与权限。"""

from __future__ import annotations

import asyncio as _asyncio
import os
import re
import shlex
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.entari_native import listen_message, prompt
from arclet.entari import Account as Bot, Event
from utils.entari_native import Pred
from loguru import logger
from arclet.letoderea.exceptions import _ExitException
from utils.entari_native import (
    ChainMsg, At, ArgVal, SendDest, event_plain_text, event_user_id,
    make_image as ChainImage, account_adapter_name, stop_session,
)

from arclet.alconna import Args, MultiVar
from nepattern import AnyString
from utils.entari_native import cmd_with_args as _cmd, get_rest
from utils.temp_files import schedule_temp_file_cleanup

from configs.config import Config as GlobalConfig

from .client import MCSMClient, OPERATION_NAMES, STATUS_MAP, STATUS_EMOJI
from .client import redact_sensitive_text as redact_mcsm_sensitive_text
from .deploy import (
    DeployOptions,
    apply_auto_port_alias,
    apply_archive_start_fallback,
    choose_deploy_port,
    daemon_id,
    daemon_name,
    detect_archive_start_command,
    detect_deploy_start_command,
    diagnose_deploy_failure,
    EULA_REMEDIATION_TEXT,
    extract_frp_candidate_ports,
    extract_created_instance_uuid,
    find_instance_toml_paths,
    find_daemon,
    find_images,
    image_display_name,
    is_frp_instance,
    is_extract_gateway_timeout_error,
    is_permission_repair_instance,
    is_upload_permission_error,
    java_runtime_image_labels,
    log_looks_suspicious,
    needs_eula_remediation,
    option_lines,
    parse_deploy_args,
    parse_selection_index,
    remediation_summary,
    running_instance_host_ports,
    choose_default_java_images,
    redact_sensitive_text,
    wait_for_deploy_start_command,
)
from .qflash import (
    QFlashArchive,
    QFlashError,
    download_qflash_archive,
    is_qflash_url,
    preflight_qflash_archive,
    qflash_archive_label,
    qflash_download_url_candidates,
    qflash_download_url,
    refresh_qflash_archive,
    resolve_qflash_archives,
    safe_archive_filename,
)
from .draw import (
    draw_admin_list,
    draw_bind_result,
    draw_console_output,
    draw_error,
    draw_notice,
    draw_panel_overview,
    draw_status,
    extract_command_output,
    merge_status_detail,
    render_console_text,
    status_summary,
)
from .store import MCSMStore

# 持久化存储
_store = MCSMStore()

# 客户端缓存（per-group）
_clients: Dict[str, MCSMClient] = {}
_pending_bind_sessions: Dict[str, dict[str, Any]] = {}
LOG_DEFAULT_ENTRIES = 10
LOG_MAX_ENTRIES = 200
LOG_USAGE = "用法: /mcsm log <别名> [-a | -n 数量]"

# 超级用户
SUPERUSERS: List[str] = list(GlobalConfig.SUPERUSERS) if GlobalConfig.SUPERUSERS else []


def _is_superuser(user_id: str) -> bool:
    return str(user_id) in [str(u) for u in SUPERUSERS]


def get_client(group_id: str) -> MCSMClient | None:
    """获取群对应的 MCSM 客户端（按 group_id 缓存）。"""
    gid = str(group_id)
    if gid in _clients:
        return _clients[gid]
    panel = _store.get_panel(gid)
    if not panel:
        return None
    url, api_key = panel
    client = MCSMClient(panel_url=url, api_key=api_key)
    _clients[gid] = client
    return client


def _clear_client(group_id: str) -> None:
    _clients.pop(str(group_id), None)


# helpers

def _get_group_id(event: Event) -> str:
    guild = getattr(event, "guild", None)
    return str(guild.id) if (guild and guild.id) else ""


def _get_user_id(event: Event) -> str:
    return str(event_user_id(event))


def _mcsm_status_code(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _mcsm_status_text(value: object) -> str:
    status = _mcsm_status_code(value)
    name = STATUS_MAP.get(status, f"UNKNOWN({value})")
    emoji = STATUS_EMOJI.get(status, "❓")
    return f"{emoji} {name}"


async def _extract_at_users(event: Event, bot: Bot) -> List[str]:
    """从消息段中提取被 @ 的用户 ID。"""
    users: List[str] = []
    try:
        msg = await ChainMsg.generate(event=event, bot=bot)
        for seg in msg:
            if isinstance(seg, At):
                uid = getattr(seg, "target", "") or getattr(seg, "user_id", "")
                if uid:
                    users.append(str(uid))
    except Exception:
        pass
    return users


GROUP_ADMIN_ROLE_TOKENS = {
    "admin",
    "administrator",
    "owner",
    "manager",
    "群主",
    "管理员",
}


def _member_has_group_admin_role(member: object) -> bool:
    roles = getattr(member, "roles", None) or []
    for role in roles:
        for value in (
            str(getattr(role, "id", "") or ""),
            str(getattr(role, "name", "") or ""),
        ):
            lowered = value.strip().lower()
            if not lowered:
                continue
            if lowered in GROUP_ADMIN_ROLE_TOKENS:
                return True
            if any(token in lowered for token in GROUP_ADMIN_ROLE_TOKENS):
                return True
    return False


async def _is_group_manager(bot: Bot, event: Event, group_id: str, user_id: str) -> bool:
    if _is_superuser(user_id) or _store.is_admin(group_id, user_id):
        return True
    member = getattr(event, "member", None)
    if member is not None and _member_has_group_admin_role(member):
        return True
    getter = getattr(bot, "guild_member_get", None)
    if callable(getter) and group_id:
        try:
            member = await getter(guild_id=group_id, user_id=str(user_id))
        except Exception as exc:
            logger.debug(f"[MCSM] 获取群成员角色失败: {exc}")
        else:
            if member is not None and _member_has_group_admin_role(member):
                return True
    return False


async def _require_group_manager(bot: Bot, event: Event, group_id: str, user_id: str) -> Optional[str]:
    if await _is_group_manager(bot, event, group_id, user_id):
        return None
    return "仅本群 MCSM 管理员、QQ 群管理员、群主或 SUPERUSER 可执行此操作"


async def _can_view_instance(bot: Bot, event: Event, group_id: str, alias: str, user_id: str) -> bool:
    """隐藏实例仅群级管理员可见。"""
    inst = _store.get_instance(group_id, alias)
    if not inst or not inst.get("hidden"):
        return True
    return await _is_group_manager(bot, event, group_id, user_id)


def _help_tip() -> str:
    return "使用 /help mcsm 查看 MCSM 插件帮助"


def _to_image_segment(output: BytesIO) -> ChainImage:
    """Write PNG bytes to a temporary file and return a Satori-compatible image segment."""
    output.seek(0)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(output.getvalue())
        f.flush()
        schedule_temp_file_cleanup(f.name)
        return ChainImage(path=f.name)


async def _finish_image_or_text(matcher, output: BytesIO, fallback: str) -> None:
    try:
        message = ChainMsg([_to_image_segment(output)])
    except Exception as exc:
        logger.warning(f"[MCSM] render image failed, fallback to text: {exc}")
        await matcher.finish(fallback)
        return
    await matcher.finish(message)


def _private_target(bot: Bot, user_id: str) -> SendDest:
    return SendDest(
        user_id, "", False, True, "",
        account_adapter_name(bot),
    )


async def _finish_dm_image_or_text(matcher, bot: Bot, user_id: str, output: BytesIO, fallback: str) -> None:
    target = _private_target(bot, user_id)
    try:
        message = ChainMsg([_to_image_segment(output)])
    except Exception as exc:
        logger.warning(f"[MCSM] render DM image failed, fallback to text: {exc}")
        await ChainMsg.text(fallback).send()
        stop_session()
        return
    try:
        await message.send(target, bot)
    except Exception as exc:
        logger.warning(f"[MCSM] send DM image failed, fallback to text: {exc}")
        await ChainMsg.text(fallback).send()
        stop_session()
        return
    stop_session()


async def _finish_notice(matcher, title: str, lines: List[str] | tuple[str, ...], level: str = "info") -> None:
    fallback = title
    if lines:
        fallback += "\n" + "\n".join(lines)
    await _finish_image_or_text(matcher, draw_notice(title, lines, level=level), fallback)


async def _finish_error(matcher, message: str) -> None:
    await _finish_image_or_text(matcher, draw_error(message), message)


async def _finish_dm_notice(
    matcher,
    bot: Bot,
    user_id: str,
    title: str,
    lines: List[str] | tuple[str, ...],
    level: str = "info",
) -> None:
    fallback = title
    if lines:
        fallback += "\n" + "\n".join(lines)
    await _finish_dm_image_or_text(matcher, bot, user_id, draw_notice(title, lines, level=level), fallback)


# DM API Key 接收处理器

async def _is_dm_for_pending_key(event: Event) -> bool:
    """检查是否为 DM 消息，且发件人有待确认的 API Key 请求。"""
    uid = str(event_user_id(event))
    if not _store.get_pending_key(uid):
        return False
    guild = getattr(event, "guild", None)
    if guild and guild.id:
        return False
    return True


dm_key_handler = listen_message(rule=Pred(_is_dm_for_pending_key), priority=5, block=True)


@dm_key_handler.handle()
async def handle_dm_key(event: Event, bot: Bot):
    """接收用户私聊发来的 API Key。"""
    user_id = str(event_user_id(event))
    pending = _store.get_pending_key(user_id)
    if not pending:
        stop_session()
        return

    group_id, panel_url = pending
    api_key = event_plain_text(event).strip()

    # 取消绑定
    if api_key in ("取消", "cancel", "Cancel", "q", "Q"):
        _store.clear_pending_key(user_id)
        _store.clear_panel(group_id)
        await _finish_dm_notice(dm_key_handler, bot, user_id, "已取消 MCSM 面板绑定", (), "warning")
        return

    # 去除可能的前缀
    for prefix in ("api key:", "apikey:", "key:", "密钥:", "API Key:"):
        if api_key.lower().startswith(prefix.lower()):
            api_key = api_key[len(prefix):].strip()

    if not api_key or len(api_key) < 8:
        await _finish_dm_notice(
            dm_key_handler,
            bot,
            user_id,
            "API Key 格式似乎不正确",
            ("API Key 太短，请重新发送完整 Key。", "回复“取消”可中止绑定。"),
            "warning",
        )
        return

    # 验证 Key 是否有效
    await ChainMsg.text("正在验证 API Key...").send()
    test_client = MCSMClient(panel_url=panel_url, api_key=api_key)
    try:
        daemons = await test_client.get_daemon_list()
    except Exception as e:
        await _finish_dm_notice(
            dm_key_handler,
            bot,
            user_id,
            "API Key 验证失败",
            (str(e), "请检查 Key 后重新发送。"),
            "error",
        )
        return

    if not daemons:
        await _finish_dm_notice(
            dm_key_handler,
            bot,
            user_id,
            "面板没有可用节点",
            ("请检查面板地址是否正确。", "API Key 未保存，请重新发送正确 Key，或回复“取消”。"),
            "warning",
        )
        return

    # 保存 API Key
    _store.set_api_key(group_id, api_key)
    _store.clear_pending_key(user_id)
    _clear_client(group_id)

    node_count = len(daemons)
    await _finish_dm_notice(
        dm_key_handler,
        bot,
        user_id,
        "MCSM 面板绑定成功",
        (f"面板: {panel_url}", f"可用节点: {node_count}", "可在群内使用 /mcsm bind <节点ID> 选择绑定实例。"),
        "success",
    )


# 主命令入口

async def _is_dm_for_pending_bind(event: Event) -> bool:
    uid = str(event_user_id(event))
    if _store.get_pending_key(uid):
        return False
    _clear_expired_bind_sessions()
    if uid not in _pending_bind_sessions:
        return False
    guild = getattr(event, "guild", None)
    if guild and guild.id:
        return False
    return True


dm_bind_handler = listen_message(rule=Pred(_is_dm_for_pending_bind), priority=6, block=True)


def _instance_match_values(inst: dict) -> set[str]:
    cfg = inst.get("config", {}) or {}
    values = {
        _instance_display_name(inst),
        str(inst.get("instanceName") or ""),
        str(inst.get("name") or ""),
        str(cfg.get("nickname") or ""),
        _instance_uuid(inst),
        _instance_uuid(inst)[:8],
    }
    return {value.lower() for value in values if value}


def _find_candidate_by_token(instances: List[dict], token: str) -> tuple[int, dict] | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(instances):
            return index, instances[index - 1]
        return None
    lowered = token.lower()
    for index, inst in enumerate(instances, 1):
        if lowered in _instance_match_values(inst):
            return index, inst
    return None


def _parse_bind_selection(text: str, instances: List[dict]) -> tuple[list[tuple[int, dict, str]], list[str]]:
    selected: list[tuple[int, dict, str]] = []
    errors: list[str] = []
    for raw_line in text.replace(",", " ").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if any("=" in part for part in parts):
            for part in parts:
                if "=" not in part:
                    match = _find_candidate_by_token(instances, part)
                    if match:
                        selected.append((match[0], match[1], _default_alias(match[1])))
                    else:
                        errors.append(f"未找到 {part}")
                    continue
                left, alias = part.split("=", 1)
                match = _find_candidate_by_token(instances, left.strip())
                if match:
                    selected.append((match[0], match[1], alias.strip() or _default_alias(match[1])))
                else:
                    errors.append(f"未找到 {left.strip()}")
            continue
        if parts[0].isdigit() and len(parts) > 1 and not all(part.isdigit() for part in parts):
            match = _find_candidate_by_token(instances, parts[0])
            if match:
                selected.append((match[0], match[1], " ".join(parts[1:]).strip() or _default_alias(match[1])))
            else:
                errors.append(f"未找到 {parts[0]}")
            continue
        for part in parts:
            match = _find_candidate_by_token(instances, part)
            if match:
                selected.append((match[0], match[1], _default_alias(match[1])))
            else:
                errors.append(f"未找到 {part}")
    return selected, errors


def _split_command_text(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


async def _prompt_select(title: str, items: list[Any], label_func) -> Any | None:
    lines = [title, *option_lines(items, label_func), "请回复编号，输入 cancel 取消。"]
    answer = await prompt("\n".join(lines), timeout=60)
    plain = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    if plain.strip().lower() in {"cancel", "q", "取消"}:
        return None
    index = parse_selection_index(answer, len(items))
    if index is None:
        return None
    return items[index]


async def _resolve_deploy_download(options: DeployOptions, summary: dict[str, Any]) -> tuple[str, str, QFlashArchive | None]:
    if not is_qflash_url(options.url):
        summary["package"] = "direct URL"
        return options.url, "server package", None

    archives = await resolve_qflash_archives(options.url)
    if len(archives) == 1:
        selected = archives[0]
    else:
        selected = await _prompt_select("闪传内有多个压缩包，请选择用于部署的服务器压缩包：", archives, qflash_archive_label)
        if selected is None:
            raise QFlashError("已取消闪传压缩包选择")

    assert isinstance(selected, QFlashArchive)
    summary["package"] = qflash_archive_label(selected)
    summary["package_size"] = qflash_archive_label(selected)
    if selected.expired_time:
        summary["package_expired_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(selected.expired_time))
    await preflight_qflash_archive(selected)
    fresh = await _refresh_qflash_download(options.url, selected, summary)
    return qflash_download_url(fresh), fresh.name, fresh


def _large_package_threshold_bytes() -> int:
    raw = os.getenv("MCSM_DEPLOY_LARGE_PACKAGE_MB", "200")
    try:
        mb = int(str(raw).strip())
    except ValueError:
        mb = 200
    return max(1, mb) * 1024 * 1024


def _is_large_qflash_archive(item: QFlashArchive | None) -> bool:
    return bool(item and int(item.size or 0) >= _large_package_threshold_bytes())


async def _refresh_qflash_download(source_url: str, selected: QFlashArchive, summary: dict[str, Any]) -> QFlashArchive:
    fresh = await refresh_qflash_archive(source_url, selected)
    summary["qflash_refresh_count"] = int(summary.get("qflash_refresh_count") or 0) + 1
    return fresh


def _daemon_label(daemon: dict) -> str:
    images = daemon.get("_dockerImages") or []
    image_preview = ", ".join(java_runtime_image_labels(images))
    suffix = f" | JDK/JRE: {image_preview or '无'}"
    return f"{daemon_name(daemon)} ({daemon_id(daemon)[:8]}){suffix}"


async def _select_deploy_daemon(client: MCSMClient, options: DeployOptions) -> dict | None:
    daemons = await client.list_supported_docker_daemons()
    if not daemons:
        await _finish_notice(
            mcsm,
            "没有可用于 Docker 部署的节点",
            ("请检查 daemon 是否在线、Docker 是否安装、MCSM daemon 是否有 Docker 权限。",),
            "warning",
        )
        return None
    if options.node:
        matches = find_daemon(daemons, options.node)
        if not matches:
            await _finish_notice(mcsm, "未找到匹配的 Docker 节点", (f"节点关键词: {options.node}",), "warning")
            return None
        if len(matches) == 1:
            return matches[0]
        selected = await _prompt_select("匹配到多个 Docker 节点，请选择：", matches, _daemon_label)
        if selected is None:
            await _finish_notice(mcsm, "已取消 Docker 节点选择", (), "warning")
        return selected
    if len(daemons) == 1:
        return daemons[0]
    selected = await _prompt_select("请选择用于部署的 Docker 节点：", daemons, _daemon_label)
    if selected is None:
        await _finish_notice(mcsm, "已取消 Docker 节点选择", (), "warning")
    return selected


async def _select_deploy_image(client: MCSMClient, daemon: dict, options: DeployOptions) -> dict | None:
    did = daemon_id(daemon)
    images = list(daemon.get("_dockerImages") or [])
    if not images:
        images = await client.list_docker_images(did)
    if options.image:
        matches = find_images(images, options.image)
        if not matches:
            await _finish_notice(mcsm, "未找到匹配的 Docker 镜像", (f"镜像关键词: {options.image}",), "warning")
            return None
    else:
        matches = choose_default_java_images(images)
        if not matches:
            await _finish_notice(
                mcsm,
                "目标节点没有可用 Java 镜像",
                ("请先在面板拉取 Java 21 或 Java 17 镜像，或使用 --image 指定已有镜像。",),
                "warning",
            )
            return None
    if len(matches) == 1:
        return matches[0]
    selected = await _prompt_select("请选择 Docker 镜像：", matches, image_display_name)
    if selected is None:
        await _finish_notice(mcsm, "已取消 Docker 镜像选择", (), "warning")
    return selected


async def _toml_paths_for_instance(client: MCSMClient, uuid: str, daemon_id_value: str) -> list[str]:
    return await find_instance_toml_paths(client, uuid, daemon_id_value)


async def _frp_candidate_ports_for_instance(client: MCSMClient, uuid: str, daemon_id_value: str) -> tuple[set[int], list[str]]:
    ports: set[int] = set()
    targets = await _toml_paths_for_instance(client, uuid, daemon_id_value)
    for target in targets:
        try:
            content = await client.read_instance_file(uuid, daemon_id_value, target)
        except Exception as exc:
            logger.debug(f"[MCSM] 读取 FRP TOML {target} 失败: {type(exc).__name__}: {exc}")
            continue
        ports.update(extract_frp_candidate_ports(content))
    return ports, targets


async def _auto_deploy_port(client: MCSMClient, daemon_id_value: str) -> tuple[int, str]:
    instances = await client.get_daemon_instances(daemon_id_value)
    frp_instances = [
        inst for inst in instances
        if is_frp_instance(inst)
    ]
    if not frp_instances:
        raise RuntimeError("未指定 --port，且当前节点未找到名称包含 Frp 的实例，无法自动分配端口")

    candidates: set[int] = set()
    toml_paths: list[str] = []
    parsed_toml_paths: list[str] = []
    for inst in frp_instances:
        uuid = _instance_uuid(inst)
        if uuid:
            ports, paths = await _frp_candidate_ports_for_instance(client, uuid, daemon_id_value)
            candidates.update(ports)
            toml_paths.extend(paths)
            if ports:
                parsed_toml_paths.extend(paths)
    occupied = running_instance_host_ports(instances)
    if not candidates:
        instance_names = ", ".join(_instance_display_name(inst) for inst in frp_instances[:5])
        if len(frp_instances) > 5:
            instance_names += f" 等 {len(frp_instances)} 个"
        path_summary = "未扫描到 TOML" if not toml_paths else ", ".join(toml_paths[:5])
        if len(toml_paths) > 5:
            path_summary += f" 等 {len(toml_paths)} 个"
        raise RuntimeError(
            "未指定 --port，FRP 实例的 TOML 中未找到 remotePort/allowPorts/parseNumberRangePair 可用端口；"
            f"FRP 实例: {instance_names or '未知'}；TOML: {path_summary}；"
            f"候选端口: 0；当前节点运行中占用端口: {len(occupied)} 个"
        )

    port = choose_deploy_port(candidates, occupied)
    if not port:
        raise RuntimeError(
            "未指定 --port，FRP 候选端口均已被当前节点运行中的实例占用；"
            f"候选 {len(candidates)} 个，占用 {len(candidates & occupied)} 个"
        )
    source_paths = parsed_toml_paths or toml_paths
    source = ", ".join(source_paths[:3]) if source_paths else "未记录"
    if len(source_paths) > 3:
        source += f" 等 {len(source_paths)} 个"
    return port, f"FRP 自动分配: {port}，候选 {len(candidates)} 个，已占用 {len(candidates & occupied)} 个，TOML: {source}"


async def _finish_deploy_failure(
    stage: str,
    error: str,
    summary: dict[str, Any],
    log_text: str = "",
) -> None:
    lines = [
        f"阶段: {stage}",
        f"错误: {redact_sensitive_text(error)}",
    ]
    if summary.get("uuid"):
        lines.append("已保留现场，可在 MCSM 面板继续排查。")
    else:
        lines.append("未创建实例，请修正后重新执行部署。")
    if summary.get("transfer_package"):
        lines.append(f"中转压缩包: {redact_mcsm_sensitive_text(summary['transfer_package'])}")
    if summary.get("transfer_status"):
        lines.append(f"中转状态: {redact_mcsm_sensitive_text(summary['transfer_status'])}")
    if summary.get("extract_status"):
        lines.append(f"解压状态: {redact_mcsm_sensitive_text(summary['extract_status'])}")
    if summary.get("cleanup_status"):
        lines.append(f"清理状态: {redact_mcsm_sensitive_text(summary['cleanup_status'])}")
    if summary.get("deploy_cleanup_status"):
        lines.append(f"失败清理: {redact_mcsm_sensitive_text(summary['deploy_cleanup_status'])}")
    if summary.get("upload_repair_instance"):
        lines.append(f"权限修复实例: {redact_mcsm_sensitive_text(summary['upload_repair_instance'])}")
    if summary.get("upload_repair_status"):
        lines.append(f"权限修复状态: {redact_mcsm_sensitive_text(summary['upload_repair_status'])}")
    if summary.get("large_package_strategy"):
        lines.append(f"大包策略: {redact_mcsm_sensitive_text(summary['large_package_strategy'])}")
    if summary.get("large_package_remote_retry"):
        lines.append(f"大包远程重试: {redact_mcsm_sensitive_text(summary['large_package_remote_retry'])}")
    if summary.get("qflash_refresh_count"):
        lines.append(f"闪传直链刷新: {summary['qflash_refresh_count']} 次")
    if summary.get("remote_install_url_variant"):
        lines.append(f"直链候选: {redact_mcsm_sensitive_text(summary['remote_install_url_variant'])}")
    if summary.get("remote_install_retry_status"):
        lines.append(f"远程直链安装: {redact_mcsm_sensitive_text(summary['remote_install_retry_status'])}")
    advice = await diagnose_deploy_failure(stage, error, log_text=log_text, summary=summary)
    if advice:
        lines.append("LLM 诊断建议:")
        lines.extend(advice.splitlines()[:8])
    await _finish_notice(mcsm, "MCSM Docker 部署失败", tuple(lines), "error")


async def _auto_remediate_deploy_start(
    client: MCSMClient,
    uuid: str,
    daemon_id_value: str,
    start_error: str,
    log_text: str,
    summary: dict[str, Any],
) -> tuple[bool, str, list[str]]:
    """Apply deterministic automatic repairs after a new Minecraft instance starts."""
    actions: list[str] = []
    if not needs_eula_remediation(start_error, log_text):
        return False, log_text, actions

    await client.write_instance_file(uuid, daemon_id_value, "eula.txt", EULA_REMEDIATION_TEXT)
    actions.append("已写入 eula.txt=eula=true")

    restart_result = await client.start_instance(uuid, daemon_id_value)
    if restart_result.get("status") != 200:
        error = MCSMClient._api_error_message(restart_result)
        actions.append(f"重启失败: {error}")
        summary["auto_remediation"] = remediation_summary(actions)
        return False, log_text, actions

    actions.append("已重新启动实例")
    await _asyncio.sleep(8)
    fixed_log = await client.get_instance_output(uuid, daemon_id_value, size=128)
    summary["auto_remediation"] = remediation_summary(actions)
    return True, fixed_log, actions


def _instance_uuid(instance: dict[str, Any]) -> str:
    return str(instance.get("instanceUuid") or instance.get("uuid") or instance.get("id") or "").strip()


def _instance_label(instance: dict[str, Any]) -> str:
    config = instance.get("config")
    if isinstance(config, dict):
        name = config.get("nickname") or config.get("name") or config.get("remarks")
        if name:
            return str(name)
    return str(instance.get("nickname") or instance.get("instanceName") or instance.get("name") or instance.get("remarks") or _instance_uuid(instance)[:8])


async def _retry_upload_after_permission_repair(
    client: MCSMClient,
    uuid: str,
    daemon_id_value: str,
    local_file: Path,
    summary: dict[str, Any],
) -> str:
    try:
        instances = await client.get_daemon_instances(daemon_id_value)
    except Exception as exc:
        summary["upload_repair_status"] = f"查找权限修复实例失败: {type(exc).__name__}: {redact_mcsm_sensitive_text(exc)}"
        raise

    repair = next((inst for inst in instances if is_permission_repair_instance(inst)), None)
    if not repair:
        summary["upload_repair_status"] = "未找到同节点权限修复实例"
        raise RuntimeError("上传权限修复失败: 未找到同节点权限修复实例 0-AAA卡权限解决脚本")

    repair_uuid = _instance_uuid(repair)
    repair_name = _instance_label(repair)
    if not repair_uuid:
        summary["upload_repair_status"] = f"权限修复实例 {repair_name} 缺少 UUID"
        raise RuntimeError(f"上传权限修复失败: 权限修复实例 {repair_name} 缺少 UUID")

    await mcsm.send(f"上传权限异常，正在执行同节点修复实例: {repair_name}")
    summary["upload_repair_instance"] = repair_name
    summary["upload_repair_status"] = "已启动权限修复实例，准备重试上传"
    start_result = await client.start_instance(repair_uuid, daemon_id_value)
    if start_result.get("status") != 200:
        err = MCSMClient._api_error_message(start_result)
        summary["upload_repair_status"] = f"启动权限修复实例失败: {err}"
        raise RuntimeError(f"上传权限修复失败: 启动 {repair_name} 失败: {err}")

    await _asyncio.sleep(12)
    summary["upload_repair_status"] = "已执行权限修复实例并重试上传"
    uploaded_name = await client.upload_file_to_instance(uuid, daemon_id_value, local_file, upload_dir="/")
    summary["auto_remediation"] = remediation_summary(
        [str(summary.get("auto_remediation", "")), "已执行权限修复实例并重试上传"]
    )
    return uploaded_name


async def _retry_remote_install_after_upload_failure(
    client: MCSMClient,
    uuid: str,
    daemon_id_value: str,
    install_url: str,
    package_name: str,
    alias: str,
    summary: dict[str, Any],
    upload_error: Exception,
    qflash_source_url: str = "",
    qflash_archive: QFlashArchive | None = None,
) -> None:
    summary["remote_install_retry_status"] = "上传失败后尝试 daemon 远程下载直链安装"
    install_urls = [install_url]
    if qflash_source_url and qflash_archive is not None:
        qflash_archive = await _refresh_qflash_download(qflash_source_url, qflash_archive, summary)
        install_urls = qflash_download_url_candidates(qflash_archive)
        package_name = qflash_archive.name
    last_remote_exc: Exception | None = None
    last_attempt = 0
    for index, candidate_url in enumerate(install_urls, 1):
        try:
            await client.install_instance_from_url(uuid, daemon_id_value, candidate_url, title=f"{alias} {package_name}")
        except Exception as remote_exc:
            last_remote_exc = remote_exc
            last_attempt = index
            summary["remote_install_retry_status"] = (
                f"远程下载直链安装第 {index}/{len(install_urls)} 次失败: "
                f"{type(remote_exc).__name__}: {redact_mcsm_sensitive_text(remote_exc)}"
            )
            continue
        summary["remote_install_retry_status"] = "远程下载直链安装成功"
        if len(install_urls) > 1:
            summary["remote_install_url_variant"] = f"{index}/{len(install_urls)}"
        summary["transfer_status"] = "Bot 上传失败，已改用 daemon 远程下载直链安装"
        return

    assert last_remote_exc is not None
    remote_exc = last_remote_exc
    if len(install_urls) > 1:
        summary["remote_install_url_variant"] = f"全部失败 ({last_attempt}/{len(install_urls)})"
    upload_text = redact_mcsm_sensitive_text(upload_error)
    remote_text = redact_mcsm_sensitive_text(remote_exc)
    summary["remote_install_retry_status"] = (
        f"远程下载直链安装失败: {type(remote_exc).__name__}: {remote_text}"
    )
    raise RuntimeError(
        "上传到 daemon 失败，且远程下载直链安装也失败: "
        f"上传错误={type(upload_error).__name__}: {upload_text}; "
        f"远程安装错误={type(remote_exc).__name__}: {remote_text}"
    ) from upload_error


async def _retry_large_qflash_remote_install(
    client: MCSMClient,
    uuid: str,
    daemon_id_value: str,
    source_url: str,
    selected: QFlashArchive,
    alias: str,
    summary: dict[str, Any],
    first_error: Exception,
) -> tuple[bool, str, QFlashArchive]:
    if not _is_large_qflash_archive(selected):
        return False, qflash_download_url(selected), selected

    summary["large_package_strategy"] = f"启用大包直链刷新重试，阈值 {_large_package_threshold_bytes() // 1024 // 1024} MiB"
    fresh = await _refresh_qflash_download(source_url, selected, summary)
    install_urls = qflash_download_url_candidates(fresh)
    last_exc: Exception | None = None
    for index, install_url in enumerate(install_urls, 1):
        try:
            await client.install_instance_from_url(uuid, daemon_id_value, install_url, title=f"{alias} {fresh.name}")
        except Exception as retry_exc:
            last_exc = retry_exc
            summary["large_package_remote_retry"] = (
                f"大包远程安装刷新重试第 {index}/{len(install_urls)} 次失败: "
                f"{type(retry_exc).__name__}: {redact_mcsm_sensitive_text(retry_exc)}"
            )
            logger.warning(
                f"[MCSM] 大包远程安装刷新重试失败: first={type(first_error).__name__}: "
                f"{redact_mcsm_sensitive_text(first_error)}; retry={type(retry_exc).__name__}: "
                f"{redact_mcsm_sensitive_text(retry_exc)}"
            )
            continue

        summary["large_package_remote_retry"] = "大包远程安装刷新重试成功"
        if len(install_urls) > 1:
            summary["remote_install_url_variant"] = f"{index}/{len(install_urls)}"
        return True, install_url, fresh

    install_url = install_urls[-1] if install_urls else qflash_download_url(fresh)
    if last_exc is not None:
        summary["large_package_remote_retry"] = (
            f"大包远程安装刷新重试失败: {type(last_exc).__name__}: {redact_mcsm_sensitive_text(last_exc)}"
        )
    if len(install_urls) > 1:
        summary["remote_install_url_variant"] = f"全部失败 ({len(install_urls)}/{len(install_urls)})"
    return False, install_url, fresh


async def _cleanup_failed_deploy_instance(
    client: MCSMClient,
    uuid: str,
    daemon_id_value: str,
    summary: dict[str, Any],
) -> None:
    if not uuid or not daemon_id_value:
        return
    if summary.get("deploy_cleanup_status"):
        return
    try:
        await client.delete_instance(uuid, daemon_id_value, delete_files=False)
        summary["deploy_cleanup_status"] = "已自动删除本次创建的实例配置，实例文件已保留"
    except Exception as cleanup_exc:
        summary["deploy_cleanup_status"] = (
            "自动删除本次创建的实例失败，已保留现场: "
            f"{type(cleanup_exc).__name__}: {redact_mcsm_sensitive_text(cleanup_exc)}"
        )
        logger.warning(
            f"[MCSM] deploy 失败后自动删除实例失败: {type(cleanup_exc).__name__}: "
            f"{redact_mcsm_sensitive_text(cleanup_exc)}"
        )


async def _cmd_deploy(bot: Bot, event: Event, group_id: str, user_id: str, args_text: str) -> None:
    permission_error = await _require_group_manager(bot, event, group_id, user_id)
    if permission_error:
        await _finish_error(mcsm, permission_error)
        return

    parsed = parse_deploy_args(_split_command_text(args_text))
    if parsed.errors or parsed.options is None:
        await _finish_notice(mcsm, "deploy 参数错误", tuple(parsed.errors), "warning")
        return
    options = parsed.options
    if not options.alias:
        await _finish_notice(mcsm, "deploy 参数错误", ("别名不能为空",), "warning")
        return

    client = get_client(group_id)
    if client is None:
        await _finish_notice(mcsm, "当前群未绑定 MCSM 面板", ("请先使用 /mcsm bind <面板地址> 绑定面板。",), "warning")
        return

    summary: dict[str, Any] = {
        "alias": options.alias,
        "requested_alias": options.alias,
        "port": options.port,
        "memory_mb": options.memory_mb,
    }
    start_command = ""
    start_source = ""
    archive_start_command = ""
    archive_start_source = ""
    uuid = ""
    did = ""
    stage = "部署准备"
    try:
        stage = "解析下载链接"
        await mcsm.send("开始部署：解析下载链接、检测节点与镜像。")
        install_url, package_name, qflash_archive = await _resolve_deploy_download(options, summary)

        stage = "检测 Docker 节点"
        daemon = await _select_deploy_daemon(client, options)
        if daemon is None:
            return
        did = daemon_id(daemon)
        summary["daemon"] = _daemon_label(daemon)

        if not options.port:
            stage = "自动分配端口"
            allocated_port, port_source = await _auto_deploy_port(client, did)
            options.port = allocated_port
            final_alias = apply_auto_port_alias(options.alias, allocated_port)
            if final_alias != options.alias:
                summary["alias_change"] = f"{options.alias} -> {final_alias}"
                options.alias = final_alias
            summary["port"] = allocated_port
            summary["alias"] = options.alias
            summary["port_source"] = port_source

        if _store.alias_exists(group_id, options.alias):
            lines = [f"别名: {options.alias}"]
            if summary.get("alias_change"):
                lines.append(f"别名调整: {summary['alias_change']}")
            await _finish_notice(mcsm, "实例别名已存在", tuple(lines), "warning")
            return

        stage = "选择 Docker 镜像"
        image = await _select_deploy_image(client, daemon, options)
        if image is None:
            return
        image_name = image_display_name(image)
        summary["image"] = image_name

        if options.dry_run:
            await _finish_notice(
                mcsm,
                "MCSM Docker 部署预检通过",
                (
                    f"别名: {options.alias}",
                    *((f"别名调整: {summary['alias_change']}",) if summary.get("alias_change") else ()),
                    f"节点: {_daemon_label(daemon)}",
                    f"镜像: {image_name}",
                    f"压缩包: {summary.get('package', package_name)}",
                    f"端口: {options.port}:25565/tcp",
                    f"端口来源: {summary.get('port_source', '用户指定')}",
                    "未创建实例；去掉 --dry-run 后执行部署。",
                ),
                "success",
            )
            return

        stage = "创建 Docker 实例"
        await mcsm.send("正在创建实例并安装压缩包。")
        create_data = await client.create_docker_instance(
            did,
            options.alias,
            image_name,
            options.command or "sh ./start.sh",
            options.port,
            options.memory_mb,
        )
        uuid = extract_created_instance_uuid(create_data)
        if not uuid:
            raise RuntimeError(f"创建实例成功但无法解析实例 UUID: {create_data}")
        summary["uuid"] = uuid[:8]

        stage = "安装压缩包"
        try:
            await client.install_instance_from_url(uuid, did, install_url, title=f"{options.alias} {package_name}")
        except Exception as exc:
            if qflash_archive is None:
                raise
            remote_retry_succeeded, install_url, qflash_archive = await _retry_large_qflash_remote_install(
                client,
                uuid,
                did,
                options.url,
                qflash_archive,
                options.alias,
                summary,
                exc,
            )
            if remote_retry_succeeded:
                package_name = qflash_archive.name
            else:
                package_name = qflash_archive.name
            if remote_retry_succeeded:
                await _asyncio.sleep(2)
                stage = "扫描启动脚本"
                if not start_command:
                    start_command, start_source = await detect_deploy_start_command(
                        client,
                        uuid,
                        did,
                        options.memory_mb,
                        options.command,
                    )
                start_command, start_source, used_archive_fallback = apply_archive_start_fallback(
                    start_command,
                    start_source,
                    archive_start_command,
                    archive_start_source,
                )
                if not start_command:
                    summary["start_source"] = start_source
                    raise RuntimeError(start_source)
                summary["start_command"] = start_command
                summary["start_source"] = start_source
                stage = "更新启动命令"
                await client.update_instance_start_command(uuid, did, start_command)

                stage = "启动实例"
                start_result = await client.start_instance(uuid, did)
                if start_result.get("status") != 200:
                    start_error = MCSMClient._api_error_message(start_result)
                    remediated, log_text, actions = await _auto_remediate_deploy_start(
                        client,
                        uuid,
                        did,
                        start_error,
                        "",
                        summary,
                    )
                    if not remediated:
                        if actions:
                            raise RuntimeError(f"{start_error}; 自动修复: {remediation_summary(actions)}")
                        raise RuntimeError(start_error)
                else:
                    await _asyncio.sleep(5)
                    log_text = await client.get_instance_output(uuid, did, size=64)
                    if needs_eula_remediation("", log_text):
                        remediated, log_text, actions = await _auto_remediate_deploy_start(
                            client,
                            uuid,
                            did,
                            "",
                            log_text,
                            summary,
                        )
                        if not remediated and actions:
                            raise RuntimeError(f"启动后自动修复失败: {remediation_summary(actions)}")

                _store.bind_instance(group_id, options.alias, uuid, did)
                lines = [
                    f"别名: {options.alias}",
                    f"节点: {daemon_name(daemon)} ({did[:8]})",
                    f"镜像: {image_name}",
                    f"压缩包: {summary.get('package', package_name)}",
                    f"端口: {options.port}:25565/tcp",
                    f"启动命令: {start_command}",
                    f"实例: {uuid[:8]}",
                ]
                if summary.get("alias_change"):
                    lines.insert(1, f"别名调整: {summary['alias_change']}")
                if summary.get("port_source"):
                    lines.append(f"端口来源: {summary['port_source']}")
                if summary.get("package_expired_at"):
                    lines.append(f"闪传过期: {summary['package_expired_at']}")
                if summary.get("large_package_remote_retry"):
                    lines.append(f"大包远程重试: {summary['large_package_remote_retry']}")
                if summary.get("qflash_refresh_count"):
                    lines.append(f"闪传直链刷新: {summary['qflash_refresh_count']} 次")
                if summary.get("remote_install_url_variant"):
                    lines.append(f"直链候选: {summary['remote_install_url_variant']}")
                if summary.get("auto_remediation"):
                    lines.append(f"自动修复: {summary['auto_remediation']}")
                await _finish_notice(mcsm, "MCSM Docker 部署完成", tuple(lines), "success")
                return
            logger.warning(
                f"[MCSM] 闪传远程安装失败，切换 Bot 中转上传: {type(exc).__name__}: "
                f"{redact_mcsm_sensitive_text(exc)}"
            )
            stage = "下载中转"
            await mcsm.send("远程安装失败，切换 Bot 中转上传并解压。")
            filename = safe_archive_filename(qflash_archive.name)
            with tempfile.TemporaryDirectory(prefix="mcsm-qflash-") as temp_dir:
                local_file = Path(temp_dir) / filename
                qflash_archive = await _refresh_qflash_download(options.url, qflash_archive, summary)
                await download_qflash_archive(qflash_archive, local_file)
                summary["transfer_status"] = "已下载到 Bot 临时目录"
                archive_start_command, archive_start_source = detect_archive_start_command(
                    local_file,
                    options.memory_mb,
                    options.command,
                )
                if archive_start_command:
                    summary["archive_start_source"] = archive_start_source

                stage = "上传到 daemon"
                remote_install_fallback_succeeded = False
                try:
                    uploaded_name = await client.upload_file_to_instance(uuid, did, local_file, upload_dir="/")
                except Exception as upload_exc:
                    if is_upload_permission_error(upload_exc):
                        summary["upload_repair_status"] = f"上传权限异常，准备执行修复实例: {redact_mcsm_sensitive_text(upload_exc)}"
                        try:
                            uploaded_name = await _retry_upload_after_permission_repair(client, uuid, did, local_file, summary)
                        except Exception as repair_exc:
                            stage = "远程直链安装重试"
                            await _retry_remote_install_after_upload_failure(
                                client,
                                uuid,
                                did,
                                install_url,
                                package_name,
                                options.alias,
                                summary,
                                repair_exc,
                                options.url,
                                qflash_archive,
                            )
                            remote_install_fallback_succeeded = True
                    else:
                        stage = "远程直链安装重试"
                        await _retry_remote_install_after_upload_failure(
                            client,
                            uuid,
                            did,
                            install_url,
                            package_name,
                            options.alias,
                            summary,
                            upload_exc,
                            options.url,
                            qflash_archive,
                        )
                        remote_install_fallback_succeeded = True

                if not remote_install_fallback_succeeded:
                    summary["transfer_package"] = uploaded_name
                    summary["transfer_status"] = "已上传到 daemon"

                    stage = "解压压缩包"
                    try:
                        await client.extract_instance_archive(uuid, did, uploaded_name, target="/")
                        summary["extract_status"] = "解压接口已返回成功"
                    except Exception as extract_exc:
                        if not is_extract_gateway_timeout_error(extract_exc):
                            raise
                        summary["extract_timeout_waited"] = True
                        summary["extract_status"] = "解压接口超时，已进入后台轮询"
                        stage = "等待后台解压"
                        start_command, start_source = await wait_for_deploy_start_command(
                            client,
                            uuid,
                            did,
                            options.memory_mb,
                            options.command,
                            wait_seconds=180,
                            interval_seconds=5,
                        )
                        start_command, start_source, used_archive_fallback = apply_archive_start_fallback(
                            start_command,
                            start_source,
                            archive_start_command,
                            archive_start_source,
                            api_label="API 等待结果",
                        )
                        if used_archive_fallback:
                            logger.warning("[MCSM] 后台解压等待未识别启动命令，使用压缩包内容推断: {}", archive_start_source)
                        if not start_command:
                            raise RuntimeError(start_source)

                    stage = "删除临时压缩包"
                    try:
                        await client.delete_instance_file(uuid, did, uploaded_name)
                        summary["cleanup_status"] = "已删除上传压缩包"
                    except Exception as cleanup_exc:
                        summary["cleanup_status"] = "删除上传压缩包失败，已保留现场"
                        logger.warning(
                            f"[MCSM] 删除中转压缩包失败，已保留现场: {type(cleanup_exc).__name__}: "
                            f"{redact_mcsm_sensitive_text(cleanup_exc)}"
                        )
        await _asyncio.sleep(2)

        stage = "扫描启动脚本"
        await mcsm.send("正在识别启动命令并启动实例。")
        if not start_command:
            start_command, start_source = await detect_deploy_start_command(
                client,
                uuid,
                did,
                options.memory_mb,
                options.command,
            )
        start_command, start_source, used_archive_fallback = apply_archive_start_fallback(
            start_command,
            start_source,
            archive_start_command,
            archive_start_source,
        )
        if used_archive_fallback:
            logger.warning("[MCSM] API 文件扫描未识别启动命令，使用压缩包内容推断: {}", archive_start_source)
        if not start_command:
            summary["start_source"] = start_source
            raise RuntimeError(start_source)
        summary["start_command"] = start_command
        summary["start_source"] = start_source
        stage = "更新启动命令"
        await client.update_instance_start_command(uuid, did, start_command)

        stage = "启动实例"
        start_result = await client.start_instance(uuid, did)
        if start_result.get("status") != 200:
            start_error = MCSMClient._api_error_message(start_result)
            remediated, log_text, actions = await _auto_remediate_deploy_start(
                client,
                uuid,
                did,
                start_error,
                "",
                summary,
            )
            if not remediated:
                if actions:
                    raise RuntimeError(f"{start_error}; 自动修复: {remediation_summary(actions)}")
                raise RuntimeError(start_error)
        else:
            await _asyncio.sleep(5)
            log_text = await client.get_instance_output(uuid, did, size=64)
            if needs_eula_remediation("", log_text):
                remediated, log_text, actions = await _auto_remediate_deploy_start(
                    client,
                    uuid,
                    did,
                    "",
                    log_text,
                    summary,
                )
                if not remediated and actions:
                    raise RuntimeError(f"启动后自动修复失败: {remediation_summary(actions)}")

        _store.bind_instance(group_id, options.alias, uuid, did)
        lines = [
            f"别名: {options.alias}",
            f"节点: {daemon_name(daemon)} ({did[:8]})",
            f"镜像: {image_name}",
            f"压缩包: {summary.get('package', package_name)}",
            f"端口: {options.port}:25565/tcp",
            f"启动命令: {start_command}",
            f"实例: {uuid[:8]}",
        ]
        if summary.get("alias_change"):
            lines.insert(1, f"别名调整: {summary['alias_change']}")
        if summary.get("port_source"):
            lines.append(f"端口来源: {summary['port_source']}")
        if summary.get("package_expired_at"):
            lines.append(f"闪传过期: {summary['package_expired_at']}")
        if summary.get("extract_timeout_waited"):
            lines.append("解压接口曾超时，已通过后台轮询确认完成。")
        if summary.get("remote_install_retry_status") == "远程下载直链安装成功":
            lines.append("安装方式: daemon 远程下载直链")
        if summary.get("remote_install_url_variant"):
            lines.append(f"直链候选: {summary['remote_install_url_variant']}")
        if summary.get("archive_start_source") and str(summary.get("start_source", "")).startswith(str(summary["archive_start_source"])):
            lines.append("启动命令来源: 压缩包内容推断。")
        if summary.get("auto_remediation"):
            lines.append(f"自动修复: {summary['auto_remediation']}")
        if log_looks_suspicious(log_text):
            advice = await diagnose_deploy_failure("启动日志检查", "启动后日志疑似异常", log_text=log_text, summary=summary)
            if advice:
                lines.append("启动日志疑似异常，LLM 建议:")
                lines.extend(advice.splitlines()[:6])
        await _finish_notice(mcsm, "MCSM Docker 部署完成", tuple(lines), "success")
        return
    except _ExitException:
        raise
    except Exception as exc:
        logger.warning(f"[MCSM] Docker deploy failed: {type(exc).__name__}: {redact_mcsm_sensitive_text(exc)}")
        if uuid and did:
            await _cleanup_failed_deploy_instance(client, uuid, did, summary)
        await _finish_deploy_failure(stage, str(exc), summary)


@dm_bind_handler.handle()
async def handle_dm_bind(event: Event, bot: Bot):
    user_id = str(event_user_id(event))
    session = _pending_bind_sessions.get(_bind_session_key(user_id))
    if not session:
        stop_session()
        return

    text = event_plain_text(event).strip()
    if text in ("取消", "cancel", "Cancel", "q", "Q"):
        _pending_bind_sessions.pop(_bind_session_key(user_id), None)
        await _finish_dm_notice(dm_bind_handler, bot, user_id, "批量绑定已取消", (), "warning")
        return

    instances = list(session.get("instances") or [])
    selected, errors = _parse_bind_selection(text, instances)
    if not selected:
        await _finish_dm_notice(
            dm_bind_handler,
            bot,
            user_id,
            "未匹配到可绑定实例",
            tuple(errors[:8]) or ("请回复实例序号/名称，或回复“取消”。",),
            "error",
        )
        return

    group_id = str(session["group_id"])
    daemon_id = str(session["daemon_id"])
    seen_uuid: set[str] = set()
    added: list[str] = []
    skipped: list[str] = []

    for index, inst, alias in selected:
        uuid = _instance_uuid(inst)
        alias = re.sub(r"\s+", "_", alias.strip())[:64]
        if not uuid:
            skipped.append(f"{index}: 缺少 UUID")
            continue
        if uuid in seen_uuid:
            continue
        seen_uuid.add(uuid)
        existing_alias = _store.find_instance_by_uuid(group_id, uuid)
        if existing_alias:
            skipped.append(f"{index}: 已绑定为 {existing_alias}")
            continue
        if not alias:
            alias = _default_alias(inst)
        if _store.alias_exists(group_id, alias):
            skipped.append(f"{index}: 别名 {alias} 已存在")
            continue
        _store.bind_instance(group_id, alias, uuid, daemon_id)
        added.append(f"{alias} ({uuid[:8]})")

    _pending_bind_sessions.pop(_bind_session_key(user_id), None)
    lines: list[str] = []
    if added:
        lines.append("成功绑定:")
        lines.extend(added[:20])
    if skipped or errors:
        lines.append("跳过:")
        lines.extend((skipped + errors)[:20])
    await _finish_dm_notice(
        dm_bind_handler,
        bot,
        user_id,
        f"批量绑定完成: {len(added)} 个成功",
        tuple(lines) if lines else ("没有变更",),
        "success" if added else "warning",
    )
mcsm = _cmd("mcsm", args=Args["rest;?", MultiVar(AnyString)], priority=5, block=True)


@mcsm.handle()
async def handle_mcsm(bot: Bot, event: Event, rest: ArgVal):
    """MCSM 主命令 - 子命令路由。"""
    text = get_rest(rest)
    parts = text.split() if text else []
    subcmd = parts[0].lower() if parts else ""

    group_id = _get_group_id(event)
    user_id = _get_user_id(event)

    # 无子命令 -> list
    if not subcmd:
        return await _cmd_list(bot, event, group_id, user_id)

    # help
        await mcsm.finish(_help_tip())
        return

    # bind <panel_url>
    if subcmd == "bind":
        arg1 = parts[1] if len(parts) > 1 else ""
        if arg1 and ("://" in arg1 or arg1.startswith("http") or (":" in arg1 and "/" in arg1)):
            return await _cmd_bind_panel(bot, event, group_id, user_id, arg1)
        return await _cmd_bind_instance(bot, event, group_id, user_id, parts)

    # unbindpanel
    if subcmd in ("unbindpanel", "解绑面板"):
        return await _cmd_unbind_panel(bot, event, group_id, user_id)

    # 以下需要面板已绑定
    if not _store.has_panel(group_id):
        await _finish_notice(
            mcsm,
            "当前群未绑定 MCSM 面板",
            ("请使用 /mcsm bind <面板地址> 绑定面板", _help_tip()),
            "warning",
        )
        return

    # deploy <alias> <url> --port <host_port>
    if subcmd in ("deploy", "部署"):
        deploy_text = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        return await _cmd_deploy(bot, event, group_id, user_id, deploy_text)

    # list
    if subcmd in ("list", "列表"):
        show_all = any(p in ("-a", "--all", "all", "鍏ㄩ儴") for p in parts[1:])
        return await _cmd_list(bot, event, group_id, user_id, show_all=show_all)

    # status
    if subcmd in ("status", "info"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_status(bot, event, group_id, alias)

    # start
    if subcmd in ("start", "启动", "open"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_instance_action(bot, event, group_id, user_id, "open", alias)

    # stop
    if subcmd in ("stop", "停止"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_instance_action(bot, event, group_id, user_id, "stop", alias)

    # start
    if subcmd in ("restart", "閲嶅惎"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_instance_action(bot, event, group_id, user_id, "restart", alias)

    # kill
    if subcmd in ("kill", "寮哄埗缁撴潫", "寮烘潃"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_instance_action(bot, event, group_id, user_id, "kill", alias)

    # cmd
    if subcmd in ("cmd", "鍛戒护", "exec"):
        cmd_parts = text.split(None, 2)
        alias = cmd_parts[1] if len(cmd_parts) > 1 else ""
        command = cmd_parts[2] if len(cmd_parts) > 2 else ""
        return await _cmd_instance_action(bot, event, group_id, user_id, "command", alias, extra=command)

    # log
    if subcmd in ("log", "日志"):
        alias, log_limit, log_error = _parse_log_args(parts)
        if log_error:
            await _finish_error(mcsm, log_error)
            return
        return await _cmd_log(bot, event, group_id, user_id, alias, log_limit=log_limit)

    # ── hide / unhide
    if subcmd in ("hide", "隐藏"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_hide(bot, event, group_id, user_id, alias, True)
    if subcmd in ("unhide", "取消隐藏", "显示"):
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_hide(bot, event, group_id, user_id, alias, False)

    # helpers
    if subcmd == "unbind":
        alias = parts[1] if len(parts) > 1 else ""
        return await _cmd_unbind(bot, event, group_id, user_id, alias)
    if subcmd in ("delete", "del", "remove", "删除"):
        alias = parts[1] if len(parts) > 1 else ""
        delete_files = any(p in ("--files", "--delete-files", "--file", "文件") for p in parts[2:])
        return await _cmd_delete_instance(bot, event, group_id, user_id, alias, delete_files=delete_files)

    # admin
    if subcmd == "admin":
        return await _cmd_admin(bot, event, group_id, user_id, parts)

    # 模糊匹配 -> 查状态
    instances = _store.get_group_instances(group_id)
    if text in instances:
        return await _cmd_status(bot, event, group_id, text)
    matches = _store.find_instance_by_name(group_id, text)
    if len(matches) == 1:
        return await _cmd_status(bot, event, group_id, matches[0])
    if len(matches) > 1:
        lines = [f"{text} matched multiple instances:"]
        for m in matches:
            lines.append(f"  {m}")
        lines.append("Enter the full alias.")
        await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
        return

    await _finish_notice(
        mcsm,
        f"Unknown command: {text}",
        ("可用命令: list | status | start | stop | restart | kill", "cmd | log | bind | unbind | delete | admin | hide | unhide | deploy", _help_tip()),
        "warning",
    )


#  鍛戒护瀹炵幇

# bind panel

async def _cmd_bind_panel(
    bot: Bot, event: Event, group_id: str, user_id: str, panel_url: str,
):
    """绑定 MCSM 面板到当前群。"""
    if not group_id:
        await _finish_error(mcsm, "请在群聊中使用此命令")
        return
    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    if _store.has_panel(group_id):
        existing = _store.get_panel(group_id)
        await _finish_notice(
            mcsm,
            "当前群已绑定面板",
            (
                f"面板: {existing[0] if existing else '/'}",
                "面板已绑定；使用 /mcsm bind <节点ID> 绑定实例",
                "如需更换，请先使用 /mcsm unbindpanel 解绑",
            ),
            "warning",
        )
        return

    url = panel_url.rstrip("/")
    if not url.startswith("http"):
        url = f"http://{url}"

    _store.set_panel_url(group_id, url)
    _store.set_owner(group_id, user_id)
    _store.set_pending_key(user_id, group_id, url)

    try:
        target = SendDest(
            user_id, "", False, True, "",
            account_adapter_name(bot),
        )
        await ChainMsg.text(
            f"请回复此消息提供 MCSM 面板的 API Key:\n"
            f"面板地址: {url}\n\n"
            f"Key 可在 MCSM 面板的 API 密钥页面生成。\n"
            f"回复「取消」可中止绑定。"
        ).send(target, bot)
    except Exception as e:
        _store.clear_pending_key(user_id)
        _store.clear_panel(group_id)
        await _finish_notice(mcsm, "无法发送私聊", (f"请确认已添加 Bot 好友: {e}",), "error")
        return

    await _finish_notice(mcsm, "面板地址已保存", (f"面板: {url}", "请查看私聊并回复 API Key。"), "success")


# bind panel

async def _cmd_unbind_panel(bot: Bot, event: Event, group_id: str, user_id: str):
    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return
    if not _store.has_panel(group_id):
        await _finish_notice(mcsm, "当前群未绑定面板", (), "warning")
        return
    _store.clear_panel(group_id)
    _clear_client(group_id)
    await _finish_notice(mcsm, "MCSM panel unbound", ("Group instance bindings were cleared.",), "success")


def _instance_uuid(inst: dict) -> str:
    return str(inst.get("instanceUuid") or inst.get("uuid") or "")


def _instance_display_name(inst: dict) -> str:
    cfg = inst.get("config", {}) or {}
    return str(
        cfg.get("nickname")
        or inst.get("instanceName")
        or inst.get("name")
        or _instance_uuid(inst)[:8]
        or "unknown"
    )


def _default_alias(inst: dict) -> str:
    alias = re.sub(r"\s+", "_", _instance_display_name(inst).strip())
    alias = alias.strip("_")
    return alias[:64] or _instance_uuid(inst)[:8] or "instance"


def _bind_session_key(user_id: str) -> str:
    return str(user_id)


def _clear_expired_bind_sessions() -> None:
    now = time.time()
    for key, session in list(_pending_bind_sessions.items()):
        if float(session.get("expires_at", 0)) < now:
            _pending_bind_sessions.pop(key, None)


def _match_daemon(daemons: List[dict], value: str) -> dict | None:
    value_lower = value.lower()
    for daemon in daemons:
        candidates = {
            str(daemon.get("uuid", "")).lower(),
            str(daemon.get("id", "")).lower(),
            str(daemon.get("remarks", "")).lower(),
            str(daemon.get("name", "")).lower(),
        }
        if value_lower in candidates:
            return daemon
    return None


async def _send_private_text(bot: Bot, user_id: str, text: str) -> None:
    await ChainMsg.text(text).send(_private_target(bot, user_id), bot)


def _candidate_line(index: int, inst: dict) -> str:
    uuid = _instance_uuid(inst)
    status = STATUS_MAP.get(inst.get("status", inst.get("state", -1)), "UNKNOWN")
    return f"{index}. {_instance_display_name(inst)} [{status}] {uuid[:8]}"


async def _cmd_bind_node_private(bot: Bot, event: Event, group_id: str, user_id: str, daemon_arg: str):
    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    try:
        daemons = await client.get_daemon_list()
    except Exception as exc:
        await _finish_notice(mcsm, "获取节点列表失败", (str(exc),), "error")
        return

    daemon = _match_daemon(daemons, daemon_arg)
    if not daemon:
        await _finish_notice(
            mcsm,
            "未找到该节点",
            ("请确认节点 ID 是否正确", "直接绑定实例仍可使用 /mcsm bind <实例UUID> <别名> [节点ID]"),
            "error",
        )
        return

    daemon_id = str(daemon.get("uuid") or daemon.get("id") or daemon_arg)
    try:
        instances = await client.get_daemon_instances(daemon_id)
    except Exception as exc:
        await _finish_notice(
            mcsm,
            "获取节点实例失败",
            (
                str(exc),
                "请确认节点 ID 正确、daemon 在线，并且当前面板 API Key 有实例读取权限。",
            ),
            "error",
        )
        return

    if not instances:
        await _finish_notice(mcsm, "该节点没有可绑定实例", (), "warning")
        return

    session_key = _bind_session_key(user_id)
    _pending_bind_sessions[session_key] = {
        "group_id": str(group_id),
        "daemon_id": daemon_id,
        "instances": instances,
        "expires_at": time.time() + 15 * 60,
    }

    daemon_name = str(daemon.get("remarks") or daemon.get("name") or daemon_id[:12])
    lines = [
        "MCSM 批量绑定选择",
        f"群 {group_id}",
        f"节点: {daemon_name} ({daemon_id[:8]})",
        "",
        "回复示例:",
        "1 2 3",
        "1=survival 2=lobby",
        "取消",
        "",
        "实例列表:",
    ]
    for index, inst in enumerate(instances[:80], 1):
        lines.append(_candidate_line(index, inst))
    if len(instances) > 80:
        lines.append(f"... 还有 {len(instances) - 80} 个实例未展示，可使用精确名称匹配。")

    try:
        await _send_private_text(bot, user_id, "\n".join(lines))
    except Exception as exc:
        _pending_bind_sessions.pop(session_key, None)
        await _finish_notice(mcsm, "无法发送私聊选择", (f"请确认已添加 bot 好友: {exc}",), "error")
        return

    await _finish_notice(mcsm, "已发送私聊选择", ("候选实例列表和绑定结果仅在私聊显示",), "success")


# bind instance

async def _cmd_bind_instance(
    bot: Bot, event: Event, group_id: str, user_id: str, parts: List[str],
):
    """绑定实例别名: /mcsm bind <uuid> <别名> [节点ID]。"""
    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    if len(parts) == 2:
        return await _cmd_bind_node_private(bot, event, group_id, user_id, parts[1])

    if len(parts) < 3:
        await _finish_notice(
            mcsm,
            "用法错误",
            ("绑定面板: /mcsm bind <面板地址>", "绑定节点实例: /mcsm bind <节点ID>", "直接绑定实例: /mcsm bind <实例UUID> <别名> [节点ID]"),
            "error",
        )
        return

    uuid = parts[1]
    alias = parts[2]

    if _store.alias_exists(group_id, alias):
        await _finish_error(mcsm, f"Alias already exists: {alias}")
        return

    existing = _store.find_instance_by_uuid(group_id, uuid)
    if existing:
        await _finish_error(mcsm, f"This instance is already bound as {existing}")
        return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    daemon_id = parts[3] if len(parts) > 3 else ""
    if not daemon_id:
        await mcsm.send("正在自动探测实例所在节点...")
        try:
            daemon_id = await client.find_instance_daemon(uuid) or ""
        except Exception as e:
            logger.warning(f"[MCSM] 自动探测实例 {uuid} 所在节点失败: {e}")
            daemon_id = ""
        if not daemon_id:
            await _finish_notice(
                mcsm,
                "未找到该实例",
                ("在所有节点中均未找到该实例，请手动指定节点ID:", "/mcsm bind <UUID> <别名> <节点ID>"),
                "error",
            )
            return

    _store.bind_instance(group_id, alias, uuid, daemon_id)
    fallback = f"绑定成功\n别名: {alias}\nUUID: {uuid}\n节点: {daemon_id[:24]}...\n\n/mcsm admin add @某人  添加本群 MCSM 管理员"
    await _finish_image_or_text(mcsm, draw_bind_result(alias, uuid, daemon_id), fallback)


# bind instance

async def _cmd_unbind(
    bot: Bot, event: Event, group_id: str, user_id: str, alias: str,
):
    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return
    if not alias:
        await _finish_error(mcsm, "用法: /mcsm unbind <别名>")
        return
    if not _store.alias_exists(group_id, alias):
        await _finish_error(mcsm, f"Alias does not exist: {alias}")
        return
    _store.unbind_instance(group_id, alias)
    await _finish_notice(mcsm, "实例已从本群移除", (f"实例: {alias}",), "success")


async def _cmd_delete_instance(
    bot: Bot,
    event: Event,
    group_id: str,
    user_id: str,
    alias: str,
    *,
    delete_files: bool = False,
):
    if not alias:
        await _finish_error(mcsm, "用法: /mcsm delete <别名> [--files]")
        return

    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    info = _store.get_instance(group_id, alias)
    if info is None:
        matches = _store.find_instance_by_name(group_id, alias)
        if not matches:
            await _finish_error(mcsm, f"Instance not found: {alias}")
            return
        if len(matches) > 1:
            await _finish_notice(
                mcsm,
                "Multiple instances matched",
                tuple(f"  {match}" for match in matches),
                "warning",
            )
            return
        alias = matches[0]
        info = _store.get_instance(group_id, alias)
        if info is None:
            await _finish_error(mcsm, "内部错误")
            return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    daemon_id_value = str(info.get("daemonId") or "")
    uuid = str(info.get("uuid") or "")
    try:
        await client.delete_instance(uuid, daemon_id_value, delete_files=delete_files)
    except Exception as exc:
        await _finish_notice(mcsm, "删除实例失败", (str(exc),), "error")
        return

    _store.unbind_instance(group_id, alias)
    lines = [
        f"别名: {alias}",
        f"实例: {uuid[:8]}",
        f"文件: {'已请求删除' if delete_files else '已保留'}",
    ]
    await _finish_notice(mcsm, "实例已删除", tuple(lines), "success")


# list

async def _cmd_list(
    bot: Bot,
    event: Event,
    group_id: str,
    user_id: str,
    show_all: bool = False,
):
    """列出本群已绑定实例状态。"""
    if not _store.has_panel(group_id):
        await _finish_notice(
            mcsm,
            "当前群未绑定 MCSM 面板",
            ("使用 /mcsm bind <面板地址> 绑定面板", _help_tip()),
            "warning",
        )
        return

    bindings = _store.get_group_instances(group_id)
    can_manage_group = await _is_group_manager(bot, event, group_id, user_id)
    can_see_hidden = show_all and can_manage_group
    visible_bindings = {
        alias: info
        for alias, info in bindings.items()
        if can_see_hidden or not info.get("hidden")
    }
    if not visible_bindings:
        details = ["本群尚未绑定可见实例。"]
        if can_manage_group:
            details.append("使用 /mcsm bind <节点ID> 私聊选择要加入本群的实例。")
        await _finish_notice(mcsm, "暂无本群实例", tuple(details), "warning")
        return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "内部错误: 无法创建客户端")
        return

    daemon_names: Dict[str, str] = {}
    try:
        for daemon in await client.get_daemon_list():
            daemon_id = str(daemon.get("uuid") or daemon.get("id") or "")
            if daemon_id:
                daemon_names[daemon_id] = str(daemon.get("remarks") or daemon.get("ip") or daemon_id[:12])
    except Exception as e:
        logger.debug(f"[MCSM] 获取节点列表失败，继续使用本地绑定信息: {e}")

    snapshots_by_uuid: Dict[str, dict] = {}
    daemon_ids = sorted({str(info.get("daemonId") or "") for info in visible_bindings.values() if info.get("daemonId")})
    for daemon_id in daemon_ids:
        try:
            for inst in await client.get_daemon_instances(daemon_id):
                inst_uuid = str(inst.get("instanceUuid") or inst.get("uuid") or "")
                if inst_uuid:
                    snapshots_by_uuid[inst_uuid] = inst
        except Exception as e:
            logger.debug(f"[MCSM] 获取节点 {daemon_id} 实例快照失败，继续使用本地绑定信息: {e}")

    daemon_map: Dict[str, dict] = {}
    for alias, info in visible_bindings.items():
        daemon_id = str(info.get("daemonId") or "unknown")
        if daemon_id not in daemon_map:
            daemon_map[daemon_id] = {
                "name": daemon_names.get(daemon_id, daemon_id[:12] if daemon_id != "unknown" else "Unknown Daemon"),
                "uuid": daemon_id,
                "instances": [],
                "online": 0,
                "total": 0,
            }

        inst_uuid = str(info.get("uuid") or "")
        snapshot = snapshots_by_uuid.get(inst_uuid, {})
        cfg = snapshot.get("config", {}) or {}
        inst_name = (
            cfg.get("nickname", "")
            or snapshot.get("instanceName", "")
            or snapshot.get("name", "")
            or alias
            or inst_uuid[:8]
        )
        status = _mcsm_status_code(snapshot.get("status", snapshot.get("state", -1)))
        hidden = bool(info.get("hidden"))
        daemon_map[daemon_id]["instances"].append({
            "uuid": inst_uuid,
            "name": inst_name,
            "alias": alias,
            "status": status,
            "hidden": hidden,
        })
        daemon_map[daemon_id]["total"] += 1
        if status == 3:
            daemon_map[daemon_id]["online"] += 1

    panel = _store.get_panel(group_id)
    panel_url = panel[0] if panel else ""
    fallback_lines = ["MCSM 本群实例"]
    if panel_url:
        fallback_lines.append(f"面板: {panel_url[:60]}{'...' if len(panel_url) > 60 else ''}")
    for dm in daemon_map.values():
        fallback_lines.append(f"{dm['name']} [{dm['online']}/{dm['total']} 在线]")
        for inst in dm["instances"][:10]:
            label = inst["alias"] or inst["name"]
            hidden_tag = " [hidden]" if inst.get("hidden") else ""
            status_text = _mcsm_status_text(inst.get("status", -1))
            fallback_lines.append(f"  {status_text} {label}{hidden_tag}")
    fallback = "\n".join(fallback_lines)
    await _finish_image_or_text(
        mcsm,
        draw_panel_overview(
            daemon_map,
            panel_url,
            is_superuser=can_manage_group,
            show_all=show_all,
        ),
        fallback,
    )
# status

async def _cmd_status(bot: Bot, event: Event, group_id: str, alias: str):
    """查询实例详情。"""
    instances = _store.get_group_instances(group_id)

    if not alias:
        return await _cmd_list(bot, event, group_id, _get_user_id(event))

    info = instances.get(alias)
    if info is None:
        matches = _store.find_instance_by_name(group_id, alias)
        if not matches:
            await _finish_notice(mcsm, f"Instance not found: {alias}", ("Use /mcsm list to see available instances.",), "error")
            return
        if len(matches) > 1:
            lines = [f"{alias} matched multiple instances:"]
            for m in matches:
                lines.append(f"  {m}")
            await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
            return
        alias = matches[0]
        info = instances[alias]

    user_id = _get_user_id(event)
    if not await _can_view_instance(bot, event, group_id, alias, user_id):
        await _finish_notice(mcsm, f"Instance not found: {alias}", ("Use /mcsm list to see available instances.",), "error")
        return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    try:
        detail = await client.get_instance_detail(info["uuid"], info["daemonId"])
    except Exception as e:
        await _finish_notice(mcsm, "连接面板失败", (str(e),), "error")
        return

    if detail is None:
        await _finish_notice(mcsm, f"Unable to get instance info: {alias}", ("Check whether the instance still exists.",), "error")
        return

    try:
        snapshots = await client.get_daemon_instances(info["daemonId"])
    except Exception as e:
        logger.debug(f"[MCSM] 获取实例 {alias} 实时快照失败，继续使用详情数据: {e}")
    else:
        target_uuid = str(info["uuid"]).lower()
        target_names = {
            str(alias).lower(),
            str(detail.get("instanceName") or "").lower(),
            str((detail.get("config", {}) or {}).get("nickname") or "").lower(),
            str(detail.get("name") or "").lower(),
        }
        target_names.discard("")
        for snapshot in snapshots:
            snapshot_uuid = str(snapshot.get("instanceUuid") or snapshot.get("uuid") or "").lower()
            snapshot_cfg = snapshot.get("config", {}) or {}
            snapshot_names = {
                str(snapshot.get("instanceName") or "").lower(),
                str(snapshot.get("name") or "").lower(),
                str(snapshot_cfg.get("nickname") or "").lower(),
            }
            snapshot_names.discard("")
            if snapshot_uuid == target_uuid or bool(target_names & snapshot_names):
                detail = merge_status_detail(detail, snapshot)
                break

    status_code = detail.get("status", -1)
    cfg = detail.get("config", {}) or {}
    display_name = cfg.get("nickname", "") or detail.get("instanceName", "") or alias
    status_bind_info = {**info, "admins": _store.get_admins(group_id)}
    summary = status_summary(detail, status_bind_info)

    lines = [f"MCSM  {alias}", "", f"{STATUS_MAP.get(status_code, 'UNKNOWN')}"]
    if display_name != alias:
        lines.append(f"面板名称: {display_name}")
    lines.append(f"CPU: {summary['cpu']}")
    lines.append(f"内存: {summary['memory']}")
    lines.append(f"磁盘: {summary['disk']}")
    lines.append(f"玩家数: {summary['players']}")
    lines.append(f"游戏版本: {summary['version']}")
    lines.append(f"启动次数: {summary['started']}")
    lines.append(f"自动重启: {summary['auto_restart']}")
    lines.append(f"可用端口: {summary['ports']}")
    lines.append(f"到期时间: {summary['end_time']}")
    lines.append(f"最后启动: {summary['last_datetime']}")
    lines.append(f"UUID: {info['uuid']}")
    lines.append(f"节点: {info['daemonId'][:24]}...")
    if cfg.get("startCommand"):
        lines.append(f"启动命令: {cfg['startCommand'][:60]}")
    if summary["type"]:
        lines.append(f"实例类型: {summary['type']}")
    lines.append(f"管理员: {summary['admins']}")
    lines.append("")
    lines.append("/mcsm start|stop|restart|kill|cmd|log")

    await _finish_image_or_text(mcsm, draw_status(alias, detail, status_bind_info), "\n".join(lines))


# instance action (start/stop/restart/kill/cmd)

async def _cmd_instance_action(
    bot: Bot, event: Event, group_id: str, user_id: str,
    action: str, alias: str, extra: str = "",
):
    """通用实例操作。"""
    op_name = OPERATION_NAMES.get(action, action)

    if not alias:
        await _finish_error(mcsm, f"用法: /mcsm {action} <别名>")
        return

    info = _store.get_instance(group_id, alias)
    if info is None:
        matches = _store.find_instance_by_name(group_id, alias)
        if not matches:
            await _finish_error(mcsm, f"Instance not found: {alias}")
            return
        if len(matches) > 1:
            lines = [f"{alias} matched multiple instances:"]
            for m in matches:
                lines.append(f"  {m}")
            await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
            return
        alias = matches[0]
        info = _store.get_instance(group_id, alias)
        if info is None:
            await _finish_error(mcsm, "内部错误")
            return

    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    daemon_id = info["daemonId"]
    uuid = info["uuid"]

    await mcsm.send(f"Running {op_name} on {alias}...")

    try:
        if action == "command":
            if not extra:
                await _finish_error(mcsm, "请输入要执行的命令")
                return
            try:
                before_output = await client.get_instance_output(uuid, daemon_id, size=500)
            except Exception:
                before_output = ""
            result = await client.send_command(uuid, daemon_id, extra)
        else:
            result = await client._instance_action(action, uuid, daemon_id)
    except Exception as e:
        await _finish_notice(mcsm, "操作失败", (str(e),), "error")
        return

    status_code = result.get("status", -1)
    if status_code == 200:
        if action == "command":
            await _asyncio.sleep(3.0)
            try:
                after_output = await client.get_instance_output(uuid, daemon_id, size=500)
            except Exception:
                after_output = "(获取输出失败)"
            raw_output = extract_command_output(before_output, after_output, extra)
            # 截断输出
            if len(raw_output) > 7000:
                raw_output = raw_output[-7000:]
            fallback_output = render_console_text(raw_output, command=extra, show_command=True, empty_text="(无新增输出)")
            fallback = f"{alias} 控制台输出\n{'-' * 30}\n{fallback_output}"
            await _finish_image_or_text(
                mcsm,
                draw_console_output(alias, extra, raw_output, show_command=True, empty_text="(无新增输出)"),
                fallback,
            )
        else:
            await _finish_notice(mcsm, "Operation succeeded", (f"{alias} {op_name} completed",), "success")
    else:
        err = result.get("error", f"状态码 {status_code}")
        await _finish_notice(mcsm, f"{alias} {op_name} failed", (str(err),), "error")


# log

def _parse_log_args(parts: List[str]) -> tuple[str, Optional[int], Optional[str]]:
    alias = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    log_limit: Optional[int] = LOG_DEFAULT_ENTRIES
    seen_all = False
    seen_num = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-a", "--all"):
            if seen_num:
                return alias, log_limit, f"{LOG_USAGE}\n-a 不能与 -n 同时使用"
            seen_all = True
            log_limit = None
            i += 1
            continue
        value = ""
        if arg in ("-n", "--num"):
            if i + 1 >= len(args):
                return alias, log_limit, LOG_USAGE
            value = args[i + 1]
            i += 2
        elif arg.startswith("-n") and len(arg) > 2:
            value = arg[2:]
            i += 1
        elif arg.startswith("--num="):
            value = arg.split("=", 1)[1]
            i += 1
        else:
            return alias, log_limit, f"{LOG_USAGE}\n未知参数: {arg}"
        if seen_all:
            return alias, log_limit, f"{LOG_USAGE}\n-a 不能与 -n 同时使用"
        try:
            parsed = int(value)
        except ValueError:
            return alias, log_limit, LOG_USAGE
        if parsed < 1:
            return alias, log_limit, LOG_USAGE
        if parsed > LOG_MAX_ENTRIES:
            return alias, log_limit, f"日志条数不能超过 {LOG_MAX_ENTRIES}"
        seen_num = True
        log_limit = parsed
    return alias, log_limit, None


async def _cmd_log(bot: Bot, event: Event, group_id: str, user_id: str, alias: str, *, log_limit: Optional[int] = LOG_DEFAULT_ENTRIES):
    """查看实例控制台日志。"""
    if not alias:
        await _finish_error(mcsm, LOG_USAGE)
        return

    info = _store.get_instance(group_id, alias)
    if info is None:
        matches = _store.find_instance_by_name(group_id, alias)
        if not matches:
            await _finish_error(mcsm, f"Instance not found: {alias}")
            return
        if len(matches) > 1:
            await _finish_notice(mcsm, "Multiple instances matched", (f"{alias} matched multiple instances; enter the full alias.",), "warning")
            return
        alias = matches[0]
        info = _store.get_instance(group_id, alias)
        if info is None:
            await _finish_error(mcsm, "内部错误")
            return

    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    client = get_client(group_id)
    if not client:
        await _finish_error(mcsm, "当前群未绑定面板")
        return

    try:
        raw = await client.get_instance_output(info["uuid"], info["daemonId"], size=2048)
    except Exception as e:
        await _finish_notice(mcsm, "获取日志失败", (str(e),), "error")
        return

    if not raw:
        raw = "(无输出)"

    fallback_output = render_console_text(raw, max_entries=log_limit, mode="log", empty_text="(无输出)")
    fallback = f"{alias} 控制台日志\n{'-' * 30}\n{fallback_output}"
    await _finish_image_or_text(
        mcsm,
        draw_console_output(alias, "", raw, max_entries=log_limit, display_line_limit=None, mode="log"),
        fallback,
    )


# ── hide / unhide

async def _cmd_hide(
    bot: Bot, event: Event, group_id: str, user_id: str,
    alias: str, hidden: bool,
):
    action_text = "隐藏" if hidden else "显示"
    if not alias:
        await _finish_error(mcsm, f"用法: /mcsm {'hide' if hidden else 'unhide'} <别名>")
        return

    info = _store.get_instance(group_id, alias)
    if info is None:
        matches = _store.find_instance_by_name(group_id, alias)
        if not matches:
            await _finish_error(mcsm, f"Instance not found: {alias}")
            return
        if len(matches) > 1:
            await _finish_notice(mcsm, "Multiple instances matched", (f"{alias} matched multiple instances; enter the full alias.",), "warning")
            return
        alias = matches[0]

    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    ok = _store.set_hidden(group_id, alias, hidden)
    if ok:
        await _finish_notice(mcsm, f"Instance {action_text}", (f"Instance: {alias}",), "success")
    else:
        await _finish_error(mcsm, "操作失败")


async def _cmd_admin(
    bot: Bot, event: Event, group_id: str, user_id: str, parts: List[str],
):
    """Manage per-group MCSM administrators."""
    admin_parts = parts[1:]
    action = admin_parts[0].lower() if admin_parts else ""

    if action not in ("add", "del", "list", "delete", "remove"):
        await _finish_notice(
            mcsm,
            "用法错误",
            ("/mcsm admin add @某人", "/mcsm admin del @某人", "/mcsm admin list"),
            "error",
        )
        return

    if action in ("delete", "remove"):
        action = "del"

    if action == "list":
        admins = _store.get_admins(group_id)
        fallback = "本群 MCSM 管理员\n" + ("\n".join(f"  {a}" for a in admins) if admins else "  (未设置)")
        await _finish_image_or_text(mcsm, draw_admin_list("本群", admins), fallback)
        return

    perm_err = await _require_group_manager(bot, event, group_id, user_id)
    if perm_err:
        await _finish_error(mcsm, perm_err)
        return

    at_users = await _extract_at_users(event, bot)
    if not at_users:
        target_text = admin_parts[1] if len(admin_parts) > 1 else ""
        if target_text.isdigit():
            at_users = [target_text]
        else:
            await _finish_notice(mcsm, "请 @ 要操作的用户", (f"示例: /mcsm admin {action} @某人",), "error")
            return

    if action == "add":
        added, skipped = [], []
        for target_uid in at_users:
            if _store.add_admin(group_id, target_uid):
                added.append(target_uid)
            else:
                skipped.append(target_uid)
        msg = []
        if added:
            msg.append(f"已添加群管理员 {', '.join(added)}")
        if skipped:
            msg.append(f"已经是群管理员: {', '.join(skipped)}")
        await _finish_notice(mcsm, "MCSM admins updated", msg if msg else ["No changes"], "success")
        return

    if action == "del":
        removed, skipped = [], []
        for target_uid in at_users:
            if not _is_superuser(user_id) and target_uid == user_id:
                skipped.append(f"{target_uid}(不能移除自己)")
                continue
            if _store.remove_admin(group_id, target_uid):
                removed.append(target_uid)
            else:
                skipped.append(target_uid)
        msg = []
        if removed:
            msg.append(f"已移除群管理员 {', '.join(removed)}")
        if skipped:
            msg.append(f"跳过: {', '.join(skipped)}")
        await _finish_notice(mcsm, "MCSM admins updated", msg if msg else ["No changes"], "success")
        return


# 插件加载
logger.info("[MCSM] plugin loaded (per-group panel mode, image UI)")

