# 终末地日常仪表盘 UI 优化交接文档

更新时间：2026-09-11（Asia/Shanghai）

用途：记录日常仪表盘的现行灰白设计、数据边界与离线预览方式。

**设计更新（2026-09-11，第二版）**：参考终末地游戏内行动手册，采用中性灰白底、
深灰文字和固定亮黄色点缀。取消第一版的米黄/橄榄色，以及随数值变化的理智底色和进度条颜色。
**图标更新（第三版）**：用户要求使用终末地/森空岛原图标，并明确可直接裁截图。
三项指标已替换为最初截图里的 ENDFIELD、斜线三角和 P.P. 徽记，取消自绘图标。
这两项要求取代旧版文档中的“深色工业风”、第一版暖色及第二版自绘图标决策。

## 1. 功能概述

命令：`/ef 日常 [全部|编号|昵称|UID后四位]`（别名：`每日`、`仪表盘`、`实时数据`、`dashboard`、`daily`）。

对发送者**每个绑定账号**调用一次森空岛 `GET /api/v1/game/endfield/card/detail`，聚合渲染成
一张 PNG：每个账号一个面板，展示理智（含回满倒计时）、活跃度、每周事务、通行证等级。
某个账号查询失败时降级为浅灰错误面板，不影响其他账号。默认"全部"账号，多账号纵向堆叠。

当前设计：**中性灰白底、深灰字、固定亮黄色强调**（森空岛 App 信息布局，见 §5）；
**仅查询自己**（不支持 @群友）。

## 2. 代码地图

| 位置 | 职责 | 可否修改 |
|---|---|---|
| `plugins/endfield/rendering/cards.py` → `draw_daily_dashboard_card(view)` | 渲染入口，组装面板 HTML | ✅ 本文档的主战场 |
| 同上 → `_daily_account_panel(account, avatars)` | 单账号面板 HTML（正常/失败两种） | ✅ |
| 同上 → `_daily_progress_row(label, current, total, done_text=..., icon=...)` | 图标+标签+分层数值+进度条+状态 | ✅ |
| 同上 → `_daily_value` / `_daily_pair` | None → `--` 的数值格式化 | ✅ |
| 同上 → `_draw_daily_card(selector, body, extra_css)` | 独立亮色外壳（背景/标题/时间/页脚） | ✅ 仅日常卡使用，可随意改 |
| 同上 → `_draw_neutral_card(...)` | **浅色外壳，签到/抽卡/奖章等多卡共用** | ❌ 不要动 |
| `plugins/endfield/catalog/models.py` → `DailyAccountView` / `DailyDashboardView` | 视图模型（字段见 §6） | ⚠️ 改字段需联动，见 §8 |
| `plugins/endfield/account/detail/service.py` → `build_daily_account_view(detail, *, nickname, uid, server_name)` | 从 API payload 容错提取字段 | ⚠️ 只在加字段时改 |
| `plugins/endfield/handlers.py` → `_handle_daily(...)` | 多账号迭代、容错、调渲染、发图 | ⚠️ 一般不用改 |
| `plugins/endfield/catalog/commands.py` → `DASHBOARD_ALIASES` 及解析分支 | 命令注册 | ❌ 与视觉无关 |
| `tests/test_endfield_daily.py` | 命令解析 + 视图构建单测 | 加字段时同步更新 |

## 3. 渲染管线（不要改流程，只改 HTML/CSS）

```text
DailyDashboardView（纯数据）
  → draw_daily_dashboard_card 组装完整 HTML 文档字符串（内联 CSS，无外部资源）
  → _write_temp_html 落临时文件
  → screenshot_web_element(file_uri, ".daily-dashboard-card",
        viewport=(1280, 1), device_scale_factor=2.0,
        max_height=CARD_MAX_HEIGHT(6144), strict_max_height=True,
        wait_for_images=True, settle_ms=30)
  → optimize_png_container（PNG 瘦身，渲染线程池执行）
  → handlers._finish_png → 临时 .png + make_image → 发送
```

要点：

- 逻辑宽 1280px，2x 缩放出图，实际 PNG 宽 2560px。QQ 里以图片消息发送。
- `strict_max_height=True`：整卡高度超过 6144 逻辑 px 会**抛错而不是截断**。当前正常单面板约
  362 逻辑 px，当前短昵称样本 15 账号实测通过；若设计显著加高面板，注意这个上限或引入分页
  （可参考同文件 `draw_gacha_analysis_cards` 的分页降级模式）。
