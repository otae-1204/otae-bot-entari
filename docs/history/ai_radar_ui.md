# AI 智商雷达 · 卡片前端与数据选择

> **布局修订**：用户提供 demo 后，默认 `/radar` 已改为三列模型 × 档位矩阵，见 [新版总览设计](ai_radar_matrix_ui.md)。下文保留上一版细分查询卡片的说明；默认入口不再是导航长卡。

2026-09-14，基于主分支 `808353b` 的数据接口实现。这里的前端是 **QQ 机器人回复卡片**：HTML/CSS 排版，经现有 Playwright 渲染为 PNG，同时提供离线 HTML 预览。

## 使用与预览

重启或重载插件后，`/radar` 与 `/radar 帮助` 显示导航卡；现有查询命令直接返回图片。无需增加配置项或安装前端依赖。渲染或图片发送报错时自动发送原有文本；取消操作不触发补发。多页结果先完成整组渲染，再依次发送。

```bash
.venv/bin/python scripts/preview_ai_radar.py
```

Windows 使用 `.venv\Scripts\python.exe`。与项目其他 HTML 卡片一样，需要已安装 Playwright Chromium；安装方式见 README。

生成入口：[`output/ai-radar/index.html`](../output/ai-radar/index.html)。点击卡片可打开适应手机宽度的 HTML，或下载 PNG。图片宽 1080 px，按内容确定高度；长榜单每页 8 行，模型档案每页 6 档，性价比每页 3 项，推荐每个场景独立成页。渲染高度超限会回退文本，不静默裁切。

预览使用 `tests/fixtures/radar/` 的录制与重建夹具，经真实 provider / service / 命令分派生成视图。**不是实时数据，也不是完整榜单。** 所有预览带提示；长名称、缺失预警、单点曲线是单独标注的合成边界示例。不会启动 bot、发送群消息或请求实时 API。

| 页面 | 命令 | 预览图 |
| --- | --- | --- |
| 导航手册 | `/radar` | [guide.png](../output/ai-radar/guide.png) |
| 模型实力榜 | `/radar 榜` | [ranking.png](../output/ai-radar/ranking.png) |
| 模型档案 | `/radar 模型 astra low` | [model.png](../output/ai-radar/model.png) |
| 模型对比 | `/radar 对比 astra sol` | [compare.png](../output/ai-radar/compare.png) |
| 场景选型 | `/radar 推荐` | [recommend.png](../output/ai-radar/recommend.png) |
| 波动观察 | `/radar 预警` | [alerts.png](../output/ai-radar/alerts.png) |
| 成本与效率 | `/radar 性价比` | [value.png](../output/ai-radar/value.png) |
| IQ 趋势 | `/radar 趋势 astra low` | [trend.png](../output/ai-radar/trend.png) |
| 评测频道 | `/radar 频道` | [channels.png](../output/ai-radar/channels.png) |
| 可用档位 | `/radar 档位` | [tiers.png](../output/ai-radar/tiers.png) |

在查询末尾追加 `@pompeii-adjacency` 等频道参数。模型档案和趋势建议显式带档位；省略时遵循现有服务语义，并在图上写明各组数据的实际档位或“跨档位合并”。

## 视觉选择的用意

浅纸色背景、深墨绿文字模拟一份可阅读的观察报告，适合群聊放大查看。薄荷绿只突出首位得分和品牌图形；琥珀色标识预警、陈旧缓存及预览说明。正常空状态不使用代表安全承诺的绿色大勾。

页面顺序为：查询身份与频道 → 主要数据 → 解释 → 来源、时间与口径。每张卡都有“为什么看这些数据”，让转发后的图片仍可独立理解。页脚的技术口径标识用于核对来源，不占主要数据区。

**使用条形图和时间曲线，不制作多维能力雷达图。** 当前接口没有一组相同尺度的独立能力轴；将 IQ、费用、耗时、样本数拼成一个雷达图，会让多边形面积暗示一个并不存在的总能力。

