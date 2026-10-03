# Hyw Frontier

Hyw Frontier 是 Hyw 基于 DeepSeek V4.1 Flash（`deepseek-flash`）开发的下一代 Hyw 核心，提供无需浏览器的 PIL 快速原生 Markdown 渲染, 提供自动多处链路优化、压缩，以及下载超时、图片裁剪、搜索结果筛选等功能。

## 使用

Python 3.11+。在工作区执行 `uv sync` 安装依赖。以下示例使用环境变量 `DEEPSEEK_API_KEY`，搜索选择无需密钥的 DDGS；运行问答会调用实际模型与搜索服务。

```python
import asyncio
import base64
import os
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from queue import SimpleQueue

from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from md2png import FontSet

from hyw_frontier import Answer, answer


# 高阶封装：注入模型与回调，预绑定配置，返回可重复调用的异步问答函数。
def create_answerer(
    model: Model,
    *,
    send: Callable[[str], object | Awaitable[object]],
    on_event: Callable[[dict], None] | None = None,
    search_provider: str = "ddgs",
) -> Callable[..., Awaitable[Answer]]:
    return partial(
        answer,
        model=model,
        send=send,
        on_event=on_event,
        search_provider=search_provider,   # "parallel" / "jina" / "ddgs"
        search_mode="turbo",               # 仅影响 Parallel 搜索
        language="中文",
        home=Path.home() / ".hyw-frontier", # 搜索凭据目录；模型凭据由注入实例管理
        fonts=FontSet.bundled(),            # 可换成 FontSet.load(Path("fonts.json"))
        system_prompt=None,                # 默认内置提示词；可传入自定义提示词正文
        max_rounds=30,
        timeout=90,                        # 单次模型请求/过程回调上限，不是整轮时限
    )


async def main(question: str, image_path: Path | None = None) -> Answer:
    # 过程回调可同步或异步；此处可替换成 bot.send_message 等发送函数。
    # 仅有效的 send_process_intro 工具调用会触发，不逐 token 推送或发送最终回答。
    async def send(text: str) -> None:
        print(text)

    # 诊断回调在工作线程执行：必须同步、线程安全且不阻塞。
    # 此处只收集事件类型，不保存可能含用户内容的完整事件。
    events: SimpleQueue[str] = SimpleQueue()

    def on_event(event: dict) -> None:
        events.put(event.get("type", "unknown"))

    # 可选图片输入：这里接收 PNG；其他格式须填写与实际字节一致的 MIME。
    images = None if image_path is None else [{
        "mimeType": "image/png",
        "data": base64.b64encode(image_path.read_bytes()).decode("ascii"),
    }]

    # 客户端注入：连接、认证和重试由调用者配置，并负责关闭。
    async with AsyncOpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
        max_retries=0,
    ) as client:
        model = OpenAIResponsesModel(
            os.environ.get("HYW_MODEL", "deepseek-flash"),
            provider=DeepSeekProvider(openai_client=client),
            settings={"openai_reasoning_effort": "low", "openai_store": False},
        )
        ask = create_answerer(model, send=send, on_event=on_event)

        # 整轮截止时间由调用方控制；取消后仍会等待连接与渲染资源回收。
        async with asyncio.timeout(300):
            result = await ask(question, images=images, history=None)

        # 续接由调用者持有历史；在同一客户端上下文中按需调用：
        # result = await ask("补充说明其中的关键依据", history=result.messages)
        # partial 的预绑定参数也可按次覆盖：await ask("问题", max_rounds=10)

    # 最终回答由调用者投递：纯文本不绘图，Markdown 返回 PNG。
    if result.kind == "image" and result.png is not None:
        Path("answer.png").write_bytes(result.png)
    else:
        print(result.display_text)

    print("来源链接：", result.links)
    print("耗时（ms）：", result.answer_ms, result.render_ms)
    print("是否截断：", result.truncated, "渲染诊断：", result.diagnostics)
    print("费用（USD）：", result.costs.total_usd, "估算：", result.costs.estimated_total_usd)
    print("费用明细：", result.costs.items)  # 未知金额为 None，不是零
    print("诊断事件数：", events.qsize())
    # result.text 保留模型原文；display_text 是清理后的正文，也可作图片投递备份。
    # result.messages 是完整消息历史，由调用者决定是否保存或用于下一轮。
    return result


if __name__ == "__main__":
    asyncio.run(main("介绍 Hyw Frontier 这类检索问答系统的关键设计"))
    # 附图调用：asyncio.run(main("分析这张图片", Path("input.png")))
```

