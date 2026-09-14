"""AI 智商雷达的命令面。

职责边界（plugin_api §7）—— 这里**只做四件事**：

1. 解析参数（命令词 → service 调用）；
2. 调 service；
3. 调 formatters；
4. ``session.send``（与 ``plugins/hyw`` 一致的分段发送）。

**不做**：不缓存结果（缓存在 provider）、不注册任何定时推送、不直接碰 provider、
不在 import 期读 ``.env`` / 发网络请求（``tests/test_architecture.py`` 会在无 ``.env``
的临时沙箱里 import 本模块）。

两个保护：

- **全局并发闸门**：``config.concurrency``（默认 4）。准入与占位之间**不能有 await**，
  否则闸门形同虚设（``plugins/hyw/handlers.py`` 的同款写法）。
- **单命令超时**：默认 30 s，``/table`` 类（档位）放宽到 60 s。

命令面按用户要求收窄为「只看智商相关」（2026-09-14）：保留 7 个 IQ 命令
（榜 / 模型 / 对比 / 推荐 / 预警 / 性价比 / 趋势）+ 频道 + 档位 + 帮助。
被摘掉的 5 个（题 / 好题 / 贡献者 / 流水 / 实时）只摘命令面，
``service`` / ``provider`` / ``formatters`` 里的对应能力原样保留给前端使用。
"""

from __future__ import annotations

import asyncio

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Event, Session
from loguru import logger
from nepattern import AnyString

from otae_bot.adapters.entari import ArgVal, get_rest, on_alconna, send

from .config import RadarConfig
from .errors import InvalidArgument, RadarError
from .formatters import (
    format_alerts,
    format_benchmark_list,
    format_comparison,
    format_error,
    format_help,
    format_meta_footer,
    format_model_catalog,
    format_model_list,
    format_model_profile,
    format_recommendations,
    format_trend,
    format_value_picks,
)
from .provider import RadarClient
from .service import RadarService

#: 单段回复上限（与 hyw 一致：上游客户端对超长消息不友好）。
CHUNK_SIZE = 2000

#: 命令别名。``Alconna`` 的名字不带前导 ``/``。
ROOT_ALIASES = ["radar", "智商雷达"]

#: 需要 ``/table``（8.6 MB）的子命令，超时放宽到 ``table_timeout``。
_TABLE_COMMANDS = {"档位", "combos"}

#: 分派表：子命令（含英文别名）→ 内部键。写成表是为了让未知子命令的报错能列出合法值。
#:
#: **命令面按用户要求收窄为「只看智商相关」**（2026-09-14）：只保留 7 个 IQ 命令
#: （榜/模型/对比/推荐/预警/性价比/趋势）+ 频道 + 档位 + 帮助。
#: 被摘掉的 5 个（题 / 好题 / 贡献者 / 流水 / 实时）**只摘命令面**——
#: ``service.py`` / ``provider.py`` / ``formatters.py`` 里的对应能力原样保留，
#: 前端 AI 仍可经 ``RadarService`` 取到这些数据（见 docs/ai_radar_frontend_api.md §19）。
_SUBCOMMANDS: dict[str, str] = {
    "榜": "rank",
    "rank": "rank",
    "模型": "model",
    "model": "model",
    "对比": "compare",
    "vs": "compare",
    "推荐": "recommend",
    "rec": "recommend",
    "预警": "alert",
    "alert": "alert",
    "性价比": "value",
    "value": "value",
    "趋势": "trend",
    "trend": "trend",
    "频道": "bench",
    "bench": "bench",
    "档位": "combos",
    "combos": "combos",
    "帮助": "help",
    "help": "help",
}

_config = RadarConfig.from_env()
_client = RadarClient(_config)
_service = RadarService(_client, _config)

#: 在飞命令计数。准入判断与占位之间不能 await。
_active: dict[str, int] = {}


def _chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """按字符数切段（按行切，不把一行劈成两半）。"""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buffer = ""
    for line in text.split("\n"):
        if buffer and len(buffer) + len(line) + 1 > size:
            chunks.append(buffer)
            buffer = line
        else:
            buffer = f"{buffer}\n{line}" if buffer else line
    if buffer:
        chunks.append(buffer)
    return chunks


async def _reply(session: Session, lines: list[str]) -> None:
    """把 ``list[str]`` 分段发出。"""
    for chunk in _chunks("\n".join(lines)):
        await send(session, chunk)


