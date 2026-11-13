# 配置

## 文件与分工

| 文件 | 内容 | 进 Git |
|---|---|---|
| `entari.yml` | 全部设置：连接、前缀、管理员、核心与插件设置。模板 `entari.example.yml`，每项带说明与默认值 | 否 |
| `.env` | 只放密钥（令牌、API Key、Cookie、凭据加密密钥）。模板 `.env.example` | 否 |

- `entari.yml` 用 `"${{ env.名字 :- }}"` 引用 `.env` 里的同名变量；真实环境变量优先于 `.env`。必须带引号；`:-` 让未设置的变量取空串（插件提示「未配置」）。
- `.env` 的值里不能出现 `${`（python-dotenv 会展开）。
- 程序不写配置文件：启动时把 `entari.yml` 复制到 `.runtime/entari.yml` 再加载。改完重启生效。

## 段落

| 段 | 归属 | 读取 |
|---|---|---|
| `basic:` | Entari：`network`（连接）、`prefix`、`superusers`、`log`、`ignore_self_message` | Entari；`superusers` 下所有平台的 ID 合并成一份管理员名单（`otae_bot.config.settings.Config.SUPERUSERS`） |
| `otae:` | 核心设置：`llm`、`http_asset`、`http_cache`、`browser`、`media`、`help_cache_path` | `core_settings()`，模型 `otae_bot/config/core.py` |
| `plugins: plugins.<目录名>:` | 各插件设置 | `plugin_settings("<目录名>", 模型)` |
| `plugins: server:` | 进程内 OneBot 11 接入 | `otae_bot/boundary`，见 [连接](connections.md) |

插件设置是 `otae_bot.config.model.Settings` 的子类，写在插件目录里（如 `plugins/bilibilibot/config.py`），每项有默认值和一句说明。值转换不了、或引用的变量未设置（不带 `:-`）时，用默认值并记一条警告 `[config] <段>.<键> … using the default`，不打印值。

## 进程环境变量（例外）

下面这些由第三方或 vendored 代码直接从进程环境读取，写在 `.env` 里即可；启动时若进程环境里没有，会从 `.env` 补进去（`otae_bot/config/settings.py` 的 `PROCESS_ENV`）：

