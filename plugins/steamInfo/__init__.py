from .draw import (
    check_font,
    fetch_avatar,
    image_to_bytes,
    draw_player_status,
    draw_start_gaming,
    draw_friends_status,
    draw_game_stats,
    simplize_steam_player_data,
    vertically_concatenate_images,
)
from .data_source import (
    BindData,
    SteamInfoData,
    ParentData,
    DisableParentData,
    format_display_name,
    repair_from_project_data,
)
from .steam import (
    get_steam_id,
    get_steam_users_info,
    get_user_data,
    find_steam_app,
    append_app_ambiguous_cache,
    delete_app_ambiguous_cache,
    delete_app_lookup_cache,
    delete_steam_app_alias,
    find_unambiguous_steam_app,
    get_steam_app_list,
    get_owned_game,
    fetch_app_icon,
    get_player_achievement_summary,
    is_distant_llm_game_name,
    is_steam_app_list_cache_stale,
    read_all_app_ambiguous_cache,
    read_all_app_lookup_cache,
    read_app_ambiguous_cache,
    read_steam_app_aliases,
    resolve_steam_app_candidates,
    suggest_steam_game_names,
    write_app_lookup_cache,
    write_steam_app_alias,
    STEAM_ID_OFFSET,
)
from .models import PlayerSummaries, Player
from .config import Config
from utils.entari_native import (
    Text, make_image, ChainMsg, SendDest, At, ArgVal, event_chain, event_user_id,
    account_adapter_name,
)
from utils.entari_native import timer
from utils.entari_native import prompt, get_bot, on_ready
import aiohttp
import asyncio
import re
import time
import tempfile
import base64
from io import BytesIO
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, urlparse
from loguru import logger
from PIL import Image as PILImage
from typing import Union, Optional, List, Dict, Any
from utils.entari_native import inject
from arclet.entari import Event, Account as Bot, Image
from configs.config import SYSTEM_PROXY, Config as GlobalConfig, _env
from configs.path_config import IMAGE_PATH
from utils.entari_native import cmd as _cmd, get_rest
from utils.image_executor import run_image_render
from utils.temp_files import schedule_temp_file_cleanup

# alconna/aptimer 已在 bot.py 中预加载，localstore 由 import 自动加载


STEAM_HELP_USAGE = ""
help = _cmd("steamhelp", aliases={"steam帮助"}, priority=5, block=True)
bind = _cmd("steambind", aliases={"绑定steam"}, priority=5, block=True)
unbind = _cmd("steamunbind", aliases={"解绑steam"}, priority=5, block=True)
info = _cmd("steaminfo", aliases={"steam信息"}, priority=5, block=True)
check = _cmd("steamcheck", aliases={"查看steam", "查steam"}, priority=5, block=True)
game = _cmd("steamgame", aliases={"steam游戏", "steam时长"}, priority=5, block=True)
enable = _cmd("steamenable", aliases={"启用steam"}, priority=5, block=True)
disable = _cmd("steamdisable", aliases={"禁用steam"}, priority=5, block=True)
update_parent_info = _cmd("steamupdate", aliases={"更新群信息", "steam_update_parent"}, priority=5, block=True)
set_nickname = _cmd("steamnickname", aliases={"steam昵称"}, priority=5, block=True)


steam_cache = _cmd("steamcache", aliases={"steam缓存"}, priority=5, block=True)


def _env_bool(key: str, default: bool = False) -> bool:
    value = _env(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, default))
    except (TypeError, ValueError):
        return default


