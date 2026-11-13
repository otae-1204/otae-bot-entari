# 文档索引

状态：**现行**＝与当前代码一致，改代码时同步改；**计划**＝尚未实现；**历史**＝冻结的方案与记录，
内容、路径与写法以当时为准，顶部一行指向现状；**参考**＝外部接口、调研与转载资料，不是本项目规则。

入口：[README](../README.md) · [CLAUDE.md](../CLAUDE.md) · 用户说明书是 `/help` 帮助图片（源文件 `scripts/help_pages.json`）。

## 运维（operations/）

| 文档 | 内容 | 状态 |
|---|---|---|
| [operations/configuration.md](operations/configuration.md) | 配置 | 现行 |
| [operations/connections.md](operations/connections.md) | 连接 | 现行 |
| [operations/cutover.md](operations/cutover.md) | 从旧版切换到 v2 | 现行 |
| [operations/deploy.md](operations/deploy.md) | 部署脚本（scripts/deploy.ps1） | 现行 |
| [operations/endfield_gacha_batch_scripts.md](operations/endfield_gacha_batch_scripts.md) | 终末地寻访批量脚本使用说明 | 现行 |
| [operations/endfield_ownership_stats_operations.md](operations/endfield_ownership_stats_operations.md) | 终末地持有率刷新运维日志 | 现行 |
| [operations/live_testing.md](operations/live_testing.md) | 实测（双向限制的测试 bot） | 现行 |
| [operations/rsshub_selfhost.md](operations/rsshub_selfhost.md) | RSSHub 自建部署手册 | 现行 |
| [operations/setup.md](operations/setup.md) | 首次安装 | 现行 |
| [operations/troubleshooting.md](operations/troubleshooting.md) | 故障排查 | 现行 |

## 开发（development/）

| 文档 | 内容 | 状态 |
|---|---|---|
| [development/adding_a_plugin.md](development/adding_a_plugin.md) | 新增插件 | 现行 |
| [development/architecture_conventions.md](development/architecture_conventions.md) | otae-bot 架构规范 | 现行 |
| [development/code_structure.md](development/code_structure.md) | 代码结构 | 现行 |
| [development/help_cards.md](development/help_cards.md) | 帮助图片（/help）生成框架 | 现行 |
| [development/http_asset_lane.md](development/http_asset_lane.md) | 素材通道：独立并发、短超时、按主机熔断与整卡截止时间 | 现行 |
| [development/testing.md](development/testing.md) | 测试 | 现行 |
| [adr/0001-model-stage-difficulties-as-variants.md](adr/0001-model-stage-difficulties-as-variants.md) | 将难度和深度建模为关卡变体（ADR） | 现行 |
| [adr/0002-use-typed-stage-family-extensions.md](adr/0002-use-typed-stage-family-extensions.md) | 使用类型化的玩法扩展承载关卡差异（ADR） | 现行 |
| [../CONTEXT.md](../CONTEXT.md) | 终末地插件领域术语表 | 现行 |

## 功能（features/）

每个插件一页总览，大插件另有分功能的文档。