async def _run(session: Session, coro, *, timeout: float) -> None:
    """统一执行外壳：并发闸门 → 超时 → 错误转文案 → 分段发送。"""
    key = "radar"
    if sum(_active.values()) >= max(1, _config.concurrency):
        await send(session, "雷达当前较忙，请稍后再试。")
        return
    # 准入与占位之间不得 await，否则计数会被并发穿透。
    _active[key] = _active.get(key, 0) + 1
    try:
        lines = await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[radar] command timed out after {:.0f}s", timeout)
        await send(session, "雷达查询超时，请稍后重试或改用更轻的命令。")
        return
    except RadarError as error:
        # 上游响应体原文绝不进用户可见消息（plugin_api §2）。
        await send(session, format_error(error))
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 边界只记类型名，绝不记载荷
        logger.warning("[radar] command failed: {}", type(error).__name__)
        await send(session, "雷达查询失败，请稍后重试。")
        return
    finally:
        _active[key] = max(0, _active.get(key, 1) - 1)
    if lines:
        await _reply(session, lines)


def _tokens(rest: ArgVal | None) -> list[str]:
    """把 rest 拆成位置参数（容忍多个空格）。"""
    raw = get_rest(rest) or ""
    return [token for token in str(raw).split() if token]


def _split_benchmark(tokens: list[str]) -> tuple[list[str], str | None]:
    """把末尾的 ``@频道`` 摘出来作为频道参数（避免与模型名/档位混淆）。"""
    if tokens and tokens[-1].startswith("@") and len(tokens[-1]) > 1:
        return tokens[:-1], tokens[-1][1:]
    return tokens, None


radar_command = Alconna(list(ROOT_ALIASES), Args["rest;?", MultiVar(AnyString)])
_radar = on_alconna(radar_command, priority=5, block=True)


@_radar.handle()
async def handle_radar(event: Event, rest: ArgVal, session: Session) -> None:
    """``/radar`` 总入口：按第一个词分派。"""
    tokens = _tokens(rest)
    if not tokens:
        await _reply(session, format_help())
        return
    sub, *args = tokens
    key = _SUBCOMMANDS.get(sub) or _SUBCOMMANDS.get(sub.casefold())
    if key is None:
        await send(session, format_error(InvalidArgument(f"未知子命令「{sub}」，试试 /radar 帮助。", detail="unknown subcommand")))
        return
    timeout = _config.table_timeout if sub in _TABLE_COMMANDS else max(30.0, _config.timeout * 1.5)
    await _run(session, _dispatch(key, args), timeout=timeout)


async def _dispatch(key: str, args: list[str]) -> list[str]:
    """把子命令翻译成 service 调用 + formatter。"""
    args, benchmark = _split_benchmark(args)

    if key == "rank":
        rows, meta = await _service.top_models(benchmark=benchmark)
        return format_model_list(rows, meta)

    if key == "model":
        name, effort = _model_and_effort(args, "用法：/radar 模型 <名> [档位]")
        profile = await _service.model_profile(name, effort=effort, benchmark=benchmark)
        return format_model_profile(profile)

    if key == "compare":
        if len(args) < 2:
            raise InvalidArgument("用法：/radar 对比 <A> <B> [档位]", detail="missing models")
        effort = args[2] if len(args) > 2 else None
        cmp = await _service.compare(args[0], args[1], effort=effort, benchmark=benchmark)
        return format_comparison(cmp)

    if key == "recommend":
        recs = await _service.recommendations(benchmark=benchmark)
        meta = await _service.recommendations_meta(benchmark=benchmark)
        return format_recommendations(recs, meta)

    if key == "alert":
        alerts, meta = await _service.degradation_alerts(benchmark=benchmark)
        return format_alerts(alerts, meta)

    if key == "value":
        points, meta = await _service.value_picks(benchmark=benchmark)
        return format_value_picks(points, meta)

    if key == "trend":
        name, effort = _model_and_effort(args, "用法：/radar 趋势 <名> [档位]")
        points, meta = await _service.trend(name, effort=effort, benchmark=benchmark)
        label = name + (f"[{effort}]" if effort else "") + f" · {meta.note}"
        lines = format_trend(points, label=label)
        # format_trend 不接 meta，脚注在这里补（口径红线：必须有数据时间与频道）。
        lines.append(format_meta_footer(meta))
        return lines

    if key == "bench":
        items = await _service.benchmarks()
        return format_benchmark_list(items)

    if key == "combos":
        combos = await _service.model_catalog(benchmark=benchmark)
        table = await _service.table(benchmark=benchmark)
        return format_model_catalog(combos, table.meta)

    return format_help()


def _model_and_effort(args: list[str], usage: str) -> tuple[str, str | None]:
    """``<名> [档位]`` 形态的参数解析。"""
    if not args:
        raise InvalidArgument(usage, detail="missing model")
    return args[0], (args[1] if len(args) > 1 else None)


__all__ = ["CHUNK_SIZE", "ROOT_ALIASES", "handle_radar", "radar_command"]
