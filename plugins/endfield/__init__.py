from __future__ import annotations

import tempfile

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Event
from loguru import logger
from nepattern import AnyString

from utils.entari_native import ArgVal, ChainMsg, make_image, on_alconna
from utils.temp_files import schedule_temp_file_cleanup

from .client import WarfarinAPIError, WarfarinClient
from .draw import draw_operator_card, draw_weapon_card
from .service import EndfieldService


client = WarfarinClient()
service = EndfieldService(client)

endfield_cmd = on_alconna(
    Alconna(["终末地", "endfield", "ef"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)


@endfield_cmd.handle()
async def handle_endfield(event: Event, rest: ArgVal):
    kind, query = _parse_query(_rest(rest))
    if not query:
        await endfield_cmd.finish("用法：/终末地 干员 <名称> 或 /终末地 武器 <名称>")
    try:
        if kind == "weapon":
            view = await service.get_weapon_view(query)
            if view is None:
                await endfield_cmd.finish("未找到武器")
            png = await draw_weapon_card(view)
        else:
            view = await service.get_operator_view(query)
            if view is None:
                await endfield_cmd.finish("未找到干员")
            png = await draw_operator_card(view)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
            file.write(png)
            file.flush()
            schedule_temp_file_cleanup(file.name)
            await endfield_cmd.finish(ChainMsg([make_image(path=file.name)]))
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {kind} {query}: {exc}")
        await endfield_cmd.finish("数据源暂时不可用")
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {kind} {query}: {exc}")
        await endfield_cmd.finish("图片生成失败")


def _rest(match: ArgVal) -> str:
    if not match.available:
        return ""
    value = match.result
    if isinstance(value, tuple):
        return " ".join(str(item) for item in value).strip()
    return str(value or "").strip()


def _parse_operator_query(rest: str) -> str:
    return _parse_query(rest)[1]


def _parse_query(rest: str) -> tuple[str, str]:
    parts = [part for part in rest.split() if part]
    if not parts:
        return "operator", ""
    if parts[0].lower() in {"干员", "operator", "op"}:
        return "operator", " ".join(parts[1:]).strip()
    if parts[0].lower() in {"武器", "weapon", "wp"}:
        return "weapon", " ".join(parts[1:]).strip()
    return "operator", " ".join(parts).strip()