## 各项数据为什么选

| 数据 / 字段 | 放在哪里 | 选择用意与展示约束 |
| --- | --- | --- |
| `model` + `effort` | 每个模型名称旁 | 两者共同决定配置身份，避免把高档能力和低档价格拼在一起。支持哪些档位由评测目录提供。 |
| `ModelRow.pass_rate` | 榜单主列、档案、对比 | 在**同频道**比较实际评测表现。二值频道显示百分比；连续频道显示 `Macro-F1` 小数。横条以完整 0–1 为尺度。 |
| `ModelRow.iq` + `iq_derived` | 榜单参考列、档案、对比 | 保留用户关心的 IQ，并将“综合 IQ”和“频道换算 IQ”写在数字下方。不从综合 IQ 反推通过率。 |
| `graded` + `cells` | 同一行的样本列 | 分别交代有效运行样本和覆盖题目；多次运行不是多道题，也不能仅凭数量认定统计显著。 |
| `InsightPoint.iq / software_iq / visual_iq / samples` | 档案的综合视角 | 一起保留上游加权结果、构成和样本。两频道题目难度不同，视觉分更高不能解释成视觉能力更强。 |
| `EfficiencyPoint.iq` | 成本页 | 让效果与成本来自同一个效率端点，避免借用综合 IQ 给该成本组合加分。 |
| `passed / total` | 成本页与档案的效率区 | 展示该效率快照自身的评测得分和样本；连续频道按 F1 展示，加权通过量绝不显示为整数题数。 |
| `average_price_usd` / `average_cost_usd` | 成本、档案、推荐、对比 | 交代预算。直接引用 API 等价平均成本；不按 token 价格表重新算账，不称为用户实际账单。当前聚合结构未带逐项实测来源，因此保守标 `~` 和“估算”。 |
| `average_minutes` / `average_duration_minutes` | 同上 | 交代等待时间，帮助区分省钱和省时。来源标记未提供时同样保守标估算，缺失不当作 0 分钟。 |
| `combined_cost_index` | 成本榜、推荐 | 保留服务已有的性价比排序，明确向下越小越划算；费用与耗时同时给出，指数不替代实际投入。 |
| `runs_total` | 效率区次要说明 | 补充累计运行规模，与当前样本 `total` 分开标注。 |
| 推荐 `title / rule / items` | 场景选型 | 从日常开发等实际场景缩小候选范围；完整引用上游选取规则，保持候选顺序，不本地生成新推荐结论。 |
| 推荐 `weighted_passed` + `samples` | 推荐卡次要说明 | 保留上游证据规模，标“加权通过量 / F1 加权和”，明确不是通过题数。接口未提供同期通过率，不从 IQ 反推。 |
| 预警 `current_iq / avg_24h / avg_48h` | 波动观察 | 呈现上游与模型自身历史对照的依据。转发原有预警，不从曲线自行判定降智。 |
| 预警 `delta_24h / delta_48h` | 对应基线下方 | 保留上游降幅定义，不用当前值减均值替换它们。缺字段显示“—”。 |
| `TrendPoint.timestamp / iq / samples` | 趋势图 | 横轴用实际 UTC 时间，纵轴按本序列取值范围自适应（与雷达站前端 `trendScale` 同口径：上下各留 20% 余量、跨度下限 3 分、钳到 0–150），实际窗口在图上写明，避免等距时间或截断纵轴夸大变化。起止点显式带样本；HTML 悬停可查看各点时间与样本。 |
| 趋势 `meta.note` | 图标题附近 | 区分单档位与跨档位合并；不使用 `latest:` 的另一套窗口，也不把历史 IQ 与推荐趋势拼成一条线。 |
| `BenchmarkInfo` 的题目、配置数量、计分方式、窗口 | 频道页 | 解释测的是什么、覆盖多少配置，以及得分如何读。显示切换频道的可复制命令。 |
| `source_updated_at` / `fetched_at` / `stale` | 页脚、缓存提示及效率点旁 | 区分数据时间与本次获取时间；优先显示效率点自己的更新时间。陈旧缓存必须明确提示“数据可能过期”。 |
| `mode / scoring_mode / recommendation_mode` | 页脚 | 保留接口提供的统计口径，帮助复核两个看似相同分数为何不同；接口没有提供则为“—”。 |

