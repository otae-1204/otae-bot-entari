# B 站视频与直播卡片

渲染入口为 `plugins/bilibilibot/draw.py`，订阅推送与群消息链接预览共用同一套布局。

![樱花粉视频与直播卡片](images/bilibili-cards-sakura.png)

上图使用用户指定的公开直播间和视频封面；开播、下播两张为样式演示，不代表当前直播状态。

## 展示规则

- 顶部左侧使用 44 px 中文状态与 64 px 图标，先识别类型，再阅读主播和封面。
- 视频与开播采用明亮的樱花粉白色（主色 `#D46F94`，开播底色 `#FFE7F1`），下播与未开播采用浅灰紫白色。图标与文字同时表达状态，灰度显示也可区分。
- 封面保留原始比例和全部边缘，不叠加文字、状态或圆角遮罩。常规横图按内容区宽度显示；竖图、方图和极宽图片等比缩放，必要时补柔和底色。封面显示区域高度限制为 240–640 px。
- 封面下载失败或损坏时显示“封面暂不可用”；头像失败时显示名字首字。
- 标题最多 3 行，视频简介最多 3 行，动态正文最多 5 行。按像素宽度换行和省略，底部信息随内容向下排版。
- 优先使用仓库中的 MiSans 字体，避免依赖 Windows 系统字体。

| 数据类型 | 顶部标识 | 图标 | 来源 |
| --- | --- | --- | --- |
| `video` | 视频 | 播放三角 | 视频订阅、视频链接预览 |
| `live_on` | 已开播 | 声波条 | 开播订阅通知、正在直播的房间预览 |
| `live_off` | 已下播 | 停止方块 | 订阅轮询检测到开播→未开播 |
| `live_idle` | 未开播 | 暂停双线 | 未开播房间的链接预览，包括平台轮播状态 |
| `dynamic` | 动态 | 对话框 | 动态订阅 |

视觉状态由 `card_type` 决定，不依赖旧英文 `badge`。未开播房间的预览不再暗示刚刚下播；订阅轮询仍按直播状态变化发送通知。

## 本地预览

在项目根目录运行，不启动 bot、不访问 B 站、不发送群消息：

```powershell
.\.venv\Scripts\python.exe scripts/preview_bilibili_cards.py
```

输出到 `output/bilibili-cards/`（不纳入 Git）：

- `comparison.png`：同一主播和封面的 视频 / 已开播 / 已下播 对照。
- `chat-size.png`：每张缩小到 300 px 宽的群聊尺寸示意。
- `grayscale.png`：灰度对照。
- `edge-cases.png`：竖图、方图、超宽图、坏图，以及长标题和长昵称。
- 各类型的独立原尺寸 PNG。

默认封面和资料均为脚本生成的示例，可传入 `--cover "C:\path\cover.jpg"` 检查本地真实封面。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_bilibili_cards.py tests/test_core_logic.py tests/test_runtime_optimization.py tests/test_endfield_performance.py -k "bili or Bilibili" -q
```

本次 58 项相关测试通过，覆盖封面四角保留、比例、EXIF 旋转、透明图片、极端比例、状态可辨识、长文本不重叠、失败兜底、订阅状态转换、资源并发与既有 B 站逻辑。已人工检查离线渲染图，并通过真实接口验证以下链接的解析、封面/头像下载和 PNG 生成：

- [直播间 25731103](https://live.bilibili.com/25731103)：2026-09-22 17:05（北京时间）查询时未开播，生成“未开播”卡片。
- [视频 BV1extE6LEKB](https://www.bilibili.com/video/BV1extE6LEKB/)：完整保留 2400×1350 封面与长标题；隐藏接口简介中的占位符 `-`。

未进行真实 QQ 群消息联调。
