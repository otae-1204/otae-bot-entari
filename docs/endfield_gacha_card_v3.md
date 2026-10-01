# 终末地抽卡分析图 v3 与消息投递

`/ef 抽卡` 的分析图：三栏（特许寻访 | 重构寻访 + 其他寻访 | 武器申领）+ 4 格总览，沿用家族 B 中性灰外壳。
**不折叠、不截断**：每个池（包括全部历史池）、每条记录（六星、信物与申领赠礼、免费十连、加急招募）都完整显示，
长文本换行；长度只靠分页，单页不超过 4096 逻辑 px。超过 3 张图时以 QQ 合并转发发送。

## 代码位置

| 位置 | 作用 |
| --- | --- |
| `plugins/endfield/gacha/draw.py` | v3 全部布局知识：分栏与排序、保底格、记录行、测量页、分页（`paginate_gacha`）、页面 HTML、截图外壳 |
| `plugins/endfield/rendering/cards.py` `draw_gacha_analysis_cards` | 入口与回滚开关：默认转发到 v3；`ENDFIELD_GACHA_LAYOUT=v1` 走旧版两栏（`_draw_gacha_analysis_cards_v1`） |
| `otae_bot/infrastructure/rendering/browser.py` `evaluate_web_page` | 在共享浏览器线程里加载页面并执行测量脚本，与截图同一套请求路由 |
| `plugins/endfield/handlers.py` `_finish_gacha_pngs` | 抽卡投递：≤3 张普通消息，>3 张合并转发，逐级回退 |
| `otae_bot/adapters/onebot.py` `send_forward_images` | OneBot 合并转发；抽卡路径额外传节点名、`file://` 路径与 60 s 超时 |
| `scripts/render_endfield_gacha_preview.py` | 合成账号预览与校验（见文末） |

外壳 `_draw_neutral_card` 被签到、抽卡记录、奖章、档案等多张卡共用，**未改动**；抽卡用自己的
`_draw_gacha_shell`，token 与它一致，只是上限改为 `GACHA_PAGE_MAX_HEIGHT = 4096`，并检查
`.pool-column / .pool-card / .pull-row / .free-row / header` 的溢出。其他卡片仍用 `CARD_MAX_HEIGHT = 6144`。

## 分栏与排序

后端只给出 `kind_key`、`series_key`、`series_index`、`is_current`、`latest_ts` 等字段（不排序、不分栏），
栏位完全由 `draw.py` 的 `GACHA_COLUMNS` 决定：

| 栏 | 分组 | kind_key |
| --- | --- | --- |
| ① 特许寻访 | — | `special` |
| ② 重构寻访 | 上：重构寻访 | `rerun` |
| | 下：「其他寻访 · 各池独立保底」 | `joint` `standard` `beginner` `unknown_char` |
| ③ 武器申领 | — | `weapon_limited` `weapon_rerun` `weapon_constant` `unknown_weapon` |

- **当期卡**（`FEATURED_KINDS` = 特许、重构、限时申领、点绘申领，且 `is_current`）排在最前，带 CURRENT 标签、
  「当前累计」行和保底格；按 kind 顺序（限时 → 点绘）。
- 其余按 `(系列最近抽取时间, series_key, series_index, latest_ts)` 倒序：同一系列的各期始终相邻、新期在前。
  复刻无论是同 poolId 再开放（`pool_version` 不同）还是换新 poolId，`series_key` 相同，所以版面一致；
  多期系列的卡片副标题写「第 n 期」，历史期写「累计已继承至第 m 期」。
- 其他寻访、常驻申领即使是该类最新一期也按历史池样式展示（垫抽数仍在池头右侧）。
- `kind_key` 为空（旧夹具）时交给注册表 `resolve_pool_kind` 识别，前端不写名称子串判断。
- 基础寻访：`GachaAnalysis.show_standard_pools`（环境变量 `ENDFIELD_GACHA_SHOW_STANDARD`，未设读
  `GACHA_SHOW_STANDARD`，默认展示）为假时不出卡片，「其他寻访」组末尾留一行虚线说明
  「基础寻访已按设置隐藏：N 抽 · M 个六星，仍计入总数与角色寻访」；分组标题的统计只算可见池；
  只剩基础寻访时分组标题与说明照样保留。总览数字不变。

