# 合并 PR #7：接收签到卡片，帮助卡片改用新框架

[PR #7](https://github.com/otae-1204/otae-bot-entari/pull/7)（`fix(endfield): 优化帮助与签到卡片并补全签到奖励名称`）
包含两件互不相关的改动。本文档说明怎样合并它：签到部分原样接收，帮助部分由
[帮助图片生成框架](help_cards.md) 替代。合并后 GitHub 会把 PR #7 标记为已合并，作者署名保留。

## PR #7 的内容

PR 基于 `53a80e9`，只有两个提交，边界清楚：

| 提交 | 内容 | 处理 |
| --- | --- | --- |
| `8f277ba` 重排帮助卡片分类并修复文本超框 | `scripts/render_endfield_help.py`、`scripts/help_pages.json`（仅 `endfield` 页）、`assets/image/help/endfield.png`、`docs/endfield_help_card.md`、`tests/test_endfield.py` 的帮助图测试 | 不接收，由新框架替代 |
| `8352b93` 统一签到卡片配色并补全奖励名称 | `plugins/endfield/account/client.py`、`plugins/endfield/rendering/cards.py`、`scripts/render_endfield_attendance_preview.py`、`tests/fixtures/endfield_attendance_rewards.json`、`tests/test_endfield_account.py`、`docs/endfield_attendance_card.md` | 原样接收 |

## PR #7 帮助功能与替代方案对照

| PR #7 的做法 | 新框架中的对应 |
| --- | --- |
| `render_endfield_help.py` 只渲染终末地一页，采用独立的深灰加黄绿配色、1200px 宽 | `scripts/render_help_cards.py` 渲染所有帮助页，沿用原有蓝色标题条和磨砂面板，新增插画立绘窗，宽 1325px |
| spec 使用 `groups` + `sections`，条目为 `command` / `description` / `badge`，分区带 `access` 和 `notes`，页面带 `subtitle`、`legend` | 保留结构化条目和 `badge`、`access`、`notes`、`subtitle`；`groups` 改为 `columns`（左栏为账号相关，右栏为资料与工具）；`legend` 并入 `footnote`。解析器也接受 `command` / `description` 键名 |
| 浏览器内检查文字超框、分区重叠、页脚位置、字体加载、外部请求；`--stress` 测长内容 | 保留同类检查（`LAYOUT_CHECK` / `layout_problems`），另加标题与装饰碰撞、插画加载、高度上限检查；`--stress` 对每一页都测 |
| `--write-asset`：校验全部通过才覆盖正式图片；输出 `output/endfield-help/` | `--write`，规则相同；输出到 `output/help-cards/`（HTML、PNG、`validation.json`） |
| 测试：结构化断言关键命令、仅私聊命令带标签、分区数量 | 保留并扩展（`tests/test_endfield.py`）；新增 `tests/test_help_cards.py`，用 PNG 内的 spec 哈希检测图片是否过期 |
| `docs/endfield_help_card.md` | `docs/help_cards.md` |
| 600px 窄版布局检查 | 不需要：帮助图固定宽度，不存在窄屏排版 |

### 文案移植

PR #7 整理的终末地帮助内容（按账号 / 资料分区、权限标签、分区备注）已经移植到
`scripts/help_pages.json` 的 `endfield` 页，并做了以下调整，合并后请作者确认：

- 说明文字缩短为一行左右的短句，和其他帮助页的风格保持一致；去掉了“例：/ef 佩丽卡”这类示例。
- `/ef 添加账号` 单独列一行并标“仅私聊”；`/ef 装备 主力量 副敏捷` 移到“资料检索”。
- “群内统计”放在左栏末尾，以平衡两栏高度。
- 新增“图鉴与档案”分区（物品、道具、敌人、词条、档案条目），这是 PR #7 之后本地新加的命令。
- 标题沿用“终末地功能说明”；`/ef 与 /zmd 等价` 作为副标题。

## 前置条件

1. 帮助框架及相关改动已提交到本地 `main`：
   - `otae_bot/help_images.py`、`otae_bot/infrastructure/rendering/help_cards.py`
   - `scripts/render_help_cards.py`、`scripts/help_pages.json`
   - `assets/image/help/`（全部主图、`variants/`、`art/`）
   - `plugins/help_plugin/handlers.py`、`plugins/endfield/handlers.py`（`_finish_endfield_help` 改为随机挑图）
   - `tests/test_help_cards.py`、`tests/test_endfield.py`
   - `docs/help_cards.md`、本文档
2. 工作区里不能有 PR #7 会碰到的未提交改动，尤其是 `plugins/endfield/account/client.py`、
   `plugins/endfield/rendering/cards.py`、`tests/test_endfield_account.py`，否则 `git merge` 会拒绝执行。
   先提交或 `git stash`。
3. 本地未提交的 `scripts/render_endfield_help.py` 已删除（它是新框架之前的过渡版本）。

## 合并步骤

以下命令在项目根目录的 PowerShell 中执行。`origin` 指向 `otae-1204/otae-bot-entari`。

```powershell
git switch main
git pull origin main
git fetch origin pull/7/head:pr-7
git switch -c merge/pr-7
git merge --no-ff --no-commit pr-7
```

预期只有下面三个冲突，其余文件都会自动合并：

```text
CONFLICT (content): Merge conflict in assets/image/help/endfield.png
CONFLICT (content): Merge conflict in scripts/help_pages.json
CONFLICT (content): Merge conflict in tests/test_endfield.py
```

帮助相关文件一律保留当前分支的版本，PR 新增的帮助脚本和文档删除：

```powershell
git checkout HEAD -- assets/image/help/endfield.png scripts/help_pages.json tests/test_endfield.py
git rm -f scripts/render_endfield_help.py docs/endfield_help_card.md
git diff --cached --stat HEAD
```

最后一条命令应当只列出签到部分的 6 个文件，并且和 `8352b93` 的改动量一致：

```text
 docs/endfield_attendance_card.md                |  90 +++++++++
 plugins/endfield/account/client.py              |  99 +++++----
 plugins/endfield/rendering/cards.py             |  36 +++-
 scripts/render_endfield_attendance_preview.py   | 168 ++++++++++++++++
 tests/fixtures/endfield_attendance_rewards.json | 255 ++++++++++++++++++++++++
 tests/test_endfield_account.py                  |  97 ++++++++-
 6 files changed, 700 insertions(+), 45 deletions(-)
```

如果本地已经提交过其他 `client.py` 改动，这里的数字会不同。只要列表里没有帮助相关文件即可；
`client.py` 若出现冲突，两边的改动都要保留（本地改动在文件开头的导入和 `EndfieldOfficialClient`
前段，PR 改动在签到奖励名称表和 `_attendance_*` 函数，互不重叠）。

## 验证

```powershell
.venv/Scripts/python.exe scripts/render_help_cards.py --check
.venv/Scripts/python.exe -m pytest tests/test_help_cards.py tests/test_help_plugin.py tests/test_endfield.py tests/test_endfield_account.py tests/test_endfield_challenge.py -q
.venv/Scripts/python.exe scripts/render_endfield_attendance_preview.py --stress
```

- `--check` 应输出 `all N help images are up to date`。
- 签到预览脚本会把结果写到 `output/attendance-card/`，打开 PNG 目视确认配色和奖励图标。

以上都通过后提交并推送：

```powershell
git commit -m "Merge PR #7: 接收签到卡片改动，帮助卡片由 help_cards 框架替代"
git switch main
git merge --ff-only merge/pr-7
git push origin main
git branch -d merge/pr-7 pr-7
```

推送后，PR #7 的最后一个提交 `8352b93` 已经包含在 `main` 里，GitHub 会自动把 PR 标记为已合并。

## 给 PR 作者的说明（可直接贴到 PR #7）

> 感谢整理！签到卡片和奖励名称的改动已原样合并。帮助卡片这边，我们把帮助图统一改成了一个可配置的框架
> （`scripts/render_help_cards.py` + `scripts/help_pages.json` + `assets/image/help/art/gallery.json`），
> 所有帮助页共用原有的蓝色主题并加入插画立绘窗，所以没有采用这个 PR 里的新配色和
> `render_endfield_help.py`。你整理的分区、权限标签和备注已经移植到新 spec 的 `endfield` 页，
> 说明文字做了缩短；浏览器排版校验和 `--stress` 思路也保留了下来。维护说明见 `docs/help_cards.md`。

## 备选：只摘签到提交

如果不需要 GitHub 显示“已合并”，可以只摘取签到提交，然后手动关闭 PR #7：

```powershell
git fetch origin pull/7/head:pr-7
git cherry-pick 8352b93
```

## 与其他未合并 PR 的关系

[PR #9](https://github.com/otae-1204/otae-bot-entari/pull/9) 和
[PR #13](https://github.com/otae-1204/otae-bot-entari/pull/13) 也修改 `plugins/endfield/rendering/cards.py`，
但改的是蚀刻章和档案卡片，与签到卡片的 CSS 不在同一处。合并 PR #7 后再合这两个时，
如有冲突按各自区域分别保留，并重新运行上面的测试。
