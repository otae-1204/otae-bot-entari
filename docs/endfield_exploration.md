# 终末地地区探索查询

更新日期：2026-10-07（Asia/Shanghai）。

## 使用方式

先私聊 `/ef 绑定` 添加游戏账号，再发送：

```text
/zmd 探索
/zmd 探索 2
/ef 探索 昵称
/ef 地区探索 UID后四位
/ef 账号 探索 2
```

`/zmd`、`/ef`、`/endfield`、`/终末地` 共用现有入口。子命令支持 `探索`、`地区探索`、`探索统计`、`explore` 和 `exploration`。省略账号时使用当前用户的主账号；未设主账号时沿用账号存储层的首个账号回退。编号、昵称及 UID 后四位的解析也复用现有规则。

一次查询展示所选账号返回的全部大地区和小地区。“全部地区”以森空岛实际返回的数据为准，未公开、未解锁或尚未同步的内容无法补推。命令只查询发送者自己的绑定账号。群聊中 UID 脱敏，私聊中显示所选账号的完整 UID。

## 图片预览

下图使用截图中可见的 13 个地区作为本地样例，已标注“非实时查询”，不代表任何账号的当前进度。

![地区探索收集进度](images/endfield-exploration/exploration.png)

未完成、未知总量和异常数量的展示见[边界数据预览](images/endfield-exploration/partial.png)。

## 数据与字段

复用 `EndfieldOfficialClient.card_detail()`：

```text
GET /api/v1/game/endfield/card/detail?roleId=<角色>&serverId=<区服>
data.detail.domain[]
  domainId / name
  levels[]
    levelId / name
    <收集字段>: {count, total}
  collections[]
    levelId
    <收集字段>: 已收集数
```

接口鉴权、签名刷新和服务地址选择由现有客户端处理。每次查询获取一次当前档案，不按地区逐条请求，不写死地区名或地图数量。`levels[]` 提供名称、已收集数和总量；`collections[]` 仅在对应已收集字段缺失时，按 `levelId` 补充数量，不补造总量。

六列固定按用户指定顺序排列：

| 列 | API 字段 | 显示名称 |
| --- | --- | --- |
| 1 | `trchestCount` | 储藏箱 |
| 2 | `puzzleCount` | 醚质 |
| 3 | `blackboxCount` | 工业点数 |
| 4 | `pieceCount` | 维修灵感点 |
| 5 | `equipTrchestCount` | 装备模板箱 |
| 6 | `trstarCount` | 塔晶 |