`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`GOOGLE_APPLICATION_CREDENTIALS`、`GOOGLE_API_KEY`、`GEMINI_API_KEY`、`JINA_API_KEY`、`PARALLEL_API_KEY`、`HYW_FRONTIER_HOME`、`HYW_OPENCODE_SESSION`、`HYW_RETRY_ATTEMPTS`、`HYW_RETRY_BASE_DELAY`、`HYW_RETRY_MAX_DELAY`、`HYW_RETRY_BUDGET`。

Entari 自己的 `ENVIRONMENT`、`ENTARI_CONFIG_FILE`、`ENTARI_CONFIG_EXTENSION` 也留在 `.env`。

## 从旧配置转换（`scripts/convert_config.py`）

旧部署的设置都在 `.env`，另有 `configs/*/config.json`。脚本只用标准库，在部署目录（或 `--root <目录>`）里执行：

```
.venv\Scripts\python.exe -X utf8 scripts\convert_config.py            # 预演：只列键名与去处，不打印值，不写文件
.venv\Scripts\python.exe -X utf8 scripts\convert_config.py --apply    # 执行
.venv\Scripts\python.exe -X utf8 scripts\convert_config.py --rollback # 回滚
```

读取：`.env`；`configs/`（没有时找 `configs.legacy/`）；旧 `entari.yml`（优先 `entari.legacy.yml`，只取 `prefix`、`log`、`ignore_self_message`，需要已装 ruamel.yaml，否则用旧默认值）。

`--apply` 做的事：

- bot 运行中（`.runtime/bot.lock` 被占用）拒绝执行；
- 把 `.env`、`entari.yml` 备份为 `*.bak-<时间>`；
- 写新的 `entari.yml` 和只含密钥的 `.env`；
- `configs/peek/config.json`（运行时数据）复制到 `data/peek/state.json`，目标已存在则不覆盖；
- `configs/` 改名为 `configs.bak-<时间>`（`configs.legacy/` 不动）；
- 再次执行时发现 `.env` 已是新格式，直接退出，不改动。

预演报告要看的几处：

- 未识别的键原样保留在新 `.env` 的「未识别」段；旧版就不读的键（`MCSM_PANEL_URL`、`MCSM_API_KEY`、`MCSM_GROUP_WHITELIST`、`NICKNAME`、`COMMAND_START`）和已归档插件的 `GROKBOT_*` 保留在「不再读取」段。确认无用后可删。
- 没有插件读取的 `configs/` 内容写成 `entari.yml` 末尾的注释。
- 连接：脚本会列出旧 `SATORI_CLIENTS` 条目里本版不再读取的字段（如 `onebot_url`、`onebot_token`），它们不会写进新配置；条目端口是 443 时写 `secure: true` 并附说明。其余情况需要 wss 时（如 Ir 面板）手动在 `basic.network` 条目里加 `secure: true`。

`--rollback`：bot 运行中拒绝；把最新的 `.env.bak-*`、`entari.yml.bak-*` 拷回（当前文件改名为 `*.bak-<时间>-converted`）；`configs/` 不存在时从最新的 `configs.bak-*` 复制回来；`data/peek/state.json` 拷回 `configs/peek/config.json`。之后要切回旧版代码才能用。

### 旧变量与新位置

「`.env`」表示仍是 `.env` 里的密钥，由 `entari.yml` 对应项用占位符引用。

| 旧变量 | 新位置 |
|---|---|
| `SATORI_CLIENTS` | `basic.network`，每条一个 `type: ws`（`host`/`port`/`path`）；令牌 → `.env` `SATORI_TOKEN_n`。旧 `.env` 两个列表都没设时，生成一条 `localhost:5500` |
| `ONEBOT_CLIENTS` | `plugins.server.adapters`（`endpoint`）；令牌 → `.env` `ONEBOT_TOKEN_n`，键名 `access_token` |
| `SUPERUSERS` | `basic.superusers.qq`（未设置时写入旧版默认值） |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `.env` / `otae.llm.base_url` / `otae.llm.model` |
| `OTAE_HTTP_ASSET_*` | `otae.http_asset.*`（小写，去前缀）；`PROXY` 带账号密码时进 `.env` |
| `OTAE_BROWSER_CONCURRENCY` | `otae.browser.concurrency` |
| `OTAE_HTTP_{API,TABLE,I18N,ASSET}_CACHE_MIB`、`OTAE_HTTP_ASSET_LARGE_ENTRY_KIB` | `otae.http_cache.{api,table,i18n,asset}_mib`、`otae.http_cache.asset_large_entry_kib` |
| `OTAE_PUBLIC_{IMAGE,TABLE}_CACHE_{PATH,MIB}` | `otae.http_cache.public_{image,table}_{path,mib}` |
| `OTAE_HELP_CACHE_PATH` | `otae.help_cache_path` |
| `ENDFIELD_CREDENTIAL_KEY` | `.env`，`plugins.endfield.credential_key` |
| `ENDFIELD_GACHA_{LAYOUT,FORWARD_ABOVE,SHOW_STANDARD,RERUN_SIDE}`、`GACHA_SHOW_STANDARD` | `plugins.endfield.gacha_{layout,forward_above,show_standard,rerun_side}`（两个 `SHOW_STANDARD` 都有值时取 `ENDFIELD_` 的） |
| `ARKNIGHTS_CREDENTIAL_KEY` | `.env`，`plugins.arknights.credential_key` |
| `HYW_API_KEY` | `.env`，`plugins.hyw.api_key` |
| `HYW_CONFIG_SOURCE`、`HYW_BASE_URL`、`HYW_MODEL`、`HYW_CREDENTIALS_FILE`、`HYW_VERTEX_*`、`HYW_PROXY`、`HYW_SEARCH_*`、`HYW_READER_ENGINE`、`HYW_RENDER*`、`HYW_MAX_*`、`HYW_TIMEOUT`、`HYW_REQUEST_TIMEOUT`、`HYW_REASONING_MODE`、`HYW_HOME` | `plugins.hyw.*`（小写，去前缀）；`HYW_PROXY`、`HYW_SEARCH_PROXY` 带账号密码时进 `.env`。其他 `HYW_*` 不在此列：`HYW_RETRY_*`、`HYW_FRONTIER_HOME`、`HYW_OPENCODE_SESSION` 见下一行，其余进「未识别」段 |
| `HYW_RETRY_*`、`HYW_FRONTIER_HOME`、`HYW_OPENCODE_SESSION`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`GOOGLE_APPLICATION_CREDENTIALS`、`GOOGLE_API_KEY`、`GEMINI_API_KEY`、`JINA_API_KEY`、`PARALLEL_API_KEY` | 仍在 `.env`（进程环境变量，见上文） |
| `GROKBOT_*` | 不再读取（插件已归档）；保留在 `.env` 的「不再读取」段 |
| `STEAM_API_KEYS` / `STEAM_API_KEY` / `STEAM_LLM_API_KEY` | `.env`，`plugins.steamInfo.api_keys` / `api_key` / `llm_api_key` |
| `STEAM_{REQUEST_INTERVAL,DISABLE_BROADCAST_ON_STARTUP,LLM_BASE_URL,LLM_MODEL}` | `plugins.steamInfo.{request_interval,disable_broadcast_on_startup,llm_base_url,llm_model}` |
| `MCSM_DEPLOY_LARGE_PACKAGE_MB` | `plugins.mcsm.deploy_large_package_mb` |
| `MC_BROADCAST_INTERVAL` | `plugins.minecraft_plugin.broadcast_interval` |
| `BILI_SESSDATA` / `BILI_BUVID3` / `BILI_DM_IMG_LIST` / `BILI_DM_IMG_STR` / `BILI_DM_COVER_IMG_STR` | `.env`，`plugins.bilibilibot.{sessdata,buvid3,dm_img_list,dm_img_str,dm_cover_img_str}` |
| `BILI_RSSHUB_BASE_URLS` / `BILI_LOTTERY_FILTER_UIDS` / `BILI_LIVE_BATCH` | `plugins.bilibilibot.rsshub_base_urls`（逗号分隔转列表）/ `lottery_filter_uids`（原样，空串即关闭过滤）/ `live_batch` |
| `RADAR_*` | `plugins.radar.*`（小写，去前缀）；`RADAR_PROXY` 带账号密码时进 `.env` |
| `TIBO_RADAR_*` | `plugins.tibo_radar.*`（小写，去前缀） |
| `configs/eft_helper/config.json` 的 `use_proxy` | `plugins.eft_helper.use_proxy` |
| `configs/peek/config.json` | `data/peek/state.json`（运行时数据） |

`RADAR_*`、`TIBO_RADAR_*`、`OTAE_HTTP_ASSET_*` 只转换脚本认识的键（`scripts/convert_config.py` 的 `RULES`），不认识的进「未识别」段。值无效（如数字字段写了文字）时省略，新配置用默认值，与旧版行为一致。插件文档里出现的旧变量名按上表对应。
