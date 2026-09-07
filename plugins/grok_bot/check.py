"""Run with `python -m plugins.grok_bot.check` from the bot project root."""

import asyncio

from .config import GrokConfig, GrokError
from .gateway import check


def main() -> int:
    try:
        message = asyncio.run(check(GrokConfig.from_env()))
    except GrokError as error:
        print(str(error))
        return 1
    except Exception as error:  # noqa: BLE001 - A CLI diagnostic must not print tokens.
        print(f"Grok Bot 检查失败（{type(error).__name__}）。")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