| 文档 | 内容 | 状态 |
|---|---|---|
| [features/McModQuery.md](features/McModQuery.md) | MC 百科搜索（plugins/McModQuery） | 现行 |
| [features/McWikiQuery.md](features/McWikiQuery.md) | Minecraft Wiki 查询（plugins/McWikiQuery） | 现行 |
| [features/ai_radar_matrix_ui.md](features/ai_radar_matrix_ui.md) | AI 智商雷达 · 模型档位矩阵 | 现行 |
| [features/arknights_signin.md](features/arknights_signin.md) | 明日方舟森空岛签到 | 现行 |
| [features/bilibili_cards.md](features/bilibili_cards.md) | B 站视频与直播卡片 | 现行 |
| [features/bilibili_endfield_lottery_filter.md](features/bilibili_endfield_lottery_filter.md) | 动态抽奖的过滤规则 | 现行 |
| [features/bilibilibot.md](features/bilibilibot.md) | B 站订阅（plugins/bilibilibot） | 现行 |
| [features/changelog_plugin.md](features/changelog_plugin.md) | 更新日志插件（plugins/changelog） | 现行 |
| [features/eft_helper.md](features/eft_helper.md) | 逃离塔科夫弹药查询（plugins/eft_helper） | 现行 |
| [features/endfield.md](features/endfield.md) | 明日方舟：终末地（plugins/endfield） | 现行 |
| [features/endfield_announcements.md](features/endfield_announcements.md) | 终末地活动公告提醒 | 现行 |
| [features/endfield_archive_guide.md](features/endfield_archive_guide.md) | 终末地「档案库收集检查」功能指南 | 现行 |
| [features/endfield_attendance_card.md](features/endfield_attendance_card.md) | 终末地签到卡片 | 现行 |
| [features/endfield_binding.md](features/endfield_binding.md) | 终末地账号绑定流程 | 现行 |
| [features/endfield_cold_start_notice.md](features/endfield_cold_start_notice.md) | 终末地冷启动提醒 | 现行 |
| [features/endfield_daily_dashboard_ui.md](features/endfield_daily_dashboard_ui.md) | 终末地日常仪表盘 UI 优化交接文档 | 现行 |
| [features/endfield_exploration.md](features/endfield_exploration.md) | 终末地地区探索查询 | 现行 |
| [features/endfield_gacha_card_v3.md](features/endfield_gacha_card_v3.md) | 终末地抽卡分析图 v3 与消息投递 | 现行 |
| [features/endfield_medal_guide.md](features/endfield_medal_guide.md) | 终末地 · 蚀刻章/奖章模块（贡献说明） | 现行 |
| [features/endfield_notification_rules.md](features/endfield_notification_rules.md) | 终末地内容分类与跨来源去重 | 现行 |
| [features/endfield_ownership_stats_renderer.md](features/endfield_ownership_stats_renderer.md) | 终末地干员持有率展示层接口 | 现行 |
| [features/endfield_random_wiki.md](features/endfield_random_wiki.md) | 随机 Wiki 资料 | 现行 |
| [features/endfield_version_calendar.md](features/endfield_version_calendar.md) | Endfield 版本日历 | 现行 |
| [features/forkout.md](features/forkout.md) | 叉出去（plugins/forkout） | 现行 |
| [features/group_features.md](features/group_features.md) | 群内功能管理 | 现行 |
| [features/help_plugin.md](features/help_plugin.md) | 帮助（plugins/help_plugin） | 现行 |
| [features/hyw_plugin.md](features/hyw_plugin.md) | HYW 搜索问答 | 现行 |
| [features/mcsm.md](features/mcsm.md) | MCSM 面板（plugins/mcsm） | 现行 |
| [features/minecraft_plugin.md](features/minecraft_plugin.md) | Minecraft 服务器（plugins/minecraft_plugin） | 现行 |
| [features/peek.md](features/peek.md) | 窥视（plugins/peek） | 现行 |
| [features/radar.md](features/radar.md) | AI 智商雷达（plugins/radar） | 现行 |
| [features/request_handler.md](features/request_handler.md) | 好友与入群请求（plugins/request_handler） | 现行 |
| [features/status_and_usage_stats.md](features/status_and_usage_stats.md) | 运行状态与指令统计 | 现行 |
| [features/steamInfo.md](features/steamInfo.md) | Steam（plugins/steamInfo） | 现行 |
| [features/tibo_radar.md](features/tibo_radar.md) | Tibo 雷达（plugins/tibo_radar） | 现行 |
| [features/tibo_radar_draw_v2_usage.md](features/tibo_radar_draw_v2_usage.md) | Tibo 雷达 · 浅色图片渲染（draw_v2）使用说明 | 现行 |
| [features/tibo_radar_x_usage.md](features/tibo_radar_x_usage.md) | Tibo 雷达 · X 动态卡（draw_xfeed）与通用卡（draw_scope）说明 | 现行 |
| [features/unified_signin.md](features/unified_signin.md) | 双游戏签到合并：`/ak 签到` 与 `/ef 签到` | 现行 |