## 卡片

池头：名称（多期系列取系列名）、副标题（类型 · 第 n 期 · 最近日期 · 付费六星数 · 继承说明 · 统计补齐 · 同步失败），
右侧「总数 / 付费 · 免费 / 垫抽」。副标题各段 `white-space:nowrap`，只在「 · 」之后换行。有立绘横幅时显示在池头左侧。

保底格（只给当期卡）：

| kind | 格 | 数据来源 |
| --- | --- | --- |
| `special` | 距小保底（特许间共享继承，进软保底时加注）· 距大保底（120）· 距下次信物（240） | `small_pity_*`、`large_pity_*`、`keepsake_progress/claims` |
| `rerun`（2×2） | 距六星保底（全部重构共享继承）· 距大保底（120，本期首个当期 UP）· 距加急招募（系列累计 x/30·60·90，已获 n/3，未用 k）· 距下次信物（系列累计，含继承 y 抽） | 另读 `rush_*`、`series_total`、`series_inherited_total` |
| `weapon_limited` | 距小保底（x 次十连）· 距大保底（80） | 首个 UP 已出时显示「已触发 · 第 N 抽获得 UP」 |
| `weapon_rerun`（2×2） | 距小保底 · 距大保底 · 距武库赠礼 · 距点绘赠礼（系列累计申领次数，含继承） | `next_rewards`、`series_claims`、`series_inherited_claims` |

字段缺失时显示「--」/「无」/「待识别」，不会报错。

记录行（每行带 `data-row="<card_key>:<序号>"` 与 `data-kind`）：当前累计 → 六星与赠礼（池内位置新→旧）→
免费十连 / 加急招募 → 空态。赠礼文案由 `(kind_key, gift_kind)` 决定：信物「第 240 抽赠送信物」（重构按系列累计写
「累计第 N 抽」）、点绘申领的 UP 武器「点绘赠礼」、武库箱「武库赠礼」、其他武器池「UP武器」。
加急招募行：「加急招募 · 角色名 / 未出六星」，副行「日期 · 累计 60 抽获得 · 不计保底」。

## 分页（按实测高度装箱）

1. **测量**（1 次页面加载）：测量页里放两张卡——首页卡的每栏放入该栏全部池的完整卡片与续页池头样本，以及分组标题、
   隐藏说明、空态、提示块、“已展示完毕”块；续页卡只有续页外壳。读出每个池的池头、续页池头、每行高度、行间距、
   栏头和首页 / 续页开销。页眉按 99/99 页渲染，长昵称换行只会被高估。
2. **单页**：首页开销 + 最高一栏 ≤ 4096 − 16 就出 1 页。
3. **按栏装箱**：第 p 页栏高上限 `min(L + (p>0 ? 首页开销−续页开销 : 0), 可用高度)`；整栏放得下即末页，
   否则底部预留「续见第 n 页」。池卡片只在两行之间拆分（≥4 行才拆、每段 ≥2 行），续段用「（续）」池头
   （「接第 n−1 页 · 第 a–b / 总 条」）；分组标题不落单；续页从「其他寻访」中间开始时先补「其他寻访（续）」；
   已显示完的栏显示「本栏已在第 k 页展示完毕」。
4. **最少页数 + 均衡**：先用最宽松的 L 得到最少页数 N，再二分求能装进 N 页的最小 L（约 12 次装箱，毫秒级）。
5. **截图**：每页一次，`strict_max_height`。某页意外超高时用 4 倍余量（64 px）重新装箱再试一次；测量失败、
   再次超高或检测到版面溢出时回退到 v1 两栏并记 error 日志。

