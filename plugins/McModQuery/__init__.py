"""Minecraft Mod/资料搜索 — mcmod.cn 查询."""

import httpx
from lxml import etree as _etree
from urllib.parse import quote_plus

from arclet.alconna import Args
from utils.entari_native import ArgVal, ChainMsg, Text
from arclet.entari import Event

from utils.user_agent import get_user_agent
from utils.entari_native import cmd_with_args as _cmd

mod = _cmd("mod", args=Args["rest", str], aliases={"模组"})
item = _cmd("资料", args=Args["rest", str])


def _parse_search_results(html_text: str, count: int = 3):
    url = _etree.HTML(html_text)
    hrefs = url.xpath('//div[@class="head"]/a[@href]/@href')
    titles = url.xpath('//div[@class="head"]')
    for i in range(len(titles)):
        titles[i] = titles[i].xpath('.//text()')

    class_hrefs = [h for h in hrefs if h.split("/")[3] == "class"]
    return class_hrefs[:count], titles[:count]


def _build_result_msg(name: str, hrefs: list, titles: list, prefix: str) -> str:
    if not hrefs:
        return "找不到您搜索的内容，请尝试更换关键词"
    for i in range(len(titles)):
        titles[i][0] = "".join(titles[i])
    msg = prefix
    for i in range(len(hrefs)):
        msg += f"{titles[i][0]}:\n{hrefs[i]}\n"
    return msg


async def _fetch_search_html(keyword: str, *, filter_args: str = "") -> str:
    url = _search_url(keyword, filter_args=filter_args)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers=get_user_agent())
        resp.raise_for_status()
        return resp.text


def _search_url(keyword: str, *, filter_args: str = "") -> str:
    query = quote_plus(keyword)
    return f"https://search.mcmod.cn/s?key={query}{filter_args}"


@mod.handle()
async def handle_mod(rest: ArgVal[str]):
    text = rest.result.strip() if rest.available else ""
    if not text:
        await mod.finish("用法: /mod ＜模组名＞ [数量]")
        return
    content = text.split(" ")
    name = "".join(content[:-1]) if content[-1].isdigit() else "".join(content)
    try:
        count = int(content[-1])
    except (ValueError, IndexError):
        count = 3

    try:
        html_text = await _fetch_search_html(name)
    except httpx.TimeoutException:
        await mod.finish("Mcmod 搜索请求超时，请稍后再试")
    except httpx.HTTPError:
        await mod.finish("Mcmod 搜索请求失败，请稍后再试")

    hrefs, titles = _parse_search_results(html_text, count)
    msg = _build_result_msg(name, hrefs, titles, f"Mcmod中符合您搜索的mod如下(仅显示前{len(hrefs)}个)\n")
    await ChainMsg.text(msg).finish()


@item.handle()
async def handle_item(rest: ArgVal[str]):
    text = rest.result.strip() if rest.available else ""
    if not text:
        await item.finish("用法: /资料 ＜资料名＞")
        return
    name = "".join(text.split(" "))

    try:
        html_text = await _fetch_search_html(name, filter_args="&filter=3&mold=0")
    except httpx.TimeoutException:
        await item.finish("Mcmod 搜索请求超时，请稍后再试")
    except httpx.HTTPError:
        await item.finish("Mcmod 搜索请求失败，请稍后再试")

    hrefs, titles = _parse_search_results(html_text, 5)
    msg = _build_result_msg(name, hrefs, titles, f"Mcmod中符合您搜索的资料如下(仅显示前{len(hrefs)}个)\n")
    await ChainMsg.text(msg).finish()
