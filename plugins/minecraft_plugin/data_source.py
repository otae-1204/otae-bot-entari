"""Minecraft 插件数据管理层 — 以 address 为 key 的统一数据结构.

数据结构::

    plugin_data = {
        "group_server": {
            "<group_id>": {
                "<address>": {
                    "name": str,
                    "type": "java",
                    "nickname": [str, ...],
                    "broadcast": bool,
                    "player_gametime": {player_name: seconds, ...}
                }
            }
        }
    }
"""

import logging
from functools import wraps
from typing import Callable, Any, Optional, Dict, List, Union

from utils.plugin_data import Plugin_Data

logger = logging.getLogger(__name__)


def error_handler(default_return=None):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"函数 {func.__name__} 执行失败: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def async_error_handler(default_return=None):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"异步函数 {func.__name__} 执行失败: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


class MinecraftDataManager:
    """Minecraft 插件统一数据管理器（address 为 key）."""

    def __init__(self):
        self.pl_data = Plugin_Data("minecraft_plugin")
        self._init_data_structure()

    def _init_data_structure(self):
        pd = self.pl_data.plugin_data
        if 'group_server' not in pd:
            pd['group_server'] = {}
        if 'group_settings' not in pd or not isinstance(pd.get('group_settings'), dict):
            pd['group_settings'] = {}

        # 迁移旧格式：name-keyed → address-keyed
        for gid, servers in list(pd['group_server'].items()):
            if not isinstance(servers, dict):
                pd['group_server'][gid] = {}
                continue
            new_servers: dict = {}
            has_old = False
            for key, s in list(servers.items()):
                if not isinstance(s, dict):
                    continue
                addr = s.get('address', '').strip()
                if not addr:
                    continue
                # 如果 key 不是 address，需要迁移
                if key != addr:
                    has_old = True
                # 标准化字段
                new_servers[addr] = {
                    'name': s.get('name', key),
                    'type': s.get('type', 'java'),
                    'nickname': s.get('nickname', []) if isinstance(s.get('nickname'), list) else ([s['nickname']] if s.get('nickname') else []),
                    'broadcast': s.get('broadcast', False),
                    'player_gametime': s.get('player_gametime', {}),
                }
            if has_old and new_servers:
                pd['group_server'][gid] = new_servers
        self._save()

    # ── 辅助 ──

    @staticmethod
    def _vgroup(group_id: Union[int, str]) -> str:
        if not group_id:
            raise ValueError("群号不能为空")
        return str(group_id)

    @staticmethod
    def _vname(name: str) -> str:
        if not name or not name.strip():
            raise ValueError("名称不能为空")
        return name.strip()

    def _group(self, group_id: str) -> dict:
        gid = str(group_id)
        if gid not in self.pl_data.plugin_data['group_server']:
            self.pl_data.plugin_data['group_server'][gid] = {}
        return self.pl_data.plugin_data['group_server'][gid]

    def _settings(self, group_id: int | str) -> dict:
        gid = self._vgroup(group_id)
        settings = self.pl_data.plugin_data.setdefault('group_settings', {})
        group_settings = settings.get(gid)
        if not isinstance(group_settings, dict):
            group_settings = {}
            settings[gid] = group_settings
        return group_settings

    def _find_by_name(self, group_id: str, name: str) -> Optional[tuple[str, dict]]:
        """按 name 字段查找，返回 (address, server_dict) 或 None."""
        normalized_name = name.strip().casefold()
        # 精确匹配
        for addr, s in self._group(group_id).items():
            if str(s.get('name', '')).strip().casefold() == normalized_name:
                return addr, s
        # 模糊匹配
        for addr, s in self._group(group_id).items():
            if normalized_name in str(s.get('name', '')).strip().casefold():
                return addr, s
        return None

    def _find_by_nickname(self, group_id: str, nick: str) -> Optional[tuple[str, dict]]:
        """按 nickname 查找."""
        normalized_nick = nick.strip().casefold()
        for addr, s in self._group(group_id).items():
            nicknames = s.get('nickname', [])
            if isinstance(nicknames, str):
                nicknames = [nicknames]
            if any(str(item).strip().casefold() == normalized_nick for item in nicknames):
                return addr, s
        return None

    def _find_by_identifier(self, group_id: str, identifier: str) -> Optional[tuple[str, dict]]:
        """按地址、名称或 nickname 查找，返回 (address, server_dict) 或 None."""
        ident = identifier.strip()
        if not ident:
            return None
        group = self._group(group_id)
        if ident in group:
            return ident, group[ident]
        normalized_ident = ident.casefold()
        for addr, server in group.items():
            if addr.casefold() == normalized_ident:
                return addr, server
        found = self._find_by_name(group_id, ident)
        if found:
            return found
        return self._find_by_nickname(group_id, ident)

    def _save(self):
        self.pl_data.save_plugin_data()

    # ── 服务器 CRUD ──

    @error_handler(False)
    def add_group_server(self, group_id: int | str, server_name: str,
                         server_address: str, server_type: str = 'java') -> bool:
        gid = self._vgroup(group_id)
        name = self._vname(server_name)
        addr = server_address.strip()
        if not addr:
            raise ValueError("服务器地址不能为空")

        group = self._group(gid)
        if addr in group:
            logger.warning(f"地址 {addr} 已存在于群 {gid}（名: {group[addr].get('name')}）")
            return False

        group[addr] = {
            "name": name,
            "type": server_type,
            "nickname": [],
            "broadcast": False,
            "player_gametime": {},
        }
        self._save()
        logger.info(f"添加服务器 {name} ({addr}) → 群 {gid}")
        return True

    @error_handler(False)
    def remove_group_server(self, group_id: str, server_identifier: str) -> bool:
        """按名称或地址移除服务器."""
        gid = self._vgroup(group_id)
        ident = server_identifier.strip()
        if not ident:
            return False
        group = self._group(gid)
        # 先尝试地址 key
        if ident in group:
            del group[ident]
            self._save()
            return True
        # 再按名称查找
        found = self._find_by_identifier(gid, ident)
        if found:
            addr, _ = found
            del group[addr]
            if not group:
                del self.pl_data.plugin_data['group_server'][gid]
            self._save()
            return True
        return False

    @error_handler(False)
    def update_server_address(self, group_id: str, server_identifier: str, new_address: str) -> bool:
        """修改服务器地址（移动 key）."""
        gid = self._vgroup(group_id)
        new_addr = new_address.strip()
        if not new_addr:
            return False
        group = self._group(gid)
        ident = server_identifier.strip()

        found = self._find_by_identifier(gid, ident)
        if not found:
            return False
        old_addr, _ = found
        if new_addr in group:
            return False

        group[new_addr] = group.pop(old_addr)
        self._save()
        return True

    @error_handler(False)
    def update_server_name(self, group_id: str, server_identifier: str, new_name: str) -> bool:
        """修改服务器名称（改 name 字段，不改 key）."""
        gid = self._vgroup(group_id)
        ident = server_identifier.strip()
        found = self._find_by_identifier(gid, ident)
        if not found:
            return False
        _, s = found
        s['name'] = new_name.strip()
        self._save()
        return True

    @error_handler(False)
    def update_server_nickname(self, group_id: str, server_name: str, server_nickname: str) -> bool:
        gid = self._vgroup(group_id)
        ident = server_name.strip()
        found = self._find_by_identifier(gid, ident)
        if not found:
            return False
        _, s = found
        nick_list = s.setdefault('nickname', [])
        if server_nickname:
            if server_nickname not in nick_list:
                nick_list.append(server_nickname)
        else:
            s['nickname'] = []
        self._save()
        return True

    @error_handler(False)
    def remove_server_nickname(self, group_id: str, server_name: str) -> bool:
        gid = self._vgroup(group_id)
        ident = server_name.strip()
        found = self._find_by_identifier(gid, ident)
        if not found:
            return False
        _, s = found
        s['nickname'] = []
        self._save()
        return True

    # ── 查询 ──

    def get_group_servers(self, group_id: int | str) -> Dict[str, dict]:
        """获取群服务器 dict {address: {name, ...}}."""
        return dict(self._group(self._vgroup(group_id)))

    def get_group_serverlist(self, group_id: int | str) -> Optional[List[Dict]]:
        """获取群服务器列表（兼容旧接口）."""
        servers = self.get_group_servers(group_id)
        if not servers:
            return None
        return [{"name": s["name"], "address": addr, **s} for addr, s in servers.items()]

    def get_server_by_address(self, group_id: int | str, server_address: str) -> Optional[Dict]:
        s = self._group(self._vgroup(group_id)).get(server_address.strip())
        if s:
            return {"name": s["name"], "address": server_address.strip(), **s}
        return None

    def get_server_by_name(self, group_id: int | str, server_name: str) -> Optional[Dict]:
        found = self._find_by_name(self._vgroup(group_id), server_name.strip())
        if found:
            addr, s = found
            return {"name": s["name"], "address": addr, **s}
        return None

    def get_server_by_nickname(self, group_id: int | str, server_nick: str) -> Optional[Dict]:
        found = self._find_by_nickname(self._vgroup(group_id), server_nick.strip())
        if found:
            addr, s = found
            return {"name": s["name"], "address": addr, **s}
        return None

    def get_server_by_identifier(self, group_id: int | str, identifier: str) -> Optional[Dict]:
        """按地址、名称或 nickname 查找，地址优先."""
        found = self._find_by_identifier(self._vgroup(group_id), identifier.strip())
        if found:
            addr, s = found
            return {"name": s["name"], "address": addr, **s}
        return None

    # ── 广播标记 ──

    def set_broadcast(self, group_id: int | str, server_identifier: str, enabled: bool) -> bool:
        gid = self._vgroup(group_id)
        ident = server_identifier.strip()
        found = self._find_by_identifier(gid, ident)
        if not found:
            return False
        _, s = found
        s['broadcast'] = enabled
        self._save()
        return True

    def get_broadcast_servers(self, group_id: int | str) -> Dict[str, dict]:
        """获取群内所有启用了广播的服务器. {address: {name, ...}}"""
        result = {}
        for addr, s in self.get_group_servers(group_id).items():
            if s.get('broadcast'):
                result[addr] = {"name": s["name"], "address": addr, **s}
        return result

    def get_all_broadcast_servers(self) -> Dict[str, Dict[str, dict]]:
        result: Dict[str, Dict[str, dict]] = {}
        for gid in self.pl_data.plugin_data['group_server']:
            bs = self.get_broadcast_servers(gid)
            if bs:
                result[gid] = bs
        return result

    # ── 玩家游戏时间 ──

    def get_group_broadcast_interval(self, group_id: int | str, default: int | None = None) -> int | None:
        value = self._settings(group_id).get('broadcast_interval')
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return default
        return seconds if seconds > 0 else default

    def set_group_broadcast_interval(self, group_id: int | str, seconds: int) -> bool:
        seconds = int(seconds)
        if seconds <= 0:
            return False
        self._settings(group_id)['broadcast_interval'] = seconds
        self._save()
        return True

    def reset_group_broadcast_interval(self, group_id: int | str) -> bool:
        gid = self._vgroup(group_id)
        settings = self._settings(group_id)
        existed = 'broadcast_interval' in settings
        settings.pop('broadcast_interval', None)
        if not settings:
            self.pl_data.plugin_data.get('group_settings', {}).pop(gid, None)
        self._save()
        return existed

    def _gametime(self, group_id: str, addr: str) -> dict:
        s = self._group(group_id).get(addr)
        if not s:
            return {}
        return s.setdefault('player_gametime', {})

    def get_player_gametime(self, player_name: str, group_id: int | str, identifier: str) -> int:
        gid = str(group_id)
        found = self._find_by_identifier(gid, identifier)
        if not found:
            return 0
        _, s = found
        return s.get('player_gametime', {}).get(str(player_name), 0)

    def set_player_gametime(self, player_name: str, group_id: int | str, identifier: str, seconds: int):
        gid = str(group_id)
        found = self._find_by_identifier(gid, identifier)
        if not found:
            return
        _, s = found
        s.setdefault('player_gametime', {})[str(player_name)] = seconds
        self._save()

    def add_player_gametime(self, player_name: str, group_id: int | str, identifier: str, seconds: int):
        current = self.get_player_gametime(player_name, group_id, identifier)
        self.set_player_gametime(player_name, group_id, identifier, current + seconds)

    def get_server_player_gametimes(self, group_id: int | str, identifier: str) -> Dict[str, int]:
        gid = str(group_id)
        found = self._find_by_identifier(gid, identifier)
        if not found:
            return {}
        _, s = found
        return dict(s.get('player_gametime', {}))

    def get_top_players(self, group_id: int, identifier: str, limit: int = 10) -> List[Dict]:
        players = self.get_server_player_gametimes(group_id, identifier)
        if not players:
            return []
        sorted_players = sorted(players.items(), key=lambda x: x[1], reverse=True)
        if limit > 0:
            sorted_players = sorted_players[:limit]
        result = []
        for i, (pid, t) in enumerate(sorted_players, 1):
            result.append({
                "rank": i, "player_id": pid, "gametime": t,
                "formatted_time": self.format_time(t),
            })
        return result

    def get_all_servers_player_gametimes(self, group_id: int | str) -> Dict[str, int]:
        """获取群内所有服务器的玩家游戏时间总和. {player_name: total_seconds}"""
        combined: Dict[str, int] = {}
        for addr, s in self._group(self._vgroup(group_id)).items():
            for pname, seconds in s.get('player_gametime', {}).items():
                combined[pname] = combined.get(pname, 0) + seconds
        return combined

    def get_top_players_all_servers(self, group_id: int, limit: int = 10) -> List[Dict]:
        """获取群内所有服务器的玩家游戏时间排行榜（跨服务器汇总）."""
        players = self.get_all_servers_player_gametimes(group_id)
        if not players:
            return []
        sorted_players = sorted(players.items(), key=lambda x: x[1], reverse=True)
        if limit > 0:
            sorted_players = sorted_players[:limit]
        result = []
        for i, (pid, t) in enumerate(sorted_players, 1):
            result.append({
                "rank": i, "player_id": pid, "gametime": t,
                "formatted_time": self.format_time(t),
            })
        return result

    @staticmethod
    def format_time(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}秒"
        elif seconds < 3600:
            return f"{seconds // 60}分钟"
        else:
            h, m = divmod(seconds, 3600)
            return f"{h}小时{m // 60}分钟"


