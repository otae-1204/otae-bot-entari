# Upstream attribution

The search, tool loop and Markdown card renderer come from
[kumoSleeping/Hyw-Frontier](https://github.com/kumoSleeping/Hyw-Frontier)
commit `0a1fede8d7fa701e4ebaaf8a719466ccf89e0416`.
This plugin does not install `entari_plugin_hyw_frontier`.

That commit is vendored into `vendor/hyw-frontier/` and installed editable from
there, because neither `hyw-frontier` nor `md2png` is published to PyPI. The
vendored copy is not a pristine checkout: the patches below are part of it and
must be kept when the upstream code is re-synced.

## Local patches

| File | Patch | Why |
| --- | --- | --- |
| `media_fetch.py` | Accept `198.18.0.0/15` as routable | Clash/mihomo fake-ip uses that range; LAN addresses stay rejected |
| `rendering.py` | `getattr(os, "O_NOFOLLOW", 0)` | Windows has no `os.O_NOFOLLOW` |
| `request_log.py` | `getattr(os, "O_NOFOLLOW", 0)` | Same |
| `tools.py` | Default search provider `parallel` → `ddgs` | `ddgs` needs no key; the bot also passes `ddgs` unless configured |
| `model_backend.py` | Adds `RETRYABLE_STATUSES`, `http_status()`, `transient_failure()`, `_plan_retry()` and the `HYW_RETRY_*` backoff | Upstream has no retry; `tests/test_hyw.py` imports these symbols, and without them a single 429 fails the whole round |
| `prompts/system.md` | Identity line, drops the `<final_response>` wrapper | Matches the bot's help text; the final answer is Markdown, not XML |

## Excluded from the vendored copy

`hyw_frontier/server.py`, `hyw_frontier/static/`, `hyw_frontier/dev_reload.py`
(the debug web UI) and `uv.lock` are not installed. Everything else the upstream
sdist/wheel ships is present.

The previous XML / Playwright plugin was removed on 2026-10-01. It is still
recoverable from commit `d80759d` for rollback.
