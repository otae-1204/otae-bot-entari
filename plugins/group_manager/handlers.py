"""Manage plugin availability without accepting an external group ID."""

from __future__ import annotations

from loguru import logger

from otae_bot.adapters.entari import ArgVal, Session, cmd, get_rest
from otae_bot.adapters.feature_gate import loaded_group_plugins
from otae_bot.group_features import (
    PROTECTED_PLUGINS,
    feature_store,
    plugin_label,
    resolve_plugin,
    scope_from_event,
)

from .permissions import can_manage

HELP = """本群插件开关（仅 SuperUser、本群管理员或群主可用）
/功能 列表：查看本群插件状态
/功能 关闭 hyw：关闭本群 HYW 问答
/功能 开启 hyw：恢复本群 HYW 问答
可使用插件名或别名，如 ef、steam、bili、mc、tibo。
只影响当前机器人在本群的功能，重启后保留；不接受群号参数。
别名：/插件、/plugin。"""

feature_cmd = cmd("功能", aliases={"插件", "plugin"}, block=True)


@feature_cmd.handle()
async def handle_group_features(session: Session, rest: ArgVal[str]):
    scope = scope_from_event(session.account, session.event)
    if scope is None:
        await feature_cmd.finish("请在需要管理的群内使用此命令，私聊不能修改群开关。")
        return
    if not await can_manage(session.account, session.event, scope):
        await feature_cmd.finish("仅 SuperUser、本群管理员或群主可管理插件；未能确认你的管理权限。")
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
                lines.append(f"[{status}] {name} · {plugin_label(name)}")
            lines.append("用法：/功能 关闭 hyw 或 /功能 开启 hyw")
            lines.append("群管理和全局请求处理插件不支持群开关。")
            await feature_cmd.finish("\n".join(lines))
            return
        actions = {"开启": True, "启用": True, "打开": True, "on": True,
                   "enable": True, "关闭": False, "禁用": False, "off": False, "disable": False}
        if len(parts) != 2 or parts[0].casefold() not in actions:
            await feature_cmd.finish(HELP)
            return
        name = resolve_plugin(parts[1], available)
        if name is None:
            await feature_cmd.finish("未找到该插件，请用 /功能 列表 查看可用插件名。")
            return
        if name in PROTECTED_PLUGINS:
            await feature_cmd.finish("群管理和全局请求处理插件不支持群开关。")
            return
        enabled = actions[parts[0].casefold()]
        changed = feature_store.set_enabled(scope, name, enabled)
    except (OSError, ValueError) as exc:
        logger.error("[group_manager] switch storage failed error_type={}", type(exc).__name__)
        await feature_cmd.finish("开关配置读写失败，请管理员检查 data/group_manager/switches.json 和目录权限。")
        return
    status = "开启" if enabled else "关闭"
    if changed:
        logger.info("[group_manager] bot={} group={} user={} plugin={} enabled={}",
                    scope.self_id, scope.group_id, session.event.user.id, name, enabled)
    await feature_cmd.finish(
        f"本群已{status} {plugin_label(name)}（{name}）。"
        + ("配置已保存。" if changed else "状态未变化。")
    )