# ── 兼容别名 ──

class BroadcastManager:
    """兼容旧代码的广播管理器包装."""

    def __init__(self, data_manager: MinecraftDataManager):
        self.data_manager = data_manager

    async def get_all_broadcast_servers(self) -> dict:
        return self.data_manager.get_all_broadcast_servers()

    async def get_group_broadcast_servers(self, group_id: int | str) -> Optional[dict]:
        result = self.data_manager.get_broadcast_servers(group_id)
        return result if result else None

    async def add_broadcast_server_by_name(self, group_id: int | str, server_name: str, broadcast_name: str) -> dict:
        gid = str(group_id)
        existing = self.data_manager.get_server_by_name(gid, server_name)
        if not existing:
            return {'status': 'error', 'message': '服务器不存在'}
        addr = existing['address']
        if broadcast_name != existing['name']:
            existing['name'] = broadcast_name
            self.data_manager.update_server_name(gid, addr, broadcast_name)
        self.data_manager.set_broadcast(gid, addr, True)
        return {'status': 'success', 'message': '添加成功'}

    async def add_broadcast_server_by_address(self, group_id: int | str, server_address: str, broadcast_name: str) -> dict:
        gid = str(group_id)
        addr = server_address.strip()
        existing = self.data_manager.get_server_by_address(gid, addr)
        if not existing:
            self.data_manager.add_group_server(gid, broadcast_name, addr, 'java')
        self.data_manager.set_broadcast(gid, addr, True)
        return {'status': 'success', 'message': '添加成功'}

    async def remove_broadcast_server(self, group_id: int | str, server_name: str) -> dict:
        self.data_manager.set_broadcast(group_id, server_name, False)
        return {'status': 'success', 'message': '删除成功'}

    async def update_broadcast_server_address(self, group_id: int | str, server_name: str, new_address: str) -> dict:
        if self.data_manager.update_server_address(group_id, server_name, new_address):
            return {'status': 'success', 'message': '更新成功'}
        return {'status': 'error', 'message': '操作失败'}

    async def update_broadcast_server_name(self, group_id: int | str, old_name: str, new_name: str) -> dict:
        if self.data_manager.update_server_name(group_id, old_name, new_name):
            return {'status': 'success', 'message': '更新成功'}
        return {'status': 'error', 'message': '操作失败'}

    async def get_broadcast_server_by_name(self, group_id: int | str, server_name: str) -> Optional[dict]:
        return self.data_manager.get_server_by_name(group_id, server_name)

    async def get_broadcast_server_by_address(self, group_id: int | str, server_address: str) -> Optional[dict]:
        return self.data_manager.get_server_by_address(group_id, server_address)


class PlayerGameTimeManager:
    """兼容旧代码的游戏时间管理器包装."""

    def __init__(self, data_manager: MinecraftDataManager = None):
        self._dm = data_manager

    def _dm_ensure(self):
        if self._dm is None:
            self._dm = MinecraftDataManager()
        return self._dm

    def add_player_gametime(self, player_name: str, group_id: int, server_name: str, gametime: int):
        self._dm_ensure().add_player_gametime(player_name, group_id, server_name, gametime)

    def get_player_gametime(self, player_id, group_id: int, server_name: str) -> int:
        return self._dm_ensure().get_player_gametime(str(player_id), group_id, server_name)

    def get_all_player_gametime(self, group_id: int | str, server_name: str) -> Dict:
        return self._dm_ensure().get_server_player_gametimes(group_id, server_name)

    def get_top_players(self, group_id: int, server_name: str, limit: int = 10) -> List[Dict]:
        return self._dm_ensure().get_top_players(group_id, server_name, limit)

    def format_time(self, seconds: int) -> str:
        return MinecraftDataManager.format_time(seconds)