## 计划

| 文档 | 内容 | 状态 |
|---|---|---|
| [TODO.md](TODO.md) | 待办与未来计划 | 计划 |

## 参考（reference/）

| 文档 | 内容 | 状态 |
|---|---|---|
| [reference/README.md](reference/README.md) | 参考资料 | 参考 |
| [reference/ai_radar_data_dictionary.md](reference/ai_radar_data_dictionary.md) | AI 智商雷达插件 · 数据字典 | 参考 |
| [reference/ai_radar_frontend_api.md](reference/ai_radar_frontend_api.md) | AI 智商雷达 · 前端数据接口文档 | 参考 |
| [reference/ai_radar_interface.md](reference/ai_radar_interface.md) | AI 智商雷达插件 · 上游接口文档 | 参考 |
| [reference/akedata_data_access_guide.md](reference/akedata_data_access_guide.md) | AKEData 取数指南（终末地） | 参考 |
| [reference/endfield_product_facts.md](reference/endfield_product_facts.md) | Endfield challenge page facts | 参考 |
| [reference/endfield_ui_asset_extraction.md](reference/endfield_ui_asset_extraction.md) | 终末地 UI 资源学习提取流程 | 参考 |
| [reference/endfield_warfarin_wiki.md](reference/endfield_warfarin_wiki.md) | Warfarin Wiki 数据源记录 | 参考 |
| [reference/entari-plugin-writing.md](reference/entari-plugin-writing.md) | Entari 插件编写 | 参考 |
| [reference/fz_wiki_open_api_summary.md](reference/fz_wiki_open_api_summary.md) | 终末地 Wiki（fz.wiki / api.fz.wiki）开放 API 调研 | 参考 |
| [reference/skland_endfield_personal_api.md](reference/skland_endfield_personal_api.md) | 森空岛「明日方舟：终末地」个人数据接口 | 参考 |
| [reference/skland_endfield_public_query.md](reference/skland_endfield_public_query.md) | 森空岛终末地公开数据独立查询 | 参考 |
| [reference/skland_endfield_ui_data_inventory.md](reference/skland_endfield_ui_data_inventory.md) | 森空岛终末地 API：UI 数据元素完整清单 | 参考 |
| [reference/skland_medal_id_mapping.md](reference/skland_medal_id_mapping.md) | 森空岛奖章数据解析 + 跨源 id 对比 | 参考 |
| [reference/ake-research/README.md](reference/ake-research/README.md) | AKEData 研究文章（转载） | 参考 |

## 历史（history/）

方案、实施记录、验证、基准与报告。正文已还原为当时的原文，只在第一行加了说明。
基准数据在 `history/benchmarks/`，报告附件在 `history/reports/`。

