from mcstatus import JavaServer, BedrockServer
import asyncio
import re
import base64
import time
from PIL import Image
from PIL.Image import Image as PILImage
from io import BytesIO
import traceback


ANONYMOUS_PLAYER_NAMES = {"anonymous player"}


def _extract_player_sample(players_data: dict) -> tuple[list[str], bool]:
    """Extract visible player names and detect anonymized sample placeholders."""
    sample = players_data.get("sample") if isinstance(players_data, dict) else None
    if not isinstance(sample, list):
        return [], False

    players: list[str] = []
    seen = set()
    anonymous_count = 0
    for item in sample:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
        else:
            name = str(item or "").strip()
        if not name:
            continue
        if name.casefold() in ANONYMOUS_PLAYER_NAMES:
            anonymous_count += 1
            continue
        if name not in seen:
            players.append(name)
            seen.add(name)

    return players, bool(sample) and anonymous_count > 0 and not players


def _lookup_and_status(server_address: str, server_type: str) -> dict:
    """同步执行 mcstatus 查询（在 asyncio.to_thread 中运行）"""
    if server_type == "java":
        server = JavaServer.lookup(server_address)
    elif server_type == "bedrock":
        server = BedrockServer.lookup(server_address)
    else:
        return {"type": "typeError", "data": "服务器类型错误"}
    start_time = time.time()
    status = vars(server.status())["raw"]
    return {"server": server, "status": status, "latency": int((time.time() - start_time) * 1000)}


async def ping(server_address: str, server_type: str = "java") -> dict:
    try:
        raw = await asyncio.to_thread(_lookup_and_status, server_address, server_type)
        if raw.get("type") == "typeError":
            return raw
        status = raw["status"]
        latency = raw["latency"]

        # 判断是否为原版服务器
        vanilla_keys = {"version", "players", "description", "favicon", "enforcesSecureChat", "previewsChat"}
        flag = all(k in vanilla_keys for k in status)

        # 获取服务器图标
        favicon: str | None = status.get("favicon")
        if favicon is not None:
            favicon = re.sub("^data:image/.+;base64,", "", favicon)

        # 获取服务器版本信息
        game_ver = status.get('version', {}).get('name')

        # 获取在线玩家数量
        online_players = status.get('players', {}).get('online')

        # 获取最大玩家数量
        max_players = status.get('players', {}).get('max')

        players, players_hidden = _extract_player_sample(status.get("players", {}))
        players_hidden = players_hidden and bool(online_players)

        # 获取服务器Motd
        motd = status.get('description')
        if isinstance(motd, dict):
            motd = motd.get('text')

        server_info = {
            "game_version": game_ver if game_ver is not None else "未知版本",
            "is_vanilla": flag,
            "online_players": online_players if online_players is not None else 0,
            "max_players": max_players if max_players is not None else 0,
            "motd": motd if motd is not None else "未知格式MOTD",
            "favicon": favicon,
            "server_type": server_type,
            "players": players,
            "players_hidden": players_hidden,
            "latency": latency,
            "protocol": status.get('version', {}).get('protocol', 0),
        }
        return {"status": "success", "data": server_info}

    except OSError:
        return {"status": "error", "data": "服务器未开启或服务器地址错误"}
    except Exception:
        traceback.print_exc()
        return {"status": "error", "data": "出现未知错误"}
def base64_to_image(base64_str: str) -> PILImage:
    """
    将base64字符串转换为PIL Image对象
    :param base64_str: str - base64字符串
    :return: Image - PIL Image对象
    """
    base64_data = re.sub('^data:image/.+;base64,', '', base64_str)
    image = Image.open(BytesIO(base64.b64decode(base64_data)))
    return image

