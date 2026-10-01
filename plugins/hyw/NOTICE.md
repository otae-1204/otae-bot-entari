# Upstream attribution

The search, tool loop and Markdown card renderer come from
[kumoSleeping/Hyw-Frontier](https://github.com/kumoSleeping/Hyw-Frontier)
commit `0a1fede8d7fa701e4ebaaf8a719466ccf89e0416`.
This plugin does not install `entari_plugin_hyw_frontier`.

Local patches on that checkout, kept because this machine needs them:

- Windows has no `os.O_NOFOLLOW`; log and image files omit that flag.
- Image downloads allow the `198.18.0.0/15` fake-ip range used by the local DNS rewriter.
  LAN addresses stay rejected.
- The preview default search provider is `ddgs`. The bot also passes `ddgs` unless configured.

The previous XML / Playwright plugin was removed on 2026-10-01. It is still
recoverable from commit `d80759d` for rollback.