榜单命令改为调用 `top_models(by="pass_rate")`，仍沿用“每个模型最高档”筛选。这保证名次来自同一频道的同一种分数。服务层对外默认值不变；其他调用方仍可选择原来的 IQ 排序。

## 哪些数据暂不突出

- **token、缓存命中率、平均 agent 步数**：适合解释运行机制，但不能直接代表能力或费用；`model-metrics` 与主榜窗口也不同，首版不再增加一组容易混淆的指标。
- **提交吞吐、贡献者、积分、认领、判分流水**：不是当前命令面“只看智商相关”的重点，继续沿用服务已有能力，卡片不新增入口。
- **预警高点、严重度**：部分字段语义未验证，不据此制作风险等级或猜测阈值。
- **自行计算涨跌幅、趋势平滑、综合评分或置信等级**：当前接口没有充分定义，不添加会制造确定性的推导。

## 已知数据边界

`ModelProfile` 多档位返回值中，`best` 可能是最高档，而 `efficiency / insight / trend` 对应 `variants[0]`。档案辅助区分别显示真实档位；对比页只使用与 `best.key` 匹配的效率数据，不匹配就显示“—”。不同来源的 IQ 不显示差值；两侧缓存标记分别显示。

当前 `ModelRow` 合并 IQ 后没有携带独立的 insights 更新时间、样本与陈旧状态，`ModelProfile` 也未携带各辅助端点的完整 meta。因此，**页脚的主数据时间不能被解释成每项辅助数据都在该时刻更新**；效率区显示可取得的 point 时间，综合 IQ 独立时间仍为缺失。前端不伪造新鲜度，也不绕过服务层额外出网。后续应由服务增加逐来源 metadata，再进一步细化。

预警接口未提供独立预警样本或通过率；有曲线时展示末点样本，没有就标缺失。空预警只表示快照没有条目，不是稳定保证。无效趋势点断线而非补零；单点画可见圆点；0 分保留为真实值。

图片发送如果在中途报错，会补发整份文本以保全内容；已经发送的页可能与文本重复，不做自动重试或后台推送。实际 QQ 客户端的图片接收情况仍需运行中的 bot 验证；本地不会代发群消息。

## 文件与验证

- `plugins/radar/presentation.py`：纯函数视图、数据映射、SVG 图表、分页和数据选择解释。
- `plugins/radar/assets/card.css`：共用桌面 / 手机样式，无第三方 CSS / JS 依赖。
- `plugins/radar/rendering.py`：内嵌本地 MiSans 字体、CSP、共享浏览器截图与临时文件清理。
- `plugins/radar/handlers.py`：接入图片回复，保留文本回退；并发闸门与超时覆盖查询、渲染和发送。
- `scripts/preview_ai_radar.py`：离线预览、图集和浏览器布局检查。
- `tests/test_radar_rendering.py`：混合 IQ 来源、连续制得分、缺失与 0、档位对齐、分页、时间坐标、注入、发送回退、取消、并发与真实 PNG 渲染。

```bash
.venv/bin/pytest tests/test_radar.py tests/test_radar_rendering.py tests/test_architecture.py -q
.venv/bin/ruff check plugins/radar/presentation.py plugins/radar/rendering.py plugins/radar/handlers.py scripts/preview_ai_radar.py tests/test_radar_rendering.py
.venv/bin/python scripts/preview_ai_radar.py
```

浏览器验证报告在 `output/ai-radar/validation.json`：逐页检查 1080 px 与 390 px 布局、元素溢出、字体加载、缺图、最高高度与外部请求。渲染资产全在本地，CSP 禁止外部连接与脚本执行。