config = Config(
    steam_api_key=str(_env("STEAM_API_KEY", "") or ""),
    steam_api_keys=str(_env("STEAM_API_KEYS", "") or ""),
    proxy=str(_env("HTTP_PROXY", "") or "") or None,
    steam_request_interval=_env_int("STEAM_REQUEST_INTERVAL", 120),
    steam_disable_broadcast_on_startup=_env_bool("STEAM_DISABLE_BROADCAST_ON_STARTUP", False),
    steam_llm_api_key=str(_env("STEAM_LLM_API_KEY", "") or ""),
    steam_llm_base_url=str(_env("STEAM_LLM_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com"),
    steam_llm_model=str(_env("STEAM_LLM_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash"),
)


_steam_data_dir = Path("data") / "steam_info_entari"
_steam_data_dir.mkdir(parents=True, exist_ok=True)
bind_data_path = _steam_data_dir / "bind_data.json"
steam_info_data_path = _steam_data_dir / "steam_info.json"
parent_data_path = _steam_data_dir / "parent_data.json"
disable_parent_data_path = _steam_data_dir / "disable_parent_data.json"
avatar_path = _steam_data_dir / "cache"
avatar_path.mkdir(parents=True, exist_ok=True)

bind_data = BindData(bind_data_path)
steam_info_data = SteamInfoData(steam_info_data_path)
parent_data = ParentData(parent_data_path)
disable_parent_data = DisableParentData(disable_parent_data_path)

_repair_stats = repair_from_project_data(
    Path("data/steam_info"), bind_data, steam_info_data, parent_data
)
if any(_repair_stats.values()):
    logger.info(
        "[steamInfo] repaired localstore from data/steam_info: "
        f"bind={_repair_stats['bind']}, "
        f"steam_info={_repair_stats['steam_info']}, "
        f"parent={_repair_stats['parent']}"
    )

try:
    check_font()
except FileNotFoundError as e:
    logger.error(
        f"{e}, steam_info_entari 无法使用，请参考 `https://github.com/zhaomaoniu/steam-info-entari` 配置字体文件"
    )


async def get_target(event: Event, bot: Bot) -> Optional[SendDest]:
    guild = getattr(event, "guild", None)
    channel = getattr(event, "channel", None)
    guild_id = str(getattr(guild, "id", "") or "")
    channel_id = str(getattr(channel, "id", "") or "")

    if not guild_id:
        return None

    return SendDest(
        channel_id or guild_id,
        guild_id,
        True,
        False,
        "",
        account_adapter_name(bot),
    )


def _parent_id(target: Optional[SendDest]) -> str:
    if target is None:
        return ""
    return target.parent_id or target.id


def _display_name(parent_id: str, player: Player) -> str:
    user_data = bind_data.get_by_steam_id(parent_id, player["steamid"])
    return format_display_name(
        player.get("personaname"), user_data.get("nickname") if user_data else None, player["steamid"]
    )


def _steam_api_keys():
    return config.steam_api_keys or config.steam_api_key


def _steam_proxy() -> Optional[str]:
    if config.proxy:
        return config.proxy
    if isinstance(SYSTEM_PROXY, dict):
        return SYSTEM_PROXY.get("http") or SYSTEM_PROXY.get("https")
    return None


def _steam_proxy_candidates() -> List[Optional[str]]:
    proxy = _steam_proxy()
    return [proxy] if proxy else [None]


def _steam_llm_config() -> Optional[dict]:
    api_key = getattr(config, "steam_llm_api_key", "")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": getattr(config, "steam_llm_base_url", "https://api.deepseek.com"),
        "model": getattr(config, "steam_llm_model", "deepseek-v4-flash"),
    }


async def _current_parent_info(
    event: Event, parent_id: str
) -> tuple[PILImage.Image, str]:
    return await _resolve_parent_info(None, parent_id, event)


def _get_attr_or_item(source: Any, key: str, default: Any = "") -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


async def _download_parent_avatar(avatar_url: str) -> Optional[PILImage.Image]:
    if not avatar_url:
        return None

    for request_proxy in _steam_proxy_candidates():
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(avatar_url, proxy=request_proxy) as resp:
                    if resp.status != 200:
                        continue
                    image = PILImage.open(BytesIO(await resp.read()))
                    image.load()
                    return image
        except Exception as exc:
            logger.debug(f"Failed to fetch group avatar {avatar_url}: {exc}")

    return None


async def _resolve_parent_info(
    bot: Optional[Bot], parent_id: str, event: Optional[Event] = None
) -> tuple[PILImage.Image, str]:
    fallback_avatar, fallback_name = parent_data.get(parent_id)
    parent_name = fallback_name
    avatar_url = ""

    guild = getattr(event, "guild", None) if event is not None else None
    if guild is not None:
        parent_name = str(_get_attr_or_item(guild, "name", "") or parent_name)
        avatar_url = str(_get_attr_or_item(guild, "avatar", "") or "")

    if bot is not None and (parent_name == parent_id or not avatar_url):
        guild_get = getattr(bot, "guild_get", None)
        if callable(guild_get):
            try:
                full_guild = await guild_get(guild_id=str(parent_id))
                parent_name = str(
                    _get_attr_or_item(full_guild, "name", "") or parent_name
                )
                avatar_url = str(
                    _get_attr_or_item(full_guild, "avatar", "") or avatar_url
                )
            except Exception as exc:
                logger.debug(f"Failed to fetch guild info {parent_id}: {exc}")

    if not avatar_url and parent_id.isdigit():
        if parent_name == parent_id or not parent_data.has_avatar(parent_id):
            avatar_url = f"https://p.qlogo.cn/gh/{parent_id}/{parent_id}/640"

    avatar = await _download_parent_avatar(avatar_url) if avatar_url else None
    if avatar is None:
        avatar = fallback_avatar

    if parent_name != fallback_name or avatar is not fallback_avatar:
        try:
            parent_data.update(parent_id, avatar.copy(), parent_name)
        except Exception as exc:
            logger.debug(f"Failed to cache parent info {parent_id}: {exc}")

    return avatar, parent_name


def _to_image_segment(image: PILImage.Image):
    return _image_bytes_to_segment(image_to_bytes(image))


def _image_bytes_to_segment(data: bytes):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(data)
        f.flush()
        schedule_temp_file_cleanup(f.name)
        return make_image(path=f.name)


def _render_image_bytes(renderer, *args, **kwargs) -> bytes:
    return image_to_bytes(renderer(*args, **kwargs))


async def _render_image_segment(renderer, *args, **kwargs):
    data = await run_image_render(_render_image_bytes, renderer, *args, **kwargs)
    return _image_bytes_to_segment(data)


def _render_start_gaming_cards(entries: list[tuple[PILImage.Image, str, str]]) -> bytes:
    images = [draw_start_gaming(avatar, name, game_name) for avatar, name, game_name in entries]
    image = vertically_concatenate_images(images) if len(images) > 1 else images[0]
    return image_to_bytes(image)


def _play_time_text(start_time: Optional[int]) -> Optional[str]:
    if not start_time:
        return None
    seconds = max(0, int(time.time()) - start_time)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def _format_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    hours = minutes // 60
    remain_minutes = minutes % 60
    if hours and remain_minutes:
        return f"{hours} 小时 {remain_minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{remain_minutes} 分钟"


def _format_minutes_compact(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    hours = minutes // 60
    remain_minutes = minutes % 60
    if hours and remain_minutes:
        return f"{hours}小时{remain_minutes}分"
    if hours:
        return f"{hours}小时"
    return f"{remain_minutes}分"


def _format_last_played(timestamp: int) -> str:
    if not timestamp:
        return "无记录"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _bound_players(parent_id: str) -> List[Dict[str, str]]:
    records = bind_data.content.get(parent_id, [])
    result = []
    seen = set()
    for record in records:
        steam_id = record.get("steam_id")
        if not steam_id or steam_id in seen:
            continue
        seen.add(steam_id)
        record.setdefault("nickname", None)
        result.append(record)
    return result


def _game_candidate_label(candidate: Any) -> str:
    if isinstance(candidate, dict):
        name = str(candidate.get("name") or candidate.get("appid") or "")
        appid = candidate.get("appid")
        return f"{name} ({appid})" if appid else name
    return str(candidate)


def _game_candidate_prompt(query: str, candidates: List[Any]) -> str:
    lines = ["可能的 Steam 游戏名不止一个，请回复序号或直接回复游戏名："]
    lines.extend(
        f"{index}. {_game_candidate_label(candidate)}"
        for index, candidate in enumerate(candidates, start=1)
    )
    lines.append(f"鍘熻緭鍏ワ細{query}")
    return "\n".join(lines)


def _resolve_candidate_reply(reply: str, candidates: List[Any]) -> Optional[Any]:
    reply = str(reply or "").strip()
    if not reply:
        return None
    if reply.isdigit():
        index = int(reply)
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        return None
    return reply


def _is_yes_reply(reply: str) -> bool:
    return str(reply or "").strip().casefold() in {"y", "yes", "ok", "true"}


def _is_no_reply(reply: str) -> bool:
    return str(reply or "").strip().casefold() in {"n", "no", "false", "cancel"}


async def _confirm_custom_game(
    original_query: str, custom_query: str, app: Optional[dict] = None
) -> Optional[dict]:
    if app is None:
        app = await find_steam_app(
            custom_query,
            avatar_path,
            _steam_proxy(),
            llm_config=_steam_llm_config(),
            write_query_cache=False,
            steam_api_key=_steam_api_keys(),
            allow_cache_match=False,
        )
    if app is None:
        await game.finish(f"未找到游戏 {custom_query}")

    answer = await prompt(
        f"Found {app.get('name') or app['appid']} ({app['appid']}). Reply yes or no.",
        timeout=45,
    )
    if answer is None:
        await game.finish("等待确认超时，请重新执行 steamgame 查询")
    text = answer.extract_plain_text()
    if _is_yes_reply(text):
        append_app_ambiguous_cache(original_query, avatar_path, app, "user_confirmed")
        return app
    if _is_no_reply(text):
        await game.finish("已取消本次查询")
    await game.finish("确认无效，请重新执行 steamgame 查询")


async def _resolve_steam_game_query(query: str) -> tuple[str, Optional[str], Optional[dict]]:
    started_at = time.perf_counter()
    refresh_started = False
    if _steam_api_keys() and is_steam_app_list_cache_stale(avatar_path):
        asyncio.create_task(
            get_steam_app_list(
                avatar_path,
                _steam_proxy(),
                _steam_api_keys(),
                force_refresh=True,
            )
        )
        refresh_started = True

    explicit_app = await find_unambiguous_steam_app(
        query, avatar_path, _steam_proxy(), prefer_cache=True
    )
    if explicit_app is not None:
        elapsed = time.perf_counter() - started_at
        logger.debug(
            f"[steamInfo] steamgame resolve query={query!r} direct in {elapsed:.3f}s "
            f"background_refresh={refresh_started}"
        )
        return str(explicit_app.get("name") or explicit_app["appid"]), None, explicit_app

    local_started = time.perf_counter()
    candidates: List[Any] = read_app_ambiguous_cache(query, avatar_path)
    force_confirm = bool(candidates)
    if not candidates:
        candidates = await resolve_steam_app_candidates(
            query,
            avatar_path,
            _steam_proxy(),
            llm_config=_steam_llm_config(),
            steam_api_key=_steam_api_keys(),
        )
        force_confirm = bool(candidates)
        if len(candidates) > 1:
            from .steam import write_app_ambiguous_cache

            write_app_ambiguous_cache(query, avatar_path, candidates)
    local_elapsed = time.perf_counter() - local_started

    if not candidates:
        llm_started = time.perf_counter()
        candidates = await suggest_steam_game_names(
            query, _steam_llm_config(), _steam_proxy()
        )
        logger.debug(
            f"[steamInfo] steamgame fallback suggest query={query!r} "
            f"elapsed={time.perf_counter() - llm_started:.3f}s"
        )
    if not candidates:
        app = await find_steam_app(
            query,
            avatar_path,
            _steam_proxy(),
            llm_config=_steam_llm_config(),
            write_query_cache=False,
            steam_api_key=_steam_api_keys(),
            allow_cache_match=False,
        )
        if app is None:
            elapsed = time.perf_counter() - started_at
            log = logger.warning if elapsed > 3 else logger.debug
            log(
                f"[steamInfo] steamgame resolve query={query!r} no result in {elapsed:.3f}s "
                f"candidate_stage={local_elapsed:.3f}s background_refresh={refresh_started}"
            )
            return query, None, None
        elapsed = time.perf_counter() - started_at
        logger.debug(
            f"[steamInfo] steamgame resolve query={query!r} needs confirm in {elapsed:.3f}s "
            f"candidate_stage={local_elapsed:.3f}s background_refresh={refresh_started}"
        )
        return await _confirm_custom_game(query, str(app.get("name") or app["appid"]), app)
    if len(candidates) == 1 and not force_confirm:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            return await _confirm_custom_game(query, str(candidate.get("name") or candidate["appid"]), candidate)
        if candidate.casefold() == query.casefold():
            app = await _confirm_custom_game(query, candidate)
            return str(app.get("name") or app["appid"]), candidate, app
        if is_distant_llm_game_name(query, candidate):
            return query, None, None
        app = await _confirm_custom_game(query, candidate)
        return str(app.get("name") or app["appid"]), candidate, app

    elapsed = time.perf_counter() - started_at
    log = logger.warning if elapsed > 3 else logger.debug
    log(
        f"[steamInfo] steamgame resolve query={query!r} candidates={len(candidates)} "
        f"in {elapsed:.3f}s candidate_stage={local_elapsed:.3f}s "
        f"background_refresh={refresh_started}"
    )
    reply = await prompt(_game_candidate_prompt(query, candidates), timeout=45)
    if reply is None:
        await game.finish("等待选择超时，请重新执行 steamgame 查询")
    selected = _resolve_candidate_reply(reply.extract_plain_text(), candidates)
    if selected is None:
        await game.finish("选择无效，请重新执行 steamgame 查询")
    if isinstance(selected, dict):
        append_app_ambiguous_cache(query, avatar_path, selected, selected.get("source", "user_selected"))
        return str(selected.get("name") or selected["appid"]), None, selected
    if selected not in candidates:
        app = await _confirm_custom_game(query, selected)
        return str(app.get("name") or app["appid"]), selected, app
    app = await _confirm_custom_game(query, selected)
    return str(app.get("name") or app["appid"]), selected, app


def _is_superuser(event: Event) -> bool:
    return str(event_user_id(event)) in {str(user_id) for user_id in GlobalConfig.SUPERUSERS}


def _format_cache_app(app: Any) -> str:
    if not isinstance(app, dict):
        return str(app)
    return f"{app.get('name') or app.get('appid')} ({app.get('appid')})"


def _cache_search_items(data: dict, keyword: str = "") -> List[tuple[str, Any]]:
    keyword = keyword.strip().casefold()
    items = []
    for key, value in sorted(data.items()):
        haystack = f"{key} {value}".casefold()
        if keyword and keyword not in haystack:
            continue
        items.append((key, value))
        if len(items) >= 20:
            break
    return items


def _cache_list_text(kind: str, items: List[tuple[str, Any]]) -> str:
    if not items:
        return f"{kind} cache: no matched items"
    lines = [f"{kind} cache:"]
    for key, value in items:
        if kind == "ambiguous":
            candidates = value.get("candidates", []) if isinstance(value, dict) else []
            rendered = ", ".join(_format_cache_app(app) for app in candidates[:5])
            lines.append(f"{key}: {rendered or 'empty'}")
        else:
            app = value.get("app") if kind == "lookup" and isinstance(value, dict) else value
            lines.append(f"{key}: {_format_cache_app(app)}")
    return "\n".join(lines)


def _parse_cache_app(args: List[str]) -> Optional[dict]:
    if len(args) < 2 or not args[0].isdigit():
        return None
    return {"appid": int(args[0]), "name": " ".join(args[1:]).strip() or args[0]}


def _steam_cache_usage() -> str:
    return (
        "Usage:\n"
        "/steamcache refresh\n"
        "/steamcache alias list [keyword]\n"
        "/steamcache alias set <alias> <appid> <name>\n"
        "/steamcache alias del <alias>\n"
        "/steamcache lookup list [keyword]\n"
        "/steamcache lookup set <query> <appid> <name>\n"
        "/steamcache lookup del <query>\n"
        "/steamcache ambiguous list [keyword]\n"
        "/steamcache ambiguous del <query>"
    )


async def to_image_data(image: Image) -> Union[BytesIO, bytes]:
    raw = getattr(image, "raw", None)
    if callable(raw):
        raw = None
    if raw is not None:
        return raw

    src = str(getattr(image, "src", "") or "")
    if src.startswith("data:"):
        header, _, payload = src.partition(",")
        if ";base64" not in header:
            raise ValueError("无法获取图片数据: unsupported data URL")
        return base64.b64decode(payload)

    if src.startswith("file://"):
        parsed = urlparse(src)
        file_path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", file_path):
            file_path = file_path[1:]
        return Path(file_path).read_bytes()

    if src.startswith(("http://", "https://")):
        async with aiohttp.ClientSession() as session:
            async with session.get(src, proxy=_steam_proxy()) as resp:
                if resp.status != 200:
                    raise ValueError(f"无法获取图片数据: {resp.status}")
                return await resp.read()

    raise ValueError("无法获取图片数据")


def _is_steam_broadcast_send_failure(exc: Exception) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "ApiNotImplementedException",
            "\u53d1\u9001\u5931\u8d25",
            "\u767c\u9001\u5931\u6557",
            "sendMsg",
            "message_create",
            "\u5df2\u88ab\u79fb\u51fa\u8be5\u7fa4",
            "\u5df2\u88ab\u79fb\u51fa\u8a72\u7fa4",
            "\u88ab\u79fb\u51fa\u8be5\u7fa4",
            "\u88ab\u79fb\u51fa\u8a72\u7fa4",
            "not implemented",
        )
    )


