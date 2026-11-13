# 明日方舟：终末地（plugins/endfield）

终末地的公开资料查询（干员、武器、装备、关卡、图鉴）和绑定森空岛账号后的个人数据（签到、日常、抽卡、挑战记录等），以及群内匿名统计和国服公告提醒。本页是索引，细节见各详细文档。

## 指令入口

根指令 `/ef`，别名 `/zmd` `/终末地` `/endfield`，四者等价；指令统计里合并显示为 `/ef`。另有四个快捷指令：

| 快捷指令 | 等价于 |
|---|---|
| `/efop` `/efoperator` `/终末地干员` | `/ef 干员 …` |
| `/efwp` `/efweapon` `/终末地武器` | `/ef 武器 …` |
| `/efeq` `/efequipment` `/终末地装备` | `/ef 装备 …` |
| `/efs` `/efsearch` `/终末地搜索` | `/ef 搜索 …` |

`/ef`、`/ef 帮助`（`help` `h` `?`）发送帮助图；帮助图内容来自 `scripts/help_pages.json` 的 `endfield` 页，渲染不可用时退回文字帮助（`catalog/commands.py` 的 `format_help`）。

下表 `[账号]` 可填编号、昵称或 UID 后四位，不填用主账号。

## 子命令

| 分组 | 指令（主要别名） | 说明 | 详细文档 |
|---|---|---|---|
| 账号 | `/ef 绑定`（`添加账号`）、`/ef 主账号 <编号>`、`/ef 解绑 <编号>` | 绑定森空岛账号（国服 Token / 短信验证，亚服 Token）；仅私聊 | [endfield_binding.md](endfield_binding.md) |
| 账号 | `/ef 账号 [账号]` | 账号概览图（干员、武器装备、技能与潜能） | — |
| 账号 | `/ef 账号 基建 [账号]` | 据点产出、票据库存与干员心情 | — |
| 账号 | `/ef 养成统计 [账号]`（`资源消耗`） | 养成消耗与材料总览 | — |
| 账号 | `/ef 资源流水 [账号] [日期…]`（`流水`） | 源石、嵌晶玉、武库配额流水，默认近 30 天；`-d N` `-a` `--资源` `--类型` | — |
| 签到 | `/ef 签到 [全部\|账号]` | 森空岛每日签到，默认全部；同时绑定了明日方舟时顺带签到并合成一张图 | [endfield_attendance_card.md](endfield_attendance_card.md)、[unified_signin.md](unified_signin.md) |
| 日常 | `/ef 日常 [全部\|账号]`（`每日` `仪表盘`） | 理智、活跃度、每周事务、通行证 | [endfield_daily_dashboard_ui.md](endfield_daily_dashboard_ui.md) |
| 探索 | `/ef 探索 [账号]`（`/ef 账号 探索`） | 全部地区六类收集进度 | [endfield_exploration.md](endfield_exploration.md) |
| 抽卡 | `/ef 抽卡 [账号]`、`/ef 抽卡同步 [账号] [--full]` | 同步记录并生成分析卡，超过 `gacha_forward_above` 张合并转发 | [endfield_gacha_card_v3.md](endfield_gacha_card_v3.md) |
| 抽卡 | `/ef 抽卡记录 [账号] [页码] [--池 名称]` | 分页查看抽卡明细 | — |
| 抽卡 | `/ef 抽卡导入 [账号]` | 用手机验证码导入小黑盒历史统计；仅私聊 | — |
| 挑战 | `/ef 影拓 …`、`/ef 回响 …` | 影拓丰碑 / 战争回响的当期进度、历史、指定关卡记录；群聊末尾 @群友 查对方主账号 | — |
| 使用率 | `/ef 影拓 使用率 …`、`/ef 回响 使用率 …`、`/ef 使用率 刷新 [群内\|全局]` | 本群已绑定成员最佳记录阵容的干员使用率（`plugins/endfield/usage`）；刷新需群管或机器人管理员 | — |
| 持有率 | `/ef 持有率 [群内\|全局]`、`/ef 持有率 刷新 [群内\|全局]` | 匿名干员持有率；群聊默认本群，私聊默认全局 | [endfield_ownership_stats_renderer.md](endfield_ownership_stats_renderer.md) |
| 奖章 | `/ef 奖章`、`/ef 奖章 刷新`、`/ef 奖章 缺章 [账号]` | 蚀刻章总数与新增、同步 AKEData、个人缺章检查 | [endfield_medal_guide.md](endfield_medal_guide.md) |
| 档案 | `/ef 档案`、`/ef 档案 刷新`、`/ef 档案 收集 [账号]`、`/ef 档案 <名称>` | 档案总数与新增、个人收集进度、条目详情 | [endfield_archive_guide.md](endfield_archive_guide.md) |
| 资料 | `/ef <关键词>`、`/ef 干员\|武器\|装备 [名称]`、`/ef 搜索 <关键词>` | 资料卡与目录；`--source fz\|akedata\|warfarin` 指定数据源，`--rarity` 筛品阶 | — |
| 关卡 | `/ef 关卡 [名称] [变体\|总览]`（`副本`） | 关卡资料，只用 AkeData | — |
| 图鉴 | `/ef 物品\|道具\|敌人\|词条 [名称]` | 百科图鉴，只用 AkeData | — |
| 配装 | `/ef 配装 [干员 武器 装备… 角色潜能N 武器潜能N]` | 配装模拟；不带参数时交互输入 | — |
| 速算 | `/ef 速算 <等级+效果> <技艺强度>` | 腐蚀 / 导电 / 碎甲伤害速算，如 `/ef 速算 2腐蚀 200` | — |
| 日历 | `/ef 版本日历`（`日历`） | 当前版本活动与关卡开放日程 | [endfield_version_calendar.md](endfield_version_calendar.md) |
| 公告 | `/ef 公告 …`（`活动提醒`） | 订阅国服活动公告并提前提醒；`/ef 公告 帮助` 查看子命令 | [endfield_announcements.md](endfield_announcements.md)、[endfield_notification_rules.md](endfield_notification_rules.md) |
| 随机 | `/ef 随机`（`/zmd 随机` `random`） | 随机一条公开 Wiki 资料，无需绑定 | [endfield_random_wiki.md](endfield_random_wiki.md) |
| 其他 | `/ef 数据源` | 数据源说明 | — |
| 管理 | `/ef dev …`、`/ef 别名 添加 …` | 仅 SUPERUSER，其他人看到「未知指令」 | — |

