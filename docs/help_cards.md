# 帮助图片（/help）生成框架

`/help`、`/help <主题>`、`/ef help`、`/zmd help` 发送的图片全部由同一套框架生成，不再手工做图。
改文案、换插画、加主题都只需要改数据，然后重新出图。

| 位置 | 作用 |
| --- | --- |
| `scripts/help_pages.json` | 每个帮助页的标题、分栏、分区、命令和配图 |
| `assets/image/help/art/` | 插画原图和 `gallery.json`（插画登记表） |
| `otae_bot/infrastructure/rendering/help_cards.py` | 读取上面两份数据，生成 HTML、校验排版、写入 spec 哈希 |
| `scripts/render_help_cards.py` | 命令行：出图、校验、写入正式资源、检查是否过期 |
| `otae_bot/infrastructure/rendering/help_runtime.py` | 运行时按比例挑插画、出图并缓存到 `data/cache/help-cards/` |
| `otae_bot/help_images.py` | 比例与可见度的几何计算 |
| `assets/image/help/<file>.png` | 每页的静态回退图（无浏览器时使用） |

## 版式

蓝色标题条（可带副标题）+ 卡片。卡片背景是插画的模糊版本，左边是半透明磨砂面板放命令，
右边是清晰的立绘窗展示同一张插画。卡片宽度固定（1325px），高度随内容增长。
插画只做等比缩放和裁切，裁切时让 `focus` 指定的位置保持在画面内，不会拉伸变形。

主题参数（宽度、配色、透明度、模糊半径、字体等）集中在 `HelpTheme` 里。

## 插画怎么挑

发送时不再从预渲染好的图里随机，而是**按页面底板比例现挑现渲染**：

1. 算出该页底板容器（`.card`）和立绘窗（`.window`）的比例。底板比例由页面渲染高度决定，
   而版式与插画无关，所以直接用已提交主图的高度推算，不必先渲染。
2. 对 `gallery.json` 里每张插画算出它填进立绘窗后还剩多少可见面积（等比裁切，不拉伸）。
3. **立绘窗是唯一门槛**：可见面积不低于 `WINDOW_VISIBILITY_FLOOR`（默认 0.15）的插画进入候选池，
   发送时等概率随机取一张。裁切始终围绕插画的 `focus`，所以人物的脸一定在画面内。
4. 底板不参与筛选，只用于排序（匹配度高的排前面，保证同一页结果稳定）；
   万一整池都不过线就退回全部，保证总能发出图。

**所以 `gallery.json` 里的插画每页都会用到**，同一页每次发出来的人物都不一样，
横图竖图都会出现。之所以敢让竖图铺满宽底板，是因为底板是模糊的、还被磨砂面板压住，
裁得再狠也只是把模糊放大，观感不受影响；真正清晰、需要保证能看清人物的是右边的立绘窗。

## 渲染与缓存

挑中的插画第一次发送时现场渲染，结果连同 spec 摘要一起写进 `data/cache/help-cards/<页面>.<插画>.png`；
之后命中缓存直接发送。摘要对不上（改了文案、换了插画或主题参数）会自动重渲染。
渲染走 `screenshot_web_element`，字体以路由资源注入，页面不访问网络，出图与离线管线逐像素一致。
同一张图并发只渲染一次。缓存目录可用环境变量 `OTAE_HELP_CACHE_PATH` 覆盖。
渲染不可用时（没有浏览器）回退到仓库里的静态回退图。

## 添加插画

1. 把图片放进 `assets/image/help/art/`。建议长边不小于 1000px，画面里不要有文字或水印；
   竖图最适合立绘窗，横图也可以，会裁出人物部分。
2. 在 `assets/image/help/art/gallery.json` 的 `art` 里登记：

   ```json
   {"id": "summer", "file": "summer.jpg", "focus": [0.45, 0.25], "credit": "来源说明"}
   ```

   - `id`：只能用字母、数字、`-`、`_`，页面里用它引用插画。
   - `focus`：人物脸部在原图里的位置，按宽、高的比例写（0 到 1）。立绘窗和背景都围绕它裁切。
   - `backdrop_focus`（可选）：背景想用不同的裁切位置时再写，比如要避开角落里的水印或签名。
   - `credit`（可选）：来源，只用于维护，不会画到图上。