注入的 `Model` 决定提供商、协议和模型设置，不要同时传入 `provider`、`api`、`base_url`、`api_key` 或 `backend`。不传 `reasoning` 时保留实例设置；已验证的 DeepSeek 模型可显式传入三档映射，仅逐请求覆盖思考强度，不修改共享实例。共享客户端应在同一个事件循环中使用；Hyw 不关闭调用方注入的模型或客户端。示例模型为限时 ID，失效后需通过 `HYW_MODEL` 显式指定可用模型，不自动回退。

思考等级切换工具及相关提示词暂时停用，页面可手动选择固定思考强度，并按模型记住设置。DeepSeek 可选 `off/low/high/max`；Gemini 3.8 Flash 可选 `low/medium/high`，不支持关闭或 `minimal`；本地 Qwen 保留模型自身设置。

API 的 `reasoning={"high": "max", "medium": "low", "low": "off"}` 必须且只能包含高／中／低三个键，值可重复，不接受单个字符串。上述默认映射用于已验证的 DeepSeek；Gemini 默认映射为 `{"high": "high", "medium": "medium", "low": "low"}`。`reasoning_mode="auto"` 暂时全程保持 `medium`（DeepSeek 实际强度 `low`，Gemini 为 `medium`），也可用 `"high"`、`"medium"`、`"low"` 固定对应档位。页面选择具体强度时，三档都映射到该强度，不在提问过程中自动切换。

也可以不注入实例，直接向 `answer()` 传入模型 ID 和连接参数；更底层的模型传输可通过 `backend` 注入。完整参数、返回字段及生命周期约定见 [库接口文档](docs/library.md)。

## 本地 Gemini（调试页面可选）

安装 Google 可选依赖：`uv sync --extra google`；之后使用 `.venv/bin/python -m hyw_frontier.cli serve` 启动，或用 `uv run --extra google hyw-frontier serve` 保留该依赖。

Gemini Developer API 可用 `login google api_key` 保存密钥。Google Cloud / Vertex AI 则把服务账号 JSON 对象合并到 `~/.hyw-frontier/auth.json` 的 `google` 字段（保留 `"type": "service_account"` 和其他原始字段，不覆盖其他提供商）。该文件应位于仓库外，权限为 `0600`，父目录为 `0700`；不要上传到前端或提交 Git。显式 API Key 或环境变量中的 Google API Key 优先于已保存服务账号；服务账号使用自身 `project_id`，需具备 Vertex AI 调用权限，项目需启用 API 和结算。

在同目录 `models.json` 中合并：

```json
{
  "google": {
    "model": "gemini-3.8-flash",
    "label": "Gemini 3.8 Flash",
    "location": "global"
  }
}
```

截至 2026-09-15，[最新通用 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash) 为 `gemini-3.8-flash`，最大输出 65,536 tokens；不自动回退其他型号。`location` 仅用于服务账号接入，默认 `global`。

调试页面只有一个模型下拉框：仅展示凭据已配置且具有模型预设的选项（默认 DeepSeek 使用内置模型 ID），不提供任意提供商或模型 ID 输入。浏览器记住选择；修改后端配置后刷新页面即可更新选项。这里的“已配置”不代表已联网检查权限或本地模型服务正在运行。CLI 和库仍可显式指定模型。

## 本地 MLX 模型（调试页面可选）

Apple Silicon 上可用独立的 `mlx-vlm` 服务提供 OpenAI Chat Completions API，不需要把 MLX 安装进本项目环境。在模型目录运行：

```bash
.venv/bin/python -m mlx_vlm.server --host 127.0.0.1 --port 8768 \
  --model ./4bit --max-tokens 1024 --prefill-step-size 256 --vision-cache-size 2
```

在 `~/.hyw-frontier/models.json`（或 `HYW_FRONTIER_HOME` 下同名文件）中合并以下提供商配置，不覆盖其他条目：

```json
{
  "openai-compatible": {
    "api": "chat",
    "base_url": "http://127.0.0.1:8768/v1",
    "model": "./4bit",
    "label": "本地 Qwen · MLX 4-bit（支持图片）",
    "max_output_tokens": 1024
  }
}
```

本机未开启 API 认证时，设置 `OPENAI_COMPATIBLE_API_KEY=local-mlx`，或用 `login openai-compatible api_key` 保存该占位字符串（非真实密钥）。重启调试服务并刷新页面，在模型菜单选择本地选项即可，无需填写模型 ID；首次打开时 DeepSeek 仍是默认选项。`model`、`label` 仅为页面预设，命令行及库调用仍显式指定模型 ID。

