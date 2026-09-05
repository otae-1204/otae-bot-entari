# Endfield 版本日历

## 目标

`/zmd 版本日历` 优先自动同步终末地官网当前展示的简体中文版日历，并组合成 1080×1632 PNG。官网不可用或改版导致资源发现失败时，自动降级到 AkeData 驱动的 HTML/CSS 日历。

生成链路：

```text
终末地官网首页
  -> 当前 Next.js layout 构建文件
  -> 定位简中 calendar.title / timeline / content
  -> 校验官方 CDN、MIME、尺寸和文件体积
  -> HTML/CSS 无损拼合
  -> Chromium 截图
  -> 1080×1632 PNG

官网链路失败
  -> AkeData manifest / 当前 TableCfg
  -> 活动、时间、卡池、文本表
  -> current.json 视觉清单
  -> HTML/CSS 时间轴
  -> Chromium 截图
```

排版由 HTML/CSS 完成，Pillow 不参与日历布局。

## 文件

- `plugins/endfield/assets/calendar/current.json`
  - AkeData 降级版的版本窗口、栏目、泳道、展示文案和 ID 映射。
- `plugins/endfield/calendar/official.py`
  - 从官网当前构建自动发现三个简中日历资源。
  - 限定 `web.hycdn.cn` 官方 CDN，并校验类型、尺寸和体积。
- `plugins/endfield/calendar/official_draw.py`
  - 按官网原始比例拼接标题、日期轴和正文。
  - 不在仓库内保存官方整图。
- `plugins/endfield/calendar/akedata.py`
  - 校验 AkeData 降级清单。
  - 加载 AkeData 当前版本及数据表。
  - 用 AkeData 的时间、卡池名和素材 ID 水合条目。
- `plugins/endfield/calendar/draw.py`
  - 把时间换算为横向百分比。
  - 生成栏目、日期轴、活动条和页头页尾 HTML。
  - 下载 AkeData 素材并交给 Chromium 截图。
- `scripts/render_endfield_version_calendar.py`
  - 本地预览入口。

## 官方日历发现

官网首页的版本日历由三个带构建哈希的资源组成：

- `calendar.title`
- `calendar.timeline`
- `calendar.content`

资源文件名随官网发布自动变化，因此程序不保存固定 URL。每次发现时先从首页取得当前 `layout-*.js`，再选择带 `zh-cn` 语言标记的资源映射。CDN 文件带长缓存，渲染结果也按三个 URL 的摘要缓存。

如果官网调整前端结构、返回非图片内容或资源尺寸明显异常，本次发现会失败并使用 AkeData 降级版，不会把错误页面作为图片发送。

## AkeData 降级数据源

当前使用这些表：

| 表 | 用途 |
| --- | --- |
| `ActivityTable` | 活动对应的时间 ID 和活动页签素材 |
| `TimeRangeTable` | 活动、签到和卡池的开放/结束时间 |
| `GachaCharPoolTable` | 角色寻访名和 UP 角色 ID |
| `GachaWeaponPoolTable` | 武器申领名和 UP 武器 ID |
| `I18nTextTable_CN` | 文本 ID 对应的简体中文 |

素材使用 AkeData 的公开资源目录：

```text
https://data.akedata.wiki/public/images/assets/beyond/
dynamicassets/gameplay/ui/sprites/
```

常用目录包括：

- `charremoteicon/icon_{char_id}.png`
- `itemiconbig/{weapon_or_item_id}.png`
- `activity/{activity_tab_image}.png`

## 为什么仍需 current.json

AkeData 提供游戏配置事实，但不会提供官方宣传图的视觉编排，也可能在下半卡池尚未进入当前热更新时暂时缺少对应行。因此本地清单作为官网链路的降级方案，承担：

- 栏目和泳道；
- 宣传展示名；
- AkeData ID 映射；
- 未发布配置的时间和素材路径兜底；
- 颜色风格。

一旦 AkeData 出现对应记录，运行时会优先使用其时间、卡池名与素材。AkeData 暂时不可用时，仍可用清单安全生成日历。

## 更新到下一个版本

正常情况下无需为官方图片修改代码或 URL；官网更新首页版本日历后，下一次缓存刷新会自动发现新资源。

只需同时维护降级版：

1. 修改 `current.json` 的版本号、版本名和起止时间。
2. 按官方内容增删条目，并填入 AkeData 的 `kind`、`id`、`time_id`。
3. 只在 AkeData 尚未发布时填写 `start_at`、`end_at` 和 `art` 兜底。
4. 调整栏目 `rows` 和条目 `lane`，不需要修改渲染器。
5. 生成预览：

   ```powershell
   .\.venv\Scripts\python.exe scripts\render_endfield_version_calendar.py
   ```

   加 `--generated` 可单独检查 AkeData 降级版。

6. 检查 `.runtime/endfield_calendar/version_calendar.png`，然后运行 Endfield 测试。

日期轴按真实时间计算，所以版本跨度变化不会要求手工移动每一根活动条。