登记进 `gallery.json` 后，这张插画就会自动进入所有页面的候选池，无需改动页面配置，
也不必重新出图——发送时由运行时按页面比例挑选并渲染。

`gallery.json` 的 `default` 是页面没写 `art` 时用的插画。

## 页面配置

```json
{
  "id": "endfield",
  "file": "endfield.png",
  "title": "终末地功能说明",
  "subtitle": "/ef 与 /zmd 等价",
  "art": ["okhands"],
  "columns": [
    [
      {
        "heading": "账号管理",
        "access": "从绑定开始",
        "items": [
          {"cmd": "/ef 绑定", "desc": "按提示绑定森空岛", "badge": "仅私聊"},
          "/ef 账号 [编号]  干员配装与养成总览"
        ],
        "notes": ["[账号] 可填编号、昵称或 UID 后四位"]
      }
    ],
    [ ... 第二栏 ... ]
  ],
  "footnote": "<>必填，[]选填"
}
```

- `columns`：几个数组就是几栏，每栏里的分区从上到下排列。出图时会输出每栏高度，差太多就挪一个分区过去。
- 条目可以写成字符串 `"命令  说明"`（中间两个空格），也可以写成对象 `{"cmd", "desc", "badge"}`；
  对象也接受 `command` / `description` 这两个键名。
- `badge` 是命令后面的小标签，用来标注“仅私聊”“需绑定”“管理权限”等。`access` 是分区标题旁边的说明。
- `notes` 是分区末尾的备注，`footnote` 是面板右下角的注释，`subtitle` 显示在标题右侧。
- `art`：第一张插画生成静态回退图 `<file>`，写一张就够。实际发送的插画由下面的比例规则决定。

新增主题时，还要在 `plugins/help_plugin/handlers.py` 的 `TOPIC_MAP` 里登记别名到文件名的映射。

## 出图

在项目根目录运行（需要已安装 Playwright Chromium，或本机的 Chrome/Edge）：

```powershell
.venv/Scripts/python.exe scripts/render_help_cards.py --stress          # 全部页面出图到 data/help/previews 并校验
.venv/Scripts/python.exe scripts/render_help_cards.py --page endfield   # 只出某几页，可重复
.venv/Scripts/python.exe scripts/render_help_cards.py --write           # 校验全部通过后写入 assets/image/help
.venv/Scripts/python.exe scripts/render_help_cards.py --check           # 不开浏览器，只列出过期、缺失或多余的图片
```

`data/help/previews/` 保存每张图的 HTML、PNG 和 `validation.json`，这些预览和校验产物不纳入 Git；通过 `--write` 发布的正式图片仍位于 `assets/image/help/`。校验在浏览器实际排版后进行，检查：

- 文字是否超出所在面板或标题条；
- 标题和副标题是否撞上标题条右侧的圆点装饰；
- 面板注释是否压住正文；
- 字体、插画是否加载成功，页面是否试图访问网络；
- 宽度是否等于主题宽度、高度是否超过 `HelpTheme.max_height`。

`--stress` 会给每页额外加一个超长命令、超长说明和超长备注的分区再校验一遍，但不会写入正式资源。
任何一张图校验失败都不会写入 `assets/`。全量 `--write` 还会清理 `assets/image/help/variants/` 下的历史变体图。

## 过期检测

每张图的 PNG 元数据里写有 `otae-help-digest`，由页面配置、插画登记项、插画文件内容、
`HelpTheme` 参数和 `TEMPLATE_VERSION` 计算得出。`tests/test_help_cards.py` 会逐张比对：
只改了配置或插画却没重新出图时，测试会失败并提示重新运行 `--write`。
运行时缓存用同一个摘要判断是否需要重渲染。
修改 `help_cards.py` 里的 HTML/CSS 后，要把 `TEMPLATE_VERSION` 加一再出图。

## 修改帮助文案时

- 同步核对实际命令解析（如终末地的 `plugins/endfield/catalog/commands.py`）和文字版回退（`format_help()`）。
- `tests/test_endfield.py` 会检查终末地页包含关键命令，且仅私聊的命令都带有“仅私聊”标签。