| 文档 | 内容 | 状态 |
|---|---|---|
| [history/ai_radar_open_items.md](history/ai_radar_open_items.md) | AI 智商雷达插件 · 开放问题与未验证事项 | 历史 |
| [history/ai_radar_plugin_api.md](history/ai_radar_plugin_api.md) | AI 智商雷达插件 · 插件侧接口契约 | 历史 |
| [history/ai_radar_plugin_framework.md](history/ai_radar_plugin_framework.md) | AI 智商雷达插件 · 基础框架设计 | 历史 |
| [history/ai_radar_ui.md](history/ai_radar_ui.md) | AI 智商雷达 · 卡片前端与数据选择 | 历史 |
| [history/bilibili_refactor_plan.md](history/bilibili_refactor_plan.md) | B站订阅插件（bilibilibot）重构方案 | 历史 |
| [history/bugfix_medal_investigator_max_tier.md](history/bugfix_medal_investigator_max_tier.md) | 谷地调查者奖章 档位判定偏差：原因分析（森空岛 level 偏移） | 历史 |
| [history/cache_request_redesign_plan.md](history/cache_request_redesign_plan.md) | 缓存与请求体系改造方案：以查询响应延迟为主矛盾 | 历史 |
| [history/endfield_account_ui_replica_spec.md](history/endfield_account_ui_replica_spec.md) | 终末地账号 UI 复刻规格 | 历史 |
| [history/endfield_ake_followup_fixes.md](history/endfield_ake_followup_fixes.md) | AKE 切换后：富文本与账号页修复 | 历史 |
| [history/endfield_ake_migration_execution.md](history/endfield_ake_migration_execution.md) | End 公共资料默认 AKE：实施记录 | 历史 |
| [history/endfield_akedata_unification.md](history/endfield_akedata_unification.md) | 终末地资料图：AkeData 统一方案 | 历史 |
| [history/endfield_cache_execution.md](history/endfield_cache_execution.md) | End 缓存与查询修复：实施和实测报告 | 历史 |
| [history/endfield_encyclopedia_design_v3.md](history/endfield_encyclopedia_design_v3.md) | 终末地图鉴实现设计 v3 | 历史 |
| [history/endfield_encyclopedia_implementation_plan.md](history/endfield_encyclopedia_implementation_plan.md) | 终末地图鉴查询实现计划（修订） | 历史 |
| [history/endfield_encyclopedia_query_plan.md](history/endfield_encyclopedia_query_plan.md) | 终末地图鉴查询方案（物品 / 道具 / 敌人 / 词条 / 档案条目） | 历史 |
| [history/endfield_entertainment_team_game_design.md](history/endfield_entertainment_team_game_design.md) | Endfield 娱乐小队玩法设计草案 | 计划（未实现） |
| [history/endfield_medal_stats.md](history/endfield_medal_stats.md) | 终末地蚀刻章统计功能需求文档 | 历史 |
| [history/endfield_query_performance.md](history/endfield_query_performance.md) | End 查询性能优化与实测 | 历史 |
| [history/endfield_stage_query_plan.md](history/endfield_stage_query_plan.md) | 终末地关卡资料查询实施 Plan | 历史 |
| [history/endfield_usage_stats_design.md](history/endfield_usage_stats_design.md) | 终末地关卡成员使用率（群内）设计与口径 | 历史 |
| [history/endfield_usage_stats_ui.md](history/endfield_usage_stats_ui.md) | 终末地关卡成员使用率 · 卡片前端设计 | 历史 |
| [history/hyw_frontier_implementation.md](history/hyw_frontier_implementation.md) | HYW 接入 Hyw-Frontier —— 实现说明 | 历史 |
| [history/hyw_vertex_credentials_plan.md](history/hyw_vertex_credentials_plan.md) | HYW 插件兼容 Google 服务账号凭据 —— 修改方案（v2，已实施） | 历史 |
| [history/insight_plugin_plan.md](history/insight_plugin_plan.md) | `/统计` 命令使用统计插件方案（plugins/insight） | 历史 |
| [history/pr7_help_merge.md](history/pr7_help_merge.md) | 合并 PR #7：接收签到卡片，帮助卡片改用新框架 | 历史 |
| [history/refactor_validation.md](history/refactor_validation.md) | 结构重构验证记录 | 历史 |
| [history/tibo_delivery_repair.md](history/tibo_delivery_repair.md) | Tibo 后台推送修复（2026-09-05） | 历史 |
| [history/reports/text-guide-rewrite-2026-10.md](history/reports/text-guide-rewrite-2026-10.md) | otaeBot 文本引导重写 · 改动总结 | 历史 |

图片在 `images/`，被各文档引用。
