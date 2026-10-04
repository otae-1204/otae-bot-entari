"""Manage plugin availability without accepting an external group ID."""

from __future__ import annotations

from loguru import logger

from otae_bot.adapters.entari import ArgVal, Session, cmd, get_rest
from otae_bot.adapters.feature_gate import loaded_group_plugins
from otae_bot.group_features import (
    PROTECTED_PLUGINS,
    SUPERUSER_ENABLE_PLUGINS,
    feature_store,
    plugin_label,
    resolve_plugin,
    scope_from_event,
)
from otae_bot.permissions import is_superuser

from .permissions import can_manage

HELP = """本群插件管理（仅限 SuperUser、本群管理员或群主）
/功能 列表 —— 查看本群各插件启用状态
/功能 开启 <插件名> —— 在本群启用指定插件（如 /功能 开启 hyw）
/功能 关闭 <插件名> —— 在本群禁用指定插件（如 /功能 关闭 hyw）
支持插件原名及常见别名（如 ef、steam、bili、mc、tibo）。
Grok Bot 默认关闭，仅 SuperUser 可执行 /功能 开启 grok（管理员与群主可关闭）。
设置仅作用于当前群且重启后仍保留，不支持跨群指定群号。
别名：/插件、/plugin。"""

feature_cmd = cmd("功能", aliases={"插件", "plugin"}, block=True)


@feature_cmd.handle()
async def handle_group_features(session: Session, rest: ArgVal[str]):
    scope = scope_from_event(session.account, session.event)
    if scope is None:
        await feature_cmd.finish("请在目标群聊内发送此命令，私聊环境无法调整群插件开关。")
        return
    if not await can_manage(session.account, session.event, scope):
        await feature_cmd.finish("权限不足：仅限 SuperUser、本群管理员或群主管理插件。")
        return
    parts = get_rest(rest).split()
    if not parts or parts == ["帮助"] or parts == ["help"]:
        await feature_cmd.finish(HELP)
        return
    available = loaded_group_plugins()
    try:
        if len(parts) == 1 and parts[0].casefold() in {"列表", "状态", "list", "status"}:
            lines = [f"本群插件状态（{scope.group_id}）"]
            for name in available:
                if name in PROTECTED_PLUGINS:
                    continue
                status = "开启" if feature_store.is_enabled(scope, name) else "关闭"
                restriction = "（仅 SuperUser 可开启）" if name in SUPERUSER_ENABLE_PLUGINS else ""
                lines.append(f"[{status}] {name} · {plugin_label(name)}{restriction}")
            lines.append("格式：/功能 开启 <插件名> 或 /功能 关闭 <插件名>")
            lines.append("注意：基础管理及全局核心插件不支持群内开关。")
            await feature_cmd.finish("\n".join(lines))
            return
        actions = {"开启": True, "启用": True, "打开": True, "on": True,
                   "enable": True, "关闭": False, "禁用": False, "off": False, "disable": False}
        if len(parts) != 2 or parts[0].casefold() not in actions:
            await feature_cmd.finish(HELP)
            return
        name = resolve_plugin(parts[1], available)
        if name is None:
            await feature_cmd.finish("未找到匹配的插件，请使用 /功能 列表 查看可用插件名称。")
            return
        if name in PROTECTED_PLUGINS:
            await feature_cmd.finish("基础管理及全局核心插件为系统内置保护插件，不支持群内开关。")
            return
        enabled = actions[parts[0].casefold()]
        if enabled and name in SUPERUSER_ENABLE_PLUGINS and not is_superuser(session.event):
            await feature_cmd.finish(f"权限受限：仅 SuperUser 可开启 {plugin_label(name)}，本群管理员与群主仅支持执行关闭。")
            return
        changed = feature_store.set_enabled(scope, name, enabled)
    except (OSError, ValueError) as exc:
        logger.error("[group_manager] switch storage failed error_type={}", type(exc).__name__)
        await feature_cmd.finish("插件开关配置保存失败，请联系管理员检查 data/group_manager/switches.json 及目录读写权限。")
        return
    status = "开启" if enabled else "关闭"
    if changed:
        logger.info("[group_manager] bot={} group={} user={} plugin={} enabled={}",
                    scope.self_id, scope.group_id, session.event.user.id, name, enabled)
    await feature_cmd.finish(
        f"本群已成功{status} {plugin_label(name)}（{name}）。"
        + ("配置已持久化保存。" if changed else "开关状态未发生改变。")
    )
