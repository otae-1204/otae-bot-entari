# AI 智商雷达（plugins/radar）

只读查询 api.codexradar.com 的模型评测数据（IQ、通过率、档位、降智预警、性价比、趋势），以 HTML 卡片截图回复；渲染失败时回退为分段文字。

## 指令

根指令 `/radar`，别名 `/智商雷达`。群聊、私聊均可，无权限限制。子命令英文别名不区分大小写。

| 指令 | 说明 |
|---|---|
| `/radar`、`/radar 总览`（`overview`） | 模型与档位矩阵 |
| `/radar 榜`（`rank`） | 档位表现排行（按通过率） |
| `/radar 模型 <名> [档位]`（`model`） | 模型档案 |
| `/radar 对比 <A> <B> [档位]`（`vs`） | 横向对比 |
| `/radar 推荐`（`rec`） | 官方推荐模型 |
| `/radar 预警`（`alert`） | 降智预警 |
| `/radar 性价比`（`value`） | 性价比榜 |
| `/radar 趋势 <名> [档位]`（`trend`） | IQ 趋势 |
| `/radar 频道`（`bench`） | 所有评测频道 |
| `/radar 档位`（`combos`） | 模型档位清单（需拉取 `/table`，超时用 `table_timeout`） |
| `/radar 帮助`（`help`） | 帮助卡 |

- 频道参数写在末尾，以 `@` 开头，例如 `/radar 榜 @pompeii-adjacency`；不写时用 `default_benchmark`。
- 档位取值：`low` `medium` `high` `xhigh` `max` `ultra`。
- 同时执行的命令数超过 `concurrency` 时回复「雷达系统正忙」；上游错误只回复归类后的提示，不转发响应体。

## 配置

`entari.yml` 的 `plugins: plugins.radar:` 段（`plugins/radar/config.py` 的 `RadarSettings`），全部可留空，无密钥（公开读端点免鉴权）。

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` | `https://api.codexradar.com` | 上游 API 基址 |
| `default_benchmark` | `deep-swe` | 默认频道：`deep-swe`（代码修复）或 `pompeii-adjacency`（视觉恢复），两者分数不可比 |
| `timeout` | 20.0 | 单次请求超时秒数（下限 1.0）；普通命令总超时取 `max(30, timeout × 1.5)` |
| `table_timeout` | 60.0 | `/radar 档位` 的超时秒数（下限 1.0） |
| `concurrency` | 4 | 同时执行的雷达命令数上限（下限 1） |
| `max_bytes` | 12582912 | 单次响应字节上限（下限 1024） |
| `max_table_bytes` | 25165824 | `/table` 单次响应字节上限（下限 1024） |
| `user_agent` | 空 | 留空使用 `otae-bot-radar/<版本> (+仓库地址)` |
| `proxy` | 空 | 预留，不生效；设置后只记一条警告 |

缓存 TTL 与 429/503 退避参数写死在 `config.py`（`RadarCacheTTL`、`RadarRetry`），不可配置。

## 数据

不落盘，只有进程内缓存。卡片模板与静态资源在 `plugins/radar/assets/`。

## 推送与定时任务

无。只响应指令。

## 详细文档

- [ai_radar_matrix_ui.md](ai_radar_matrix_ui.md)：总览矩阵与卡片样式
- [../reference/ai_radar_interface.md](../reference/ai_radar_interface.md)：上游接口说明
- [../reference/ai_radar_data_dictionary.md](../reference/ai_radar_data_dictionary.md)：数据字典
- [../reference/ai_radar_frontend_api.md](../reference/ai_radar_frontend_api.md)：`RadarService` 能力清单（含已摘掉命令面的题目、贡献者、流水、实时等）

本地预览：`scripts/preview/preview_ai_radar.py`、`scripts/preview/preview_ai_radar_matrix.py`。

## 已知问题

- 文字帮助（`format_help`）把频道写成 `[频道]` 位置参数，实际只识别末尾的 `@频道`；`/radar 榜 deep-swe` 这类写法会忽略频道。
