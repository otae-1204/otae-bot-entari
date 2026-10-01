"""Small local CLI. Login interactions are human UI, never model-callable tools."""
import argparse
import json
from pathlib import Path
import sys

from .runtime import Bridge, DEFAULT_LANGUAGE, DEFAULT_MODEL, DEFAULT_PROMPT, DEFAULT_PROVIDER, FrontierError, PROVIDERS, build_context, load_prompt
from .jina import JinaClient
from .media import MAX_IMAGES, MAX_READER_IMAGES
from .tools import ToolRuntime, tool_definitions


def main() -> int:
    parser = argparse.ArgumentParser(prog="hyw-frontier")
    parser.add_argument("--home", type=Path, help="独立凭据目录（默认 ~/.hyw-frontier）")
    parser.add_argument("--timeout", type=float, default=300, help="每次操作超时秒数")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("providers", help="列出支持的提供商和登录方式")
    server = commands.add_parser("serve", help="启动仅限本机的交互测试页面")
    server.add_argument("--port", type=int, default=8767)
    server.add_argument("--no-reload", action="store_true", help="关闭源码文件监听与自动重启")
    logs = commands.add_parser("logs", help="查询后端自动保存的请求日志和逐阶段耗时；不调用模型")
    logs.add_argument("--query", default="", help="按用户问题关键词筛选")
    logs.add_argument("--limit", type=int, default=5, help="最近多少次请求，默认5，最大100")
    logs.add_argument('--compact', action='store_true', help='仅输出阶段、轮次和调用耗时，省略搜索与图片详情')
    commands.add_parser("status", help="只显示本项目已保存模型凭据的类型，不显示密钥")
    commands.add_parser("tools", help="查看项目自定义工具定义；不包含 Browser 或 Pi 工具")
    tool = commands.add_parser("call-tool", help="直接测试一个项目工具，不调用模型")
    tool.add_argument("name", choices=[item["name"] for item in tool_definitions()])
    tool.add_argument("arguments", help="JSON 参数对象；不要在其中填写密钥")
    login = commands.add_parser("login", help="保存项目独立 API Key；不使用 OAuth 或 Pi 凭据")
    login.add_argument("provider", choices=PROVIDERS)
    login.add_argument("--method", choices=("api_key",), default="api_key")
    logout = commands.add_parser("logout", help="删除本项目保存的提供商凭据")
    logout.add_argument("provider", choices=PROVIDERS)
    models = commands.add_parser("models", help="通过提供商 API 查询当前账号可用模型（需要 API Key）")
    models.add_argument("provider", choices=PROVIDERS)
    ask = commands.add_parser("ask", help="执行检索与工具编排，输出最终回答；不保存历史")
    ask.add_argument("text")
    ask.add_argument("--provider", choices=PROVIDERS, default=DEFAULT_PROVIDER)
    ask.add_argument("--model", default=DEFAULT_MODEL)
    ask.add_argument("--api", choices=("responses", "chat"), help="OpenAI 兼容接口协议；DeepSeek 固定 responses")
    ask.add_argument("--base-url", help="显式模型端点；也可通过独立目录 models.json 配置")
    ask.add_argument("--system-prompt", type=Path, default=DEFAULT_PROMPT)
    ask.add_argument("--language", default=DEFAULT_LANGUAGE, help="最终回复和过程回复的语言（默认：中文）")
    ask.add_argument("--max-rounds", type=int, default=30, help="模型轮次安全阈值，正整数，默认30")
    ask.add_argument("--max-tool-images", type=int, default=MAX_IMAGES,
                     help="工具图片总预算，非负整数，默认600；0禁用新增工具图片")
    ask.add_argument('--max-reader-images', type=int, default=MAX_READER_IMAGES,
                     help='单页面图片尝试上限，非负整数，默认30；0禁用新增网页图片')
    ask.add_argument('--reader-engine', choices=('default', 'browser'), default='browser')
    preview = commands.add_parser("preview", help="离线检查应用上下文，不读取凭据、不调用模型")
    preview.add_argument("text", nargs="?", default="你好")
    preview.add_argument("--system-prompt", type=Path, default=DEFAULT_PROMPT)
    preview.add_argument("--language", default=DEFAULT_LANGUAGE, help="注入提示词的回复语言（默认：中文）")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            if not Path(__file__).with_name("server.py").is_file():
                raise FrontierError("测试前端不随库 wheel 分发；请在项目源码目录运行 serve。")
            if not 0 <= args.port <= 65535:
                raise FrontierError("端口须在0–65535之间")
            if not args.no_reload:
                from .dev_reload import serve_with_reload
                return serve_with_reload(args.home, args.port, args.timeout)
            from .server import serve
            serve(args.home, args.port, args.timeout)
            return 0
        if args.command == "preview":
            result = build_context(args.text, load_prompt(args.system_prompt, language=args.language))
        elif args.command == "logs":
            from .request_log import log_summaries
            result = log_summaries(Bridge(args.home).home, limit=args.limit, query=args.query)
            if args.compact:
                for row in result:
                    row.pop('searches', None)
                    row.pop('pages', None)
                    if 'bot' in row:
                        for key in ('search_queries', 'image_downloads', 'selected_image_urls'):
                            row['bot'].pop(key, None)
        elif args.command == "tools":
            result = tool_definitions()
        elif args.command == "call-tool":
            bridge = Bridge(args.home, args.timeout)
            result = ToolRuntime(JinaClient(bridge.home)).execute({
                "id": "manual", "name": args.name, "arguments": json.loads(args.arguments),
            })
            print(result["content"][0]["text"])
            return 1 if result["isError"] else 0
        else:
            bridge = Bridge(args.home, args.timeout, api=getattr(args, "api", None), base_url=getattr(args, "base_url", None))
            if args.command == "login":
                if not sys.stdin.isatty():
                    raise FrontierError("请在交互终端运行 login；密钥不要通过命令行参数传入。")
                method = args.method
                bridge.call({"provider": args.provider, "method": method}, interactive=True)
                return 0
            if args.command == "ask":
                print(bridge.ask(args.provider, args.model, args.text, args.system_prompt,
                                 max_rounds=args.max_rounds, max_tool_images=args.max_tool_images, language=args.language,
                                 max_reader_images=args.max_reader_images, reader_engine=args.reader_engine))
                return 0
            request = {"command": args.command}
            if hasattr(args, "provider"):
                request["provider"] = args.provider
            result = bridge.call(request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (FrontierError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