第 2 页起没有总览，标题为「昵称 · 抽卡分析（续）」，右上角「同步正常 · n/N」，页脚「第 n/N 页」。

## 投递

| 页数 | 发送方式 |
| --- | --- |
| ≤ 3 | 与其他命令相同：一条消息里放全部图片 |
| > 3 | 一条 QQ 合并转发，每个节点 1 张图，节点名「终末地抽卡分析」，头像为 bot 自己；超过 50 个节点拆成多条 |

回退链与日志（`[endfield-gacha]`）：

1. Satori `<message forward>` → `deliver mode=forward via=satori pages= bytes= elapsed=`
2. 抛错或无 session → OneBot `send_group_forward_msg / send_private_forward_msg`：节点直接引用已写好的临时文件
   （`file://`），HTTP 回退超时 60 s；日志 `satori forward element failed …` 与 `via=onebot`
3. 仍失败 → 每条 ≤3 张分批发送，第一条带一句说明；日志 `merged forward unavailable, fallback=batches …` 与 `mode=batches`

`file://` 与 60 s 超时**只在抽卡路径生效**（`_send_forward_pngs(..., onebot_timeout=...)` 显式开启）；
影拓 / 回响历史的规则不变：>2 页合并转发，失败时回复文字提示分页查看，OneBot 回退仍是 base64 + 10 s；
`call_onebot_action` 的其他调用方默认超时仍是 10 s。

## 配置与回滚

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `ENDFIELD_GACHA_LAYOUT` | `v3` | `v1` 回到旧版两栏（6144 上限、按行数预算分页） |
| `ENDFIELD_GACHA_FORWARD_ABOVE` | `3` | 超过几张改为合并转发；`0` 关闭合并转发（一律普通消息） |
| `ENDFIELD_GACHA_SHOW_STANDARD`（或 `GACHA_SHOW_STANDARD`） | 展示 | `0/false/no/off` 隐藏基础寻访卡片（仍计入总数） |

## 新增池类型

1. 后端：在 `plugins/endfield/gacha/pools.py` 注册新的 `PoolKind`（规则参数、识别前缀 / 枚举、`sort_rank`）。
2. 前端：把它的 `kind_key` 加进 `draw.py` 的 `GACHA_COLUMNS` 某个分组。漏加时导入 `draw.py` 会直接断言失败
   （`tests/test_endfield_gacha_layout.py` 也会覆盖）。
3. 如需当期卡（CURRENT、当前累计行、保底格），加入 `FEATURED_KINDS`，并在 `build_pity_cells` 写保底格模板。
4. 赠礼文案不同则补 `gift_label`；更新本文的分栏表与保底格表；用预览脚本加一个样本确认版面。

## 预览与校验

```bash
python scripts/render_endfield_gacha_preview.py --output-dir output/gacha-preview [--accounts normal stress] [--keep-html]
```

不读凭据、不联网：合成账号（normal、heavy、sparse、stress、standard-hidden）的记录、名称、图标与横幅全部虚构，
图标为本地生成的灰度占位图。每个账号走 `build_gacha_analysis → draw_gacha_analysis_cards` 的正式路径，
截图用的 HTML 被截获后在同一浏览器里重新校验，结果写入 `validation.json`：每页 PNG 尺寸（宽 2560 = 1280×2，
高 ≤ 8192）、横向溢出、裁切、越界、重叠、省略号样式、坏图与外链；跨页汇总记录缺失 / 多余 / 重复 / 被切断、
每个池恰好一个完整池头（`truncated_or_omitted` 必须为 0）。

测试：`tests/test_endfield_gacha_layout.py`（分栏、保底格、记录行、分页纯函数、渲染入口与回退、真实浏览器单页），
`tests/test_endfield_challenge.py::EndfieldGachaDeliveryTests`（投递与回退、环境变量、节点拆分、影拓规则不变）。
