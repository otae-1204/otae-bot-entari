# 归档

## grok_bot（2026-11-12）

- 文件：`grok_bot-2026-11-12.tar.gz`
- SHA-256：`6152da2c47f3dd308f276d46ca234b4f8efe4fbfc0e196731d0990851af66cc2`（81366 字节）
- 快照来源提交：`a236def4765fb00b174e3cacd1e4aad7e0c8aa1c`
- 原因：暂不使用，后续不一定维护。归档后不加载、不测试、不配置、不随架构调整更新。

### 包内文件（相对仓库根目录）

```
docs/grok_bot_attachment_protocol.md
docs/grok_bot_plugin.md
plugins/grok_bot/__init__.py
plugins/grok_bot/check.py
plugins/grok_bot/config.py
plugins/grok_bot/conversations.py
plugins/grok_bot/gateway.py
plugins/grok_bot/handlers.py
plugins/grok_bot/media.py
plugins/grok_bot/persona.md
plugins/grok_bot/relay.py
plugins/grok_bot/stream.py
scripts/ops/repair_grok_binding.py
tests/test_grok_bot.py
tests/test_grok_conversations.py
tests/test_grok_download.py
tests/test_grok_media.py
tests/test_grok_stream.py
```

### 需要的配置项（只列名字）

`entari.yml` 的 `plugins.grok_bot:` 段：`gateway_url`、`gateway_token`、`agent_id`、`timeout`、`max_pending`、`max_concurrent`、`persona_file`。
`.env` 中的密钥：`GROKBOT_GATEWAY_TOKEN`（由 `gateway_token` 引用）。

除此之外，插件当时还依赖核心里的特殊处理：`otae_bot/groups/features.py` 中的 `PLUGIN_NAMES` 条目（别名 `grok`、`grokbot`），以及「默认关闭、仅 SuperUser 可开启」（`DEFAULT_OFF_PLUGINS`、`SUPERUSER_ENABLE_PLUGINS`）；`scripts/migrate_platform.py` 的 `--include-grok` 会话迁移。

### 恢复方法

1. 在仓库根目录解压：`tar -xzf archive/grok_bot-2026-11-12.tar.gz`。
2. 在 `entari.yml` 补回 `plugins.grok_bot:` 段，在 `.env` 补回 `GROKBOT_GATEWAY_TOKEN`，并同步 `entari.example.yml` / `.env.example`。
3. 补回上面列出的核心特殊处理（功能开关名称与默认关闭）、`/help grok` 文本与 `scripts/help_pages.json` 条目，重新渲染帮助图。

**注意：** 包内代码只对应上述快照提交时的架构，归档后不再保持兼容。恢复后必须按当时的架构规范重新适配（配置模型、指令写法、守卫与测试），不能直接启用。