`max_output_tokens` 是**显式的部署输出预算**，优先于模型元数据；1024 并非模型理论最大输出能力。未配置时仍自动解析模型输出容量。16GB Mac 建议先用短对话和单张小图；本地模型 API 不联网，但 Frontier 的搜索与网页读取工具仍会联网。MLX 的 `--max-tokens` 是未指定请求额度时的默认值，并非全局硬上限。不要将无认证服务绑定到公网地址。

## 项目定义

Hyw Frontier 是 Python 原生的检索问答核心：负责提示词、模型工具循环、网页搜索与读取、图片审阅，以及最终回答的文本或 PNG 输出。

模型接入仅使用 **Pydantic AI Slim 的模型层**，不运行其 Agent 框架；绘图由独立的 **md2png / Pillow** 离线完成，不依赖 Node 或浏览器截图。核心库不持久保存会话，聊天平台由 [Entari 插件](entari_plugin_hyw_frontier/README.md) 接入。本地调试页面使用 `uv run hyw-frontier serve` 启动，地址为 `http://127.0.0.1:8767`。

## 发布约定

本项目中，“推送”默认同时提交并推送到 GitHub 仓库 `kumoSleeping/Hyw-Frontier`，以及部署到 `ssh-ykhm.kumo.ltd` 的生产 Entari 服务，无需再次确认目标。部署须先完成构建与实际功能核验；上线验证失败时回滚，不将失败版本视为发布完成。

## 修改搜索服务

**切换已有服务不需要改源码。** 修改示例中的 `search_provider`，或在本地网页、Entari 插件配置中选择同名选项：

| `search_provider` | 网页搜索 | 图片搜索 | 所需凭据 |
| --- | --- | --- | --- |
| `parallel` | Parallel，核心库默认 | Jina SVIP | `PARALLEL_API_KEY`；图片搜索另需 `JINA_API_KEY` |
| `jina` | Jina SVIP | Jina SVIP | `JINA_API_KEY` |
| `ddgs` | DDGS | DDGS | 无需搜索 API Key |

`search_mode` 仅对 Parallel 生效，可选 `turbo`、`fast`、`basic`、`advanced`。无论选择哪个搜索服务，网页读取工具 `jina_read_url` 均使用匿名 Jina Reader，默认发送 `X-Engine: browser` 和 `X-Respond-With: markdown`，由 Jina 的浏览器执行网页 JavaScript 后提取正文。使用 `POST https://r.jina.ai/` 和 JSON 请求体 `{"url": "目标网址"}`，以 JSON 的 `data.content` 接收完整页面 Markdown 正文；不要求 API Key。本地调试服务的 `/api/config` 中 `reader` 字段公开当前引擎和认证方式。

密钥放在环境变量，或 `home` 目录中的 `parallel.json` / `jina.json`（字段为 `api_key`），不要写入源码或 README。搜索凭据与模型凭据相互独立。

网页直接返回 Markdown。`reader_engine="default"` 使用 Jina 默认引擎，`"browser"`（默认）强制浏览器；`max_reader_images=30` 控制单页面图片尝试上限，可设为非负整数，0禁用新增网页图片。本地测试页可调整上限、切换引擎并查看逐页耗时；整次提问仍默认最多尝试600张工具图片，同时最多下载20张。

**接入新的搜索服务**：参考 `hyw_frontier/parallel.py`、`jina.py` 或 `ddgs.py` 实现客户端，在 `hyw_frontier/tools.py` 的 `SEARCH_PROVIDERS` 和 `ToolRuntime` 中注册、选择和释放资源；同步调整 `hyw_frontier/tools.json` 的工具参数及服务适用范围。若需在本地网页正确显示名称、模式和凭据提示，同步更新 `hyw_frontier/static/settings.js`。公共 `answer()` 当前没有任意搜索客户端注入参数，`model` / `backend` 注入只替换模型层，不替换搜索服务。

提示词统一目录为 `hyw_frontier/prompts/`，包含主模型、图片说明及临近轮次上限提醒。文件用途、注入时机和仍保留在代码中的协议内容见 [提示词审计](docs/prompt-audit.md)。

请求日志的阶段字段、图片脱敏、计时口径与本地/生产查询方式见 [日志说明](docs/logging.md)。