def _summarize_exception(exc: Exception, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


async def broadcast_steam_info(parent_id: str, steam_info: PlayerSummaries):
    if disable_parent_data.is_disabled(parent_id):
        return True

    bot = get_bot()

    play_data = steam_info_data.compare(parent_id, steam_info["response"])

    msg = []
    for entry in play_data:
        player: Player = entry["player"]
        old_player: Player = entry.get("old_player")
        display_name = _display_name(parent_id, player)
        if entry["type"] == "start":
            msg.append(
                f"{display_name} 开始玩 {player['gameextrainfo']} 了")
        elif entry["type"] == "stop":
            play_time_text = _play_time_text(old_player.get("game_start_time"))
            if play_time_text:
                msg.append(
                    f"{display_name} 玩了 {play_time_text} {old_player['gameextrainfo']} 后不玩了"
                )
            else:
                msg.append(
                    f"{display_name} 停止玩 {old_player['gameextrainfo']} 了")
        elif entry["type"] == "change":
            msg.append(
                f"{display_name} 停止玩 {old_player['gameextrainfo']}，开始玩 {player['gameextrainfo']} 了"
            )
        elif entry["type"] == "error":
            logger.error(
                "Steam broadcast diff error: player=%s new=%s old=%s",
                player.get("personaname"),
                player.get("gameextrainfo"),
                old_player.get("gameextrainfo"),
            )
        else:
            logger.error(f"未知的播报类型 {entry['type']}")

    if msg == []:
        return True

    if config.steam_broadcast_type == "all":
        steam_status_data = []
        for player in steam_info["response"]["players"]:
            item = await simplize_steam_player_data(player, _steam_proxy(), avatar_path)
            item["name"] = _display_name(parent_id, player)
            steam_status_data.append(item)

        parent_avatar, parent_name = await _resolve_parent_info(bot, parent_id)
        uni_msg = ChainMsg(
            [
                Text("\n".join(msg)),
                await _render_image_segment(
                    draw_friends_status,
                    parent_avatar,
                    parent_name,
                    steam_status_data,
                ),
            ]
        )
    elif config.steam_broadcast_type == "part":
        entries = [
            (
                await fetch_avatar(entry["player"], avatar_path, _steam_proxy()),
                _display_name(parent_id, entry["player"]),
                entry["player"]["gameextrainfo"],
            )
            for entry in play_data
            if entry["type"] == "start"
        ]
        if entries == []:
            uni_msg = ChainMsg([Text("\n".join(msg))])
        else:
            image_data = await run_image_render(_render_start_gaming_cards, entries)
            uni_msg = ChainMsg(
                [Text("\n".join(msg)), _image_bytes_to_segment(image_data)])
    else:
        uni_msg = ChainMsg([Text("\n".join(msg))])

    try:
        await uni_msg.send(
            SendDest(parent_id, parent_id, True, False,
                   "", account_adapter_name(bot)), bot
        )
        return True
    except Exception as exc:
        if _is_steam_broadcast_send_failure(exc):
            disable_parent_data.add(parent_id)
            disable_parent_data.save()
            logger.warning(
                f"[steamInfo] disabled Steam broadcast for parent={parent_id} after send failure: "
                f"{_summarize_exception(exc)}"
            )
            return False
        logger.exception(
            f"[steamInfo] failed to broadcast Steam info parent={parent_id}: "
            f"{_summarize_exception(exc)}"
        )
        return False


async def init_steam_info():
    for parent_id in list(bind_data.content):
        steam_ids = bind_data.get_all(parent_id)
        if not steam_ids:
            bind_data.content.pop(parent_id, None)
            bind_data.save()
            logger.info(f"[steamInfo] removed empty Steam bind parent={parent_id}")
            continue

        steam_info = await get_steam_users_info(
            steam_ids, _steam_api_keys(), _steam_proxy()
        )

        steam_info_data.update(parent_id, steam_info["response"])
        steam_info_data.save()


def _log_init_steam_info_result(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.error(
            f"[steamInfo] initial Steam refresh failed: {type(exc).__name__}"
        )


def _start_init_steam_info():
    task = asyncio.create_task(init_steam_info(), name="steam-info-initial-refresh")
    task.add_done_callback(_log_init_steam_info_result)


if not config.steam_disable_broadcast_on_startup:
    on_ready(_start_init_steam_info)
else:
    logger.info("已禁用启动时的 Steam 信息刷新")


@timer.scheduled_job(
    "interval", minutes=config.steam_request_interval / 60, id="update_steam_info"
)
async def update_steam_info():
    for parent_id in list(bind_data.content):
        steam_ids = bind_data.get_all(parent_id)
        if not steam_ids:
            bind_data.content.pop(parent_id, None)
            bind_data.save()
            logger.info(f"[steamInfo] skipped empty Steam bind parent={parent_id}")
            continue

        steam_info = await get_steam_users_info(
            steam_ids, _steam_api_keys(), _steam_proxy()
        )

        broadcast_ok = await broadcast_steam_info(parent_id, steam_info)
        if not broadcast_ok:
            continue

        steam_info_data.update(parent_id, steam_info["response"])
        steam_info_data.save()


@help.handle()
async def help_handle():
    await help.finish(__plugin_meta__.usage)


@steam_cache.handle()
async def steam_cache_handle(event: Event, rest: ArgVal[str]):
    if not _is_superuser(event):
        await steam_cache.finish("Permission denied")

    raw_arg = get_rest(rest)
    args = raw_arg.split()
    if not args:
        await steam_cache.finish(_steam_cache_usage())

    action = args[0].casefold()
    if action == "refresh":
        apps = await get_steam_app_list(
            avatar_path,
            _steam_proxy(),
            _steam_api_keys(),
            force_refresh=True,
        )
        await steam_cache.finish(f"Steam app list refreshed: {len(apps)} apps")

    if action not in {"alias", "lookup", "ambiguous"} or len(args) < 2:
        await steam_cache.finish(_steam_cache_usage())

    kind = action
    sub_action = args[1].casefold()
    sub_args = args[2:]

    if kind == "alias":
        if sub_action == "list":
            aliases = read_steam_app_aliases()
            await steam_cache.finish(_cache_list_text("alias", _cache_search_items(aliases, " ".join(sub_args))))
        if sub_action == "set" and len(sub_args) >= 3:
            alias = sub_args[0]
            app = _parse_cache_app(sub_args[1:])
            if app is None:
                await steam_cache.finish(_steam_cache_usage())
            write_steam_app_alias(alias, app)
            await steam_cache.finish(f"alias set: {alias.casefold()} -> {_format_cache_app(app)}")
        if sub_action == "del" and sub_args:
            alias = " ".join(sub_args)
            existed = delete_steam_app_alias(alias)
            await steam_cache.finish(f"alias {'deleted' if existed else 'not found'}: {alias.casefold()}")
        await steam_cache.finish(_steam_cache_usage())

    if kind == "lookup":
        if sub_action == "list":
            lookups = read_all_app_lookup_cache(avatar_path)
            await steam_cache.finish(_cache_list_text("lookup", _cache_search_items(lookups, " ".join(sub_args))))
        if sub_action == "set" and len(sub_args) >= 3:
            query = sub_args[0]
            app = _parse_cache_app(sub_args[1:])
            if app is None:
                await steam_cache.finish(_steam_cache_usage())
            write_app_lookup_cache(query, avatar_path, app)
            await steam_cache.finish(f"lookup set: {query.casefold()} -> {_format_cache_app(app)}")
        if sub_action == "del" and sub_args:
            query = " ".join(sub_args)
            existed = delete_app_lookup_cache(query, avatar_path)
            await steam_cache.finish(f"lookup {'deleted' if existed else 'not found'}: {query.casefold()}")
        await steam_cache.finish(_steam_cache_usage())

    if kind == "ambiguous":
        if sub_action == "list":
            ambiguous = read_all_app_ambiguous_cache(avatar_path)
            await steam_cache.finish(_cache_list_text("ambiguous", _cache_search_items(ambiguous, " ".join(sub_args))))
        if sub_action == "del" and sub_args:
            query = " ".join(sub_args)
            existed = delete_app_ambiguous_cache(query, avatar_path)
            await steam_cache.finish(f"ambiguous {'deleted' if existed else 'not found'}: {query.casefold()}")
        await steam_cache.finish(_steam_cache_usage())


@bind.handle()
async def bind_handle(
    event: Event,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    parent_id = _parent_id(target)
    if not parent_id:
        await bind.finish("Steam 插件仅支持群聊/频道使用")

    arg = get_rest(rest)

    if not arg.isdigit():
        await bind.finish(
            "请输入正确的 Steam ID 或 Steam 好友代码，格式: steambind [Steam ID 或 Steam好友代码]"
        )

    steam_id = get_steam_id(arg)

    if user_data := bind_data.get(parent_id, event_user_id(event)):
        user_data["steam_id"] = steam_id
        bind_data.save()

        await bind.finish(f"已更新你的 Steam ID 为 {steam_id}")
    else:
        bind_data.add(
            parent_id, {"user_id": event_user_id(event), "steam_id": steam_id, "nickname": None})
        bind_data.save()

        await bind.finish(f"已绑定你的 Steam ID 为 {steam_id}")


@unbind.handle()
async def unbind_handle(event: Event, target: Optional[SendDest] = inject(get_target)):
    parent_id = _parent_id(target)
    if not parent_id:
        await unbind.finish("Steam 插件仅支持群聊/频道使用")
    user_id = event_user_id(event)

    removed_records = bind_data.remove(parent_id, user_id)
    if removed_records:
        bind_data.save()
        steam_info_changed = steam_info_data.prune_players(
            parent_id, bind_data.get_all(parent_id)
        )
        if steam_info_changed:
            steam_info_data.save()
        logger.info(
            f"[steamInfo] unbound Steam user parent={parent_id} "
            f"user={user_id} removed={len(removed_records)}"
        )

        await unbind.finish("已解绑 Steam ID")
    else:
        await unbind.finish("未绑定 Steam ID")


@info.handle()
async def info_handle(
    bot: Bot,
    event: Event,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    parent_id = _parent_id(target)
    if not parent_id or target is None:
        await info.finish("Steam 插件仅支持群聊/频道使用")

    uni_arg = await ChainMsg.generate(message=event_chain(event), event=event, bot=bot)
    at = uni_arg[At]
    plain_arg = get_rest(rest)

    if at:
        user_data = bind_data.get(parent_id, at[0].target)
        if user_data is None:
            await info.finish("该用户未绑定 Steam ID")
        steam_id = user_data["steam_id"]
        steam_friend_code = str(int(steam_id) - STEAM_ID_OFFSET)
    elif plain_arg:
        if not plain_arg.isdigit():
            await info.finish("请输入正确的 Steam ID 或 Steam 好友代码")
        steam_id = get_steam_id(plain_arg)
        steam_friend_code = str(int(steam_id) - STEAM_ID_OFFSET)
    else:
        user_data = bind_data.get(parent_id, event_user_id(event))
        if user_data is None:
            await info.finish(
                "未绑定 Steam ID，请使用 steambind [Steam ID 或 Steam好友代码] 绑定 Steam ID"
            )
        steam_id = user_data["steam_id"]
        steam_friend_code = str(int(steam_id) - STEAM_ID_OFFSET)

    player_data = await get_user_data(int(steam_id), avatar_path, _steam_proxy())
    draw_data = [
        {
            "game_header": game["game_image"],
            "game_name": game["game_name"],
            "game_time": f"{game['play_time']} 小时" if game["play_time"] else "",
            "last_play_time": game["last_played"],
            "achievements": game["achievements"],
            "completed_achievement_number": game.get("completed_achievement_number"),
            "total_achievement_number": game.get("total_achievement_number"),
        }
        for game in player_data["game_data"]
    ]

    image = await _render_image_segment(
        draw_player_status,
        player_data["background"],
        player_data["avatar"],
        player_data["player_name"],
        steam_friend_code,
        player_data["description"],
        player_data["recent_2_week_play_time"],
        draw_data,
    )

    await ChainMsg([image]).send(target, bot)
    await info.finish()


@check.handle()
async def check_handle(
    bot: Bot,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    if get_rest(rest) != "":
        return None

    parent_id = _parent_id(target)
    if not parent_id or target is None:
        await check.finish("Steam 插件仅支持群聊/频道使用")

    steam_ids = bind_data.get_all(parent_id)

    steam_info = await get_steam_users_info(
        steam_ids, _steam_api_keys(), _steam_proxy()
    )

    logger.debug(f"{parent_id} Players info: {steam_info}")

    parent_avatar, parent_name = await _resolve_parent_info(bot, parent_id)

    steam_status_data = []
    for player in steam_info["response"]["players"]:
        item = await simplize_steam_player_data(player, _steam_proxy(), avatar_path)
        item["name"] = _display_name(parent_id, player)
        steam_status_data.append(item)

    image = await _render_image_segment(
        draw_friends_status,
        parent_avatar,
        parent_name,
        steam_status_data,
    )

    await ChainMsg([image]).send(target, bot)


@game.handle()
async def game_handle(
    bot: Bot,
    event: Event,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    parent_id = _parent_id(target)
    if not parent_id or target is None:
        await game.finish("Steam 插件仅支持群聊/频道使用")

    query = get_rest(rest)
    if not query:
        await game.finish("请输入游戏名或 appid，格式: steamgame [游戏名或 appid]")

    if not _steam_api_keys():
        await game.finish("未配置 Steam API Key，无法查询玩家游戏库数据")

    bound_players = _bound_players(parent_id)
    if not bound_players:
        await game.finish("本群还没有绑定 Steam ID")

    original_query = query
    query, confirmed_query, resolved_app = await _resolve_steam_game_query(query)
    app = resolved_app
    if app is None:
        await game.finish(f"未找到游戏 {query}")

    if confirmed_query and confirmed_query != original_query:
        write_app_lookup_cache(confirmed_query, avatar_path, app)
        write_steam_app_alias(confirmed_query, app)

    app_id = int(app["appid"])
    app_name = app.get("name") or str(app_id)
    steam_ids = [player["steam_id"] for player in bound_players]

    summaries = await get_steam_users_info(steam_ids, _steam_api_keys(), _steam_proxy())
    summary_map = {
        player["steamid"]: player
        for player in summaries.get("response", {}).get("players", [])
    }

    owned_games = await asyncio.gather(
        *[
            get_owned_game(steam_id, app_id, _steam_api_keys(), _steam_proxy())
            for steam_id in steam_ids
        ]
    )
    achievement_summaries = await asyncio.gather(
        *[
            get_player_achievement_summary(
                steam_id, app_id, _steam_api_keys(), _steam_proxy()
            )
            if owned_game is not None
            else asyncio.sleep(0, result=None)
            for steam_id, owned_game in zip(steam_ids, owned_games)
        ]
    )

    rows = []
    game_icon_hash = ""
    for record, owned_game, achievements in zip(
        bound_players, owned_games, achievement_summaries
    ):
        if owned_game is None:
            continue

        if not game_icon_hash:
            game_icon_hash = owned_game.get("img_icon_url") or ""

        steam_id = record["steam_id"]
        summary = summary_map.get(steam_id, {})
        name = (
            _display_name(parent_id, summary)
            if summary.get("steamid")
            else format_display_name(None, record.get("nickname"), steam_id)
        )
        avatar = (
            await fetch_avatar(summary, avatar_path, _steam_proxy())
            if summary.get("avatarfull")
            else PILImage.open(Path(IMAGE_PATH) / "steamInfo/unknown_avatar.jpg")
        )
        rows.append(
            {
                "name": name,
                "avatar": avatar,
                "game_name": owned_game.get("name"),
                "total": int(owned_game.get("playtime_forever", 0) or 0),
                "recent": int(owned_game.get("playtime_2weeks", 0) or 0),
                "last": int(owned_game.get("rtime_last_played", 0) or 0),
                "achievement_completed": (
                    achievements["completed"] if achievements else None
                ),
                "achievement_total": achievements["total"] if achievements else None,
            }
        )

    rows.sort(key=lambda item: item["total"], reverse=True)

    if not rows:
        await game.finish(
            f"No public playtime data found for {app_name} among {len(bound_players)} bound players."
        )

    total_players = len(rows)
    if app_name == str(app_id):
        app_name = next((item["game_name"] for item in rows if item["game_name"]), app_name)
    total_minutes = sum(item["total"] for item in rows)
    recent_minutes = sum(item["recent"] for item in rows)
    game_icon = PILImage.open(
        BytesIO(await fetch_app_icon(app_id, game_icon_hash, avatar_path, _steam_proxy()))
    )

    parent_avatar, parent_name = await _resolve_parent_info(bot, parent_id, event)
    image_rows = [
        {
            **item,
            "total_text": _format_minutes_compact(item["total"]),
            "recent_text": _format_minutes_compact(item["recent"]),
            "last_text": _format_last_played(item["last"]),
            "achievement_completed": item["achievement_completed"],
            "achievement_total": item["achievement_total"],
        }
        for item in rows[:20]
    ]
    image = await _render_image_segment(
        draw_game_stats,
        parent_avatar,
        parent_name,
        game_icon,
        app_name,
        app_id,
        total_players,
        len(bound_players),
        _format_minutes_compact(total_minutes),
        _format_minutes_compact(recent_minutes),
        image_rows,
    )

    await ChainMsg([image]).send(target, bot)
    await game.finish()


@update_parent_info.handle()
async def update_parent_info_handle(
    bot: Bot,
    event: Event,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    parent_id = _parent_id(target)
    if not parent_id:
        await update_parent_info.finish("Steam 插件仅支持群聊/频道使用")

    msg = await ChainMsg.generate(message=event_chain(event), event=event, bot=bot)
    info = {}
    name = get_rest(rest)
    if name:
        info["name"] = name
    for seg in msg:
        if isinstance(seg, Image):
            info["avatar"] = PILImage.open(BytesIO(await to_image_data(seg)))

    if "avatar" not in info or "name" not in info:
        await update_parent_info.finish("文本中应包含图片和文字")

    parent_data.update(parent_id, info["avatar"], info["name"])
    await update_parent_info.finish("鏇存柊鎴愬姛")


@enable.handle()
async def enable_handle(target: Optional[SendDest] = inject(get_target)):
    parent_id = _parent_id(target)
    if not parent_id:
        await enable.finish("Steam 插件仅支持群聊/频道使用")

    disable_parent_data.remove(parent_id)
    disable_parent_data.save()

    await enable.finish("已启用 Steam 播报")


@disable.handle()
async def disable_handle(target: Optional[SendDest] = inject(get_target)):
    parent_id = _parent_id(target)
    if not parent_id:
        await disable.finish("Steam 插件仅支持群聊/频道使用")

    disable_parent_data.add(parent_id)
    disable_parent_data.save()

    await disable.finish("已禁用 Steam 播报")


@set_nickname.handle()
async def set_nickname_handle(
    event: Event,
    rest: ArgVal[str],
    target: Optional[SendDest] = inject(get_target),
):
    parent_id = _parent_id(target)
    if not parent_id:
        await set_nickname.finish("Steam 插件仅支持群聊/频道使用")

    nickname = get_rest(rest)
    if not nickname:
        await set_nickname.finish("璇疯緭鍏ユ樀绉帮紝鏍煎紡: steamnickname [鏄电О]")

    user_data = bind_data.get(parent_id, event_user_id(event))
    if user_data is None:
        await set_nickname.finish("未绑定 Steam ID，请先使用 steambind 绑定 Steam ID 后再设置昵称")

    user_data["nickname"] = nickname
    bind_data.save()
    await set_nickname.finish(f"已设置你的 Steam 播报昵称为 {nickname}")