- 截图元素选择器是 `.daily-dashboard-card`（外壳 class 名）。若改外壳实现，保证根节点
  class 与传给 `screenshot_web_element` 的选择器一致。
- 运行环境离线：**禁止 webfont、外链 CSS/JS**。字体栈是
  `'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif`。
- 远程图片（目前只有账号头像 `base.avatarUrl`）必须先经 `_image_data_urls(urls)` 转成
  `data:` URL 注入（内部有缓存与失败容忍），失败时降级为首字方块——保留这个降级。
- 所有用户可控字符串（昵称、错误消息、服务器名）必须过 `esc()` / `esc_attr()`。

## 4. 当前视觉实现

- 外壳：中性浅灰 `#EDEDED` 与轻网格，32px 水平留白；独立亮色标题与细分割线。
- 标题：日常数据、账号数量与更新时间；底部标注森空岛来源。
- 账号：白卡、小圆角、轻阴影；序号、48px 头像、昵称、服务器/脱敏 UID、账号等级。
- 正常面板：左 360px 理智卡，右侧活跃度/每周事务/通行证三行，保持数据含义不变。
- 理智：82px 深色主数值，弱化上限；浅底斜纹与低透明闪电图标；底部回满文案。
- 理智背景固定 `#EEEEEE`；已满/恢复中/缺失使用相同灰色圆点，仅改变文案。
- 进度：当前值与上限分层，6px 细条；填充固定 `#FFE600`，完成时加勾选，颜色保持不变。
- 未知或无有效上限：斜纹空轨道，标注“暂无数据”或“数据不全”；不把未知画成已完成。
- 比例钳制到 0–100%，原数值原样呈现，不篡改超上限或异常负值。
- 失败：浅灰面板、查询失败标签、原因文本；长昵称/服务器/错误文本可换行。
- `_draw_neutral_card` 和其他卡片的共享视觉未修改。

## 5. 参考与本地素材

沿用森空岛浅色 App 截图的层级与两栏结构，配色参考用户新提供的终末地行动手册：
中性灰白、深灰文字、局部亮黄。所有基础颜色均为中性灰，不使用米黄/橄榄色。

`assets/image/endfield/daily/` 保留 AKEData 理智背景闪电 PNG；
三个指标直接裁自用户最初提供的森空岛截图，使用原图像素，不重绘。
显示为统一的 44×44px 图标槽，轮廓保持原样；裁剪坐标与图标对应关系见素材目录 README。
源地址与用途见该目录 `README.md`。`_daily_icon` 通过 `_local_image_data_url` 内嵌素材；
缺文件时用对应单字图标降级。图标不引入在线请求，头像保留原有缓存与首字降级。

## 6. 视图模型与数据边界（能画什么、不能伪造什么）

`DailyDashboardView`：`accounts: list[DailyAccountView]`，`generated_at: str`（"YYYY-MM-DD HH:MM"）。

`DailyAccountView` 字段（全部由 `build_daily_account_view` 从 `card/detail` 提取，缺失为 `None`
→ 渲染 `--`，**不得编造数值**）：

| 字段 | 语义 | 来源（`data.detail.*`） |
|---|---|---|
| `nickname` / `uid` / `server_name` | 昵称 / **已脱敏** UID / 服务器（已本地化） | `base` / 调用方 / `base.serverName` |
| `avatar_url` | 头像远程 URL（可为空） | `base.avatarUrl` |
| `account_level` | 账号等级 | `base.level` |
| `stamina_current` / `stamina_max` | 理智当前/上限 | `dungeon.curStamina` / `maxStamina` |
| `stamina_recover_text` | `"N 小时 M 分回满"` / `"已回满"` / `""`（无数据） | `dungeon.maxTs` − `currentTs` |
| `daily_current` / `daily_max` | 活跃度 | `dailyMission.dailyActivation` / `maxDailyActivation` |
| `weekly_current` / `weekly_max` | 每周事务 | `weeklyMission.score` / `total` |
| `bp_level` / `bp_max` | 通行证等级 | `bpSystem.curLevel` / `maxLevel` |
| `status` / `message` | `"ok"` 或 `"failed"` + 失败原因（如"任务正在进行"、API 错误文本） | handlers 捕获 |