首次查询公开资料时可能先回一条冷启动提示，见 [endfield_cold_start_notice.md](endfield_cold_start_notice.md)。

## 配置

`entari.yml` 的 `plugins: plugins.endfield:` 段（`plugins/endfield/config.py`）。

| 键 | 默认 | 说明 |
|---|---|---|
| `credential_key` | 空 | 账号凭据加密密钥（密钥，`.env` 的 `ENDFIELD_CREDENTIAL_KEY`，Base64 编码的 32 字节）；留空时禁用绑定与凭据读取 |
| `gacha_layout` | `v3` | 抽卡分析版式：`v3` 或 `v1`（回滚用） |
| `gacha_forward_above` | 3 | 抽卡分析图超过几张改为合并转发（0 = 关闭） |
| `gacha_show_standard` | true | 是否展示基础寻访卡片 |
| `gacha_rerun_side` | `right` | 重构侧栏位置：`right` 或 `left` |

## 数据

`data/endfield/` 下：

| 文件 | 内容 |
|---|---|
| `endfield.db` | 绑定凭据（加密）与角色、抽卡记录与小黑盒导入、资源流水、持有率用的干员快照、影拓 / 回响记录（使用率来源） |
| `announcements.db`、`notification_receipts.db` | 公告订阅、提醒任务与投递回执 |
| `medal_snapshot.json`、`archive_snapshot.json` | 奖章 / 档案版本对比基准 |
| `collection_refresh.json` | 奖章 / 档案刷新状态 |
| `image_cache/` | 抽卡图片资源缓存 |

插件自带静态资源在 `plugins/endfield/assets/`，别名表 `plugins/endfield/alias_data.json`。

## 推送与定时任务

| 任务 ID | 周期 | 内容 |
|---|---|---|
| `endfield_announcement_reminders` | 1 分钟 | 检查到期的公告提醒并推送 |
| `endfield_ownership_refresh` | 10 分钟 | 补齐到期的持有率快照 |
| `endfield_ownership_catalog_refresh` | 6 小时 | 刷新持有率用的干员目录 |
| `endfield_usage_refresh` | 10 分钟 | 补齐到期的使用率快照 |

只有公告提醒会主动发消息：经 `otae_bot.delivery.deliver` 发送，显式指定订阅时记录的平台与机器人账号；该账号不在线或本群关闭了 `endfield` 功能时延后，不换账号。签到等结果只在指令会话内回复。

## 详细文档

- 上表各功能文档
- [../operations/endfield_ownership_stats_operations.md](../operations/endfield_ownership_stats_operations.md)：持有率运维
- [../operations/endfield_gacha_batch_scripts.md](../operations/endfield_gacha_batch_scripts.md)：抽卡批处理脚本
- [status_and_usage_stats.md](status_and_usage_stats.md)：指令统计（`/ef` 子命令的统计口径）
- 外部接口与数据：[../reference/skland_endfield_personal_api.md](../reference/skland_endfield_personal_api.md)、[../reference/skland_endfield_public_query.md](../reference/skland_endfield_public_query.md)、[../reference/akedata_data_access_guide.md](../reference/akedata_data_access_guide.md)、[../reference/endfield_warfarin_wiki.md](../reference/endfield_warfarin_wiki.md)、[../reference/fz_wiki_open_api_summary.md](../reference/fz_wiki_open_api_summary.md)、[../reference/skland_medal_id_mapping.md](../reference/skland_medal_id_mapping.md)