既有响应结构见 [森空岛 UI 数据清单](skland_endfield_ui_data_inventory.md#49-地区建设与探索-domain)。其中早期文档只记录了前五类字段。第六类 `trstarCount` 的结构另与公开适配器的 [EndfieldDetail 类型定义](https://github.com/wahaha216/koishi-plugin-skland/blob/b25b595abd3d7dd74b1d7e8f22fb89b78e33c81a/src/types/endfieldDetail.ts)交叉核对；该链接是第三方适配器源码，并非森空岛官方接口文档。列名及顺序按本次用户提供的森空岛截图和说明确定，没有复制该适配器的实现。

数据边界：

- 大数字为已收集数，小数字为总量。`0/N` 表示尚未收集；只有总量明确为 0，且已收集数为 0 或未提供时，显示 `—`。
- 字段缺失显示 `?`。例如只有已收集数时显示 `5/?`，不会把缺失字段当作没有这种收集物，也不会写成已完成。
- 非负整数及对应数字字符串可解析；负数、布尔值、非整数和异常结构保留为未知。
- 已收集数大于总量时保留原值并标注 `!`，不截断数值或改成 100%。
- 大地区（`domain`）和小地区（`level`）沿用接口顺序。同一大地区内重复的非空 `levelId` 保留首条并提示；不同大地区允许出现相同 `levelId`。
- 只存在于 `collections[]` 的小地区仍会展示，名称缺失时用 `levelId` 标识，总量显示未知。空的大地区保留提示，不从结果中静默删除。
- 档案更新时间取 `base.saveTime`，按北京时间显示。`currentTs` 是请求时刻，不用于冒充游戏同步时间；缺失时明确标注未提供。

## 实现位置与渲染

| 文件 | 职责 |
| --- | --- |
| `plugins/endfield/account/exploration/models.py` | 六列定义、进度状态和地区视图模型 |
| `plugins/endfield/account/exploration/service.py` | 纯数据解析，处理缺失、兼容字段、重复区域和更新时间 |
| `plugins/endfield/account/exploration/draw.py` | 地区表格、状态标记、HTML 和分页图片 |
| `plugins/endfield/account/exploration/thumbnails.py` | 公开地图名称／ID 匹配、图块选择与缺图降级 |
| `plugins/endfield/account/exploration/artwork.py` | 森空岛原始图标的本地读取与内嵌 |
| `plugins/endfield/account/exploration/version.py` | 读取游戏版本标签，失败时返回未知 |
| `assets/image/endfield/exploration/` | 立体地区图标、六类列头图标、来源清单 |
| `plugins/endfield/catalog/commands.py` | 子命令、别名及文字帮助 |
| `plugins/endfield/handlers.py` | 当前用户账号选择、取数、图片缓存及发送 |
| `scripts/help_pages.json` | 帮助卡片中的探索入口 |
| `scripts/preview_endfield_exploration.py` | 文件样例、缩略图获取及布局检查 |

视图解析不访问网络、不注册机器人事件。命令复用 `_card_detail_with_snapshot()` 和角色任务锁，因此同角色并发请求及接口失败沿用账号查询现有处理。该调用也沿用已有的持有率快照更新行为，没有新增探索数据库。

出图保留浅灰十字网格、深色标题和红色强调。所有小地区按接口顺序连成一张表，去掉大地区分组标题及序号，在每个地区名称下方用灰色小字标注所属大地区，如“武陵”“四号谷地”。顶部只统计小地区数量，如“13 个地区”。表格没有底色、外框或竖线，各行用横线分隔，背景网格可透过表格显示；六类列头图标位于文字上方，每页只出现一次。

图片逻辑宽度为 1040 px，地区列占 31%，六类统计均分剩余宽度。地区图标为 72 × 72 px，基础行高为 116 px，长名称换行时自动增高；地区名称、已收集数和六类收集物列头文字分别为 24、31、20 px。“地区”列头为 25 px，名称下方的所属大地区与统计总量均为 20 px。复用 `_draw_gallery_catalog()`、共享 Playwright 浏览器和 PNG 优化器，按 1.25 倍生成，实际宽度为 1300 px。

标题右侧的 `COLLECTION INDEX` 后显示游戏版本，如 `V1.5`。版本来自项目既有的 [AKEData manifest](https://data.akedata.wiki/manifest.json)，通过 `game_version_label()` 提取主、次版本号；该标签表示数据站收录的当前游戏版本，不表示账号档案已同步到该版本。版本查询最多等待 5 秒，失败或返回无效标签时显示“版本未知”，收集统计仍可出图。

每页最多 24 行地区明细，空的大地区占一行提示。超过时继续分页，保留全部行，每页重复表头并标注页码；若浏览器报告超高，则按 12、6、1 行依次缩小分页。其他渲染错误正常上抛，不伪装成成功或返回截断图片。大地区归属仍保存在内部模型中，用于图标匹配和去重。

每次命令先确认账号并读取当前数据，再使用 `_render_account_pages()` 缓存图片。缓存键包含账号、区服、群聊／私聊状态和视图内容摘要；数量或版本标签变化后重新绘图，群聊不会复用包含完整 UID 的私聊图片。用户和接口返回的文本均经过 HTML 转义。

## 地区缩略图

每个小地区名称前优先展示森空岛同款立体图标。资源来自官方新版[地区探索组件](https://assets.skland.com/_static_assets/game-tools/RegionExploreTable-D36RE3Dz.js)：多数 PNG 以内嵌 Base64 保存，雪松林和遂明使用独立图片地址。组件共映射 17 个区域，首墩与首墩内部共用一张图。六类列头图标也从同一组件提取，并按其表头与统计字段逐列核对。

原始图片已存入 `assets/image/endfield/exploration/`。`manifest.json` 记录来源、地区映射和 SHA-256；地区名称及所属关系由[森空岛公开地图树](https://zonai.skland.com/web/v1/game/endfield/map/tree)补充。已收录区域无需联网下载素材，渲染时直接内嵌本地 PNG。具体来源和维护方式见[素材说明](../assets/image/endfield/exploration/README.md)。

匹配先确认大地区，再查找其下的小地区。两层均优先使用精确 ID，找不到 ID 时按完整名称匹配；重名或跨地区的候选不猜测。素材请求只访问公开地图树、索引及图片地址，不携带账号凭据、UID 或收集统计。

未收录的新区域尝试平面地图回退，图块由 [AKEData 素材索引](https://data.akedata.wiki/asset-sync-index.json)提供。选取 `levelmapchunks` 下的低分辨率 `l_` 资源，优先使用 `sprites`，缺少完整矩形网格时尝试 `textures`。两类资源独立校验，不混合拼接；每个区域最多使用 64 块。图块按横向坐标和自下而上的纵向坐标排列，整张网格等比缩放到名称前的方框中。回退只处理缺少立体图标的区域，保留已有官方图标。

公开地图树和素材索引的缓存有效期设为 1 小时；图片复用共享素材缓存、下载预算和浏览器资源注入。索引缺失、地区未匹配或下载缺图时显示占位，名称和六列统计仍正常展示。元数据请求失败或图片下载不全时，渲染健康检查会阻止该图片进入完整结果缓存，使下次查询能够重试。

## 单元测试与样例预览

在项目根目录运行：

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_endfield_exploration.py tests/test_endfield_daily.py tests/test_endfield_account.py tests/test_endfield_account_ui.py tests/test_architecture.py tests/test_group_features.py tests/test_help_cards.py tests/test_help_plugin.py
.\.venv\Scripts\python.exe scripts/preview_endfield_exploration.py --stress
```

新增测试覆盖命令别名、主账号和指定账号、群聊脱敏、完整命令分发、单次档案请求、接口错误、未绑定与无效账号、六列对应关系、新地区、缺失／零值／异常计数、旧字段按 ID 合并、重复区域、北京时间、HTML 转义、分页完整性和图片缓存隔离。版本测试另覆盖标签提取、查询失败，以及版本变化后重新出图。

缩略图测试另覆盖大地区和小地区的精确匹配、重名歧义、图块坐标、两类素材选择、不完整网格、路径限制、名称前的图片布局，以及元数据异常、缺图和渲染健康状态。官方图标的测试验证原始文件哈希、六列表头、13 个参考区域的离线匹配，以及新增区域回退时保留已有立体图标。

预览默认读取 `tests/fixtures/endfield/exploration/screenshot.json`，其中只有用户截图可见的 13 个区域。地区／区域 ID 使用测试占位；它不是实时账号响应，也不代表游戏全部区域。预览图片显式标注“截图示例（非实时查询）”。

生成文件位于 `data/endfield/exploration/previews/`：

- `exploration.png`：截图中 13 个地区的连续列表，名称下方标注所属大地区。
- `partial.png`：未完成、缺失、无此类收集物及异常数量。
- `pagination-1.png` 至 `pagination-4.png`：73 个区域的分页压力样例。
- `empty.png`：空数据提示。
- 同名 HTML 与 `validation.json`：便于检查布局、行数和外部请求。

也可传入自行保存的脱敏详情文件：

```powershell
.\.venv\Scripts\python.exe scripts/preview_endfield_exploration.py --file data/endfield/exploration/research/detail-redacted.json
```

预览脚本不读取绑定凭据，不启动机器人，不发送消息；文件预览会覆盖昵称并省略 UID。统计值来自本地文件，版本标签通过公开 manifest 查询。已收录的立体图标和六类列头图标直接读取本地素材；未知地区才联网尝试平面地图回退。脚本使用正式解析和绘图函数，另用浏览器检查文字溢出、画布高度、破图及外部请求。导出的 HTML 内嵌全部已加载图片，打开时无需额外访问图片源。

帮助图片更新方式：

```powershell
.\.venv\Scripts\python.exe scripts/render_help_cards.py --page endfield --output-dir data/endfield/exploration/previews/help --write
```

## 本次验证与待验收项

2026-10-07，Windows / Python 3.13：截图样例的 13 个地区均成功匹配并加载官方立体图标，六类列头图标在表格顶部展示一次，版本标签为 `V1.5`。逻辑尺寸为 1040 × 1989.97 px，PNG 尺寸为 1300 × 2486 px。分页压力样例保留 73 行，分为 24、24、24、1 行。全部 7 张探索样例均无文字溢出、无破图，导出 HTML 打开时没有外部请求。布局记录保存在 `data/endfield/exploration/previews/validation.json`。

探索测试共 38 项，其中 15 项覆盖图标。上述相关回归结果为 **314 passed、116 subtests passed**，有一条第三方 `creart` 事件循环弃用警告；完整输出保存在 `data/endfield/exploration/validation/tests.txt`。探索模块、测试和预览脚本的 Ruff 检查、格式检查，以及 `git diff --check` 均通过。终末地帮助卡片已重新生成，尺寸为 1325 × 1147 px，通过布局与产物一致性检查。

提交前另执行了全量测试，结果为 **1983 passed、21 failed、2 skipped、460 subtests passed**。在同一 Python 环境、未修改的上游 `f6d1716` 独立工作区中，21 项失败全部复现：1 项 B 站文档图片像素比对、1 项 Grok 响应字节数断言、3 项 HTTP 代理测试，以及 16 项 Windows SQLite 临时文件清理测试。该环境的 httpx 版本为 0.24.0，代理测试涉及较新版本的参数和内部结构。此 PR 不修改这些模块；完整结果和基线复现分别保存在本地 `full-tests.txt`、`baseline-failures.txt`，与上述回归报告同目录。

本机绑定表为空，因此本次未验证实际账号的最新接口响应或 QQ 投递。部署后需要用已绑定账号执行 `/zmd 探索`，核对全部地区名称、六类计数与森空岛当前页面是否一致，并确认群聊、私聊和指定账号的图片发送。亚服沿用现有客户端路径，但本次没有亚服真实响应样本，不能将离线测试视为该区服的线上验收。
