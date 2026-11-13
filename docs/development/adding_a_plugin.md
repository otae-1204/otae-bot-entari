# 新增插件

以一个假想的天气插件 `plugins/weather/` 为例。规则本身见 [架构规范](architecture_conventions.md) §6、§7；
写法照抄现有插件（`status`、`usage_stats`、`McWikiQuery`、`mcsm`、`tibo_radar`）。

## 目录

```text
plugins/weather/
  __init__.py     只有一行 import，不写别的
  handlers.py     指令：解析参数 → 调 service → 回复；要薄
  service.py      业务逻辑：不 import Entari、不碰消息元素，可单独测试
  config.py       Settings 子类与读取函数
  store.py        data/weather/ 下的读写
  draw.py         出图，返回 PNG 字节
```

目录名 `weather` 就是数据键（群功能开关、指令统计、`/功能` 参数），定了就不改。
插件不声明 metadata；`otae_bot/plugin_registry.py` 按目录自动发现，含 `__init__.py` 即加载。

`__init__.py`（`tests/test_architecture.py` 要求只能是文档字符串加 `from .handlers import *`）：

```python
"""Entari plugin entrypoint; command registration lives in handlers."""

from .handlers import *  # noqa: F401,F403
```

## 配置

`config.py`：

```python
from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class WeatherSettings(Settings):
    """天气查询（/天气）"""

    api_key: str = field(default="", description="天气接口 API Key（密钥）")
    push_minutes: int = field(default=60, description="订阅推送间隔分钟数")


def weather_settings() -> WeatherSettings:
    return plugin_settings("weather", WeatherSettings)
```

`entari.example.yml` 的 `plugins:` 下按目录名加一段，每项写默认值和注释；密钥用 `${{ env.名字 :- }}` 引用：

```yaml
  # 天气查询（/天气）
  plugins.weather:
    api_key: "${{ env.WEATHER_API_KEY :- }}"   # 天气接口 API Key（密钥）
    push_minutes: 60   # 订阅推送间隔分钟数
```

再在 `.env.example` 加 `WEATHER_API_KEY=`。核对它们的测试是 `tests/test_config.py::ExampleFileTests`：
`test_every_setting_is_documented_with_its_default` 要求每个字段都出现在 `entari.example.yml` 且取值等于默认值
（新模型要加进该类 `models()` 里的插件表），`test_every_referenced_secret_is_in_env_example` 要求引用的每个密钥都在 `.env.example`。

缺配置也要能加载：import 时不因缺密钥失败，被调用时回复「未配置」。

## 指令

`handlers.py`：

```python
from __future__ import annotations

import asyncio

from arclet.alconna import Alconna, Args
from arclet.entari import MessageChain, Session, command
from arclet.entari.command import Match
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.messaging import media

from .config import weather_settings
from .draw import draw_weather
from .service import query_weather

weather_cmd = Alconna(["天气", "weather"], Args["city;?", str])


@command.on(weather_cmd)
async def handle_weather(session: Session, city: Match[str]) -> None:
    name = city.result.strip() if city.available else ""
    if not name:
        await session.send("格式：/天气 <城市>")
        return
    if not weather_settings().api_key:
        await session.send("天气查询未配置。")
        return
    report = await query_weather(name)
    try:
        await session.send(MessageChain([media.png(await draw_weather(report))]))
        return
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception as error:  # noqa: BLE001 - 出图失败时保住文字
        logger.warning("[weather] card fell back to text: {}", type(error).__name__)
    await session.send(report.text)
```

- 回复一律用 `session.send`。群功能开关和指令统计由启动时安装的全局钩子自动处理，插件不判断「本群是否开启」。
- 凡 `except Exception` 之前先 `except ExitState: raise`（`asyncio.CancelledError` 同理），否则 `finish()` 会被吞掉。
- 多段免费文本参数用 `Args["rest;?", MultiVar(AnyString)]` 加 `otae_bot.messaging.inbound.get_rest(rest)`（见 `usage_stats`）。

## 出网、图片、数据

- 外部请求用共享客户端 `otae_bot.infrastructure.http.client.fetch_json(url, *, namespace=..., params=...)` / `fetch_bytes`，不自建 `httpx` 客户端。
- 图片只用字节：`media.png(data)`、`media.image(data)`、`media.file(data, name)`；静态资源用 `media.image_file(path)`。
  上传或内联由 `otae_bot/pipeline/outbound_media.py` 处理，插件不写临时文件、不调 `upload.create`（守卫 R9）。
- Pillow 出图放进共享线程池：`await run_image_render(renderer, *args)`（`otae_bot.infrastructure.rendering.executor`）；
  HTML 卡片用共享浏览器（`otae_bot.infrastructure.rendering`）。
- 运行时数据放 `data/weather/`，用 `otae_bot.infrastructure.storage` 读写，例如
  `JsonStore("data/weather/subscriptions.json")`（见 `plugins/mcsm/store.py`）。不往插件目录写数据。
- ID 一律当不透明字符串，不 `int()`；需要群取 guild，需要发送目标取 channel。

## 主动推送

定时任务用 `otae_bot.scheduling.jobs.timer`，消息走 `otae_bot.delivery.deliver`，不自己选账号：

```python
from otae_bot.delivery import Target, deliver
from otae_bot.scheduling.jobs import timer


async def _push() -> None:
    for group_id, text in await due_reports():
        result = await deliver(Target.group(group_id), text)
        if not result.sent:
            logger.info("[weather] push to {} not sent: {}", group_id, result.status)


timer.add_job(_push, "interval", minutes=weather_settings().push_minutes,
              id="weather_push", replace_existing=True, max_instances=1)
```

签名：`async def deliver(target: Target, message, *, account: Account | None = None, plugin: str | None = None) -> DeliveryResult`。
它不抛发送异常，`status` 是 `SENT` / `SUPPRESSED`（本群关了该插件）/ `NO_ACCOUNT` / `FAILED`；
需要异常时调 `result.raise_for_failure()`。私聊用 `Target.private(user_id)`。

## 需要同步登记的地方

| 位置 | 做什么 |
|---|---|
| `otae_bot/groups/features.py` 的 `PLUGIN_NAMES` | 加 `"weather": ("天气", "weather")`：第一项是 `/功能 列表` 里的中文名，其余是 `/功能 开启/关闭` 可用的别名 |
| `entari.example.yml`、`.env.example` | 见上文配置 |
| `tests/test_config.py` 的 `ExampleFileTests.models()` | 有 `Settings` 子类时加入 |
| `scripts/help_pages.json`、`plugins/help_plugin/handlers.py` 的 `TOPIC_MAP` | 需要 `/help weather` 帮助图时加页面和主题，再出图，见 [帮助卡片](help_cards.md) |
| `plugins/changelog/changelog.json` | 合并前收录本次提交（`python scripts/generate_changelog.py --check`） |

`otae_bot/plugin_registry.py` 不需要改。

## 测试

新测试用 `tests/support/dispatch.py` 走真实事件路径，不用 `SimpleNamespace` 冒充事件或会话（规范 §9.1）。
`service.py` 不依赖 Entari，直接单元测试。写法见 [测试](testing.md)，样例 `tests/test_forkout.py`。

改到收发消息路径的，还要按 [实测](../operations/live_testing.md) 在测试群里实测。