**数据边界（API 不提供，不要画）**：通行证当前经验/升阶进度（只有等级）、理智恢复速率、
活跃度分档奖励明细。**可选扩展数据**：`detail.seekSuspicion.count/total`（疑案进度，字段
结构与 weekly 相同）如需第四行可在 `build_daily_account_view` 加字段（见 §8 联动清单）。
`role.is_primary`（主账号标记）在 handlers 层可取，若想主账号置顶/加标记，需给
`DailyAccountView` 加字段并在 `_handle_daily` 传入。

## 7. 本地预览（无需登录/QQ）

```powershell
.venv/Scripts/python.exe scripts/render_endfield_daily_preview.py --stress
# 可选：用本地图片覆盖第一个样本账号的头像
.venv/Scripts/python.exe scripts/render_endfield_daily_preview.py --avatar path/to/avatar.png
```

脚本使用 synthetic package 绕开 bot 启动，不读取绑定账号或凭据。
产物在忽略目录 `output/daily-dashboard/`：

- `single.png` / `.html`：单账号正常数据。
- `multi.png` / `.html`：5 账号，覆盖进行中/已满、0%/100%、失败、缺失与长昵称。
- `empty.png` / `.html`：空账号集合。
- `boundaries.png` / `.html`：部分缺失、零上限、负值/超上限、超长错误文本、HTML 转义。
- `stress-15.png` / `.html`：加 `--stress` 时生成 15 个正常账号。
- `validation.json`：浏览器实测尺寸、文字溢出、缺图、外链请求、进度宽度，以及不同状态下的颜色一致性。

脚本调用正式截图管线，按 2x 输出 PNG，并用独立浏览器检查 HTML。
实际 QQ 发送需在机器人运行环境调用 `/ef 日常`，本预览不模拟账号接口或消息发送。

## 8. 修改约束（硬性）

1. **`_draw_neutral_card` 与其他卡片的渲染代码不可改动**——只动 `draw_daily_dashboard_card`
   `_daily_*` `_draw_daily_card` 这一簇，或新增同文件私有函数。
2. **`DailyAccountView` / `DailyDashboardView` 现有字段名与语义保持稳定**（handlers 与测试依赖）。
   若确需加字段，联动清单：`catalog/models.py` 加字段 → `account/detail/service.py`
   `build_daily_account_view` 填充 → `handlers.py` `_handle_daily` 传参（如需 role 信息）→
   `tests/test_endfield_daily.py` 补断言。
3. 用户数据一律 `esc`/`esc_attr`；数值缺失去向 `--`，不得编造。
4. 离线渲染：无 webfont/外链；远程图仅 `data:` URL 注入且必有降级。
5. 高度上限 6144 逻辑 px（`strict_max_height` 会抛错），多账号纵向堆叠时评估单面板高度。
6. 改完必须全绿：
   ```bash
   .venv/Scripts/python.exe -m pytest tests/test_endfield_daily.py tests/test_endfield_account.py
   .venv/Scripts/python.exe -m py_compile plugins/endfield/rendering/cards.py
   ```
   （注意 `tests/test_architecture.py` 有一个与本功能无关的预存失败：工作区多出
   `bilibilibot`/`McModQuery`/`McWikiQuery` 插件不在期望清单，忽略即可。）

## 9. 验证记录与验收

2026-09-11 第三版（中性灰白、森空岛截图图标、固定强调色）：

1. `pytest tests/test_endfield_daily.py tests/test_endfield_account.py`：161 passed。
2. `py_compile plugins/endfield/rendering/cards.py`：通过。
3. 单账号：1280 × 571.58 逻辑 px；正常面板约 362px。
4. 5 账号：1280 × 1886.33 逻辑 px；15 账号：1280 × 5894.20 逻辑 px，低于 6144 上限。
5. 预览无文字溢出、无缺图、无外链请求。0%、100%、缺失、超上限、失败和空态均出图检查。
6. 未改变视图模型、账号查询、命令或发送链路；本轮未连接真实 QQ 验证发送。
7. 浏览器检查满值/未满/缺失样本：进度填充始终 `rgb(255,230,0)`，理智背景始终 `rgb(238,238,238)`，状态圆点始终 `rgb(119,119,119)`。

高度与昵称换行数量相关，15 账号短昵称样本通过不代表任意长度文本均可容纳。
后续若需支持更多账号，沿用现有分页模式另行调整；不要截断卡片或吞掉账号。
