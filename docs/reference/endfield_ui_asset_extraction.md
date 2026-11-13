# 终末地 UI 资源学习提取流程

本流程只读取本机已安装的游戏文件，把选定的 Unity UI `Texture2D` 导出为 PNG。请仅用于个人学习、研究和互操作测试；资源版权仍归原权利人所有，不要公开分发导出的素材。

## 已验证环境

- 游戏目录：`D:\Hypergryph Launcher\games\Endfield Game`
- 游戏资源版本：2026-07-16 本机当前 VFS，`Bundle` / `InitBundle` 版本 3
- .NET SDK 9
- Python 3.11+
- [Endfield-Studio](https://github.com/microruri/Endfield-Studio) 提交 `4dede2ebc40ffce87d89434159b0b6cefa28cab9`
- [AnimeStudio](https://github.com/Escartem/AnimeStudio) 的 `akef_temp` 分支，提交 `e69f93d7d13e78947cfab9f252701e37c445eb85`

当前正式服 `.blc` 比 Endfield-Studio 上述提交多了几个保留字段。本项目中的兼容补丁同时为 manifest 增加 CSV 映射输出。

## 1. 准备两个解析工具

在项目根目录执行：

```powershell
New-Item -ItemType Directory -Force .runtime | Out-Null
git clone https://github.com/microruri/Endfield-Studio.git .runtime/Endfield-Studio-src
git -C .runtime/Endfield-Studio-src checkout 4dede2ebc40ffce87d89434159b0b6cefa28cab9
git -C .runtime/Endfield-Studio-src apply ../../scripts/endfield/patches/endfield-studio-live-v3.patch

git clone --branch akef_temp https://github.com/Escartem/AnimeStudio.git .runtime/AnimeStudio-akef
git -C .runtime/AnimeStudio-akef checkout e69f93d7d13e78947cfab9f252701e37c445eb85
```

## 2. 生成 VFS 和资源路径索引

```powershell
$game = 'D:\Hypergryph Launcher\games\Endfield Game'
$meta = '.runtime\endfield-extract'
$endfieldCli = '.runtime\Endfield-Studio-src\src\Endfield.Cli\Endfield.Cli.csproj'

dotnet run --project $endfieldCli -c Release -- -g $game -t blc-all -o $meta
dotnet run --project $endfieldCli -c Release -- -g $game -t manifest-assets-yaml -o "$meta\bundle_manifest_assets.yaml"
```

后续脚本使用：

- `$meta\blc_groups\Bundle.json` 和 `InitBundle.json`：Bundle 在 `.chk` 中的位置、长度、校验值和解密参数。
- `$meta\bundle_manifest_assets.csv`：语义资源路径到 Bundle 文件名的映射。

## 3. 只提取目标 UI Bundle

```powershell
python scripts/endfield/extract_endfield_ui_bundles.py `
  --game-root $game `
  --bundle-json "$meta\blc_groups\Bundle.json" `
  --bundle-json "$meta\blc_groups\InitBundle.json" `
  --manifest-csv "$meta\bundle_manifest_assets.csv" `
  --output '.runtime\endfield-ui-study-pack'
```

默认范围包括物品、角色、技能、状态、怪物、头像框、成就等小图标，以及名片和部分主题背景。脚本优先读取 `Persistent`（在线更新），找不到时回退到 `StreamingAssets`；每个 Bundle 都会按 manifest 的 MD5 验证。

先预估数量和空间可加 `--dry-run`。也可以用一个或多个 `--pattern` 替换默认范围，例如只提取名片：

```powershell
python scripts/endfield/extract_endfield_ui_bundles.py `
  --game-root $game `
  --bundle-json "$meta\blc_groups\Bundle.json" `
  --bundle-json "$meta\blc_groups\InitBundle.json" `
  --manifest-csv "$meta\bundle_manifest_assets.csv" `
  --output '.runtime\endfield-business-cards' `
  --pattern 'business_card'
```

要提取完整 UI icon 集（包括名称中带 `icon` 的全部纹理、全局 `btn_*`/`mark_*`/`logo_*`/`badge_*`/`symbol_*` 等图标式命名，以及好友名片、主线和 HUD 中使用 `deco_*` 命名的纹理），使用：

```powershell
python scripts/endfield/extract_endfield_ui_bundles.py `
  --game-root $game `
  --bundle-json "$meta\blc_groups\Bundle.json" `
  --bundle-json "$meta\blc_groups\InitBundle.json" `
  --manifest-csv "$meta\bundle_manifest_assets.csv" `
  --output '.runtime\endfield-all-icons' `
  --preset all-icons
```

`all-icons` 会完整匹配名称含 `icon` 的目录内文件，而不只匹配文件名；同时完整纳入好友、任务、主 HUD、潜能和合约 UI 族。2026-07-16 当前版本已验证选择 9,997 条资源、提取 2,424 个 Bundle、导出 10,596 张游戏 PNG，manifest 目标缺失 0。

## 4. 导出 PNG

```powershell
./scripts/endfield/export_endfield_ui_images.ps1 `
  -RawRoot '.runtime\endfield-ui-study-pack\raw' `
  -OutputRoot '.runtime\endfield-ui-study-pack\png'
```

导出脚本会在首次运行时构建 AnimeStudio CLI，并使用 AssetBundle 容器信息恢复原目录。例如：

```text
png/assets/beyond/dynamicassets/gameplay/ui/sprites/charicon/icon_chr_0030_zhuangfy.png
png/assets/beyond/arts/ui/sprites/friend/businesscardbg/business_card_topic_chr_0009_azrila.png
```

完整图标集可生成可搜索的离线目录和 CSV 清单：

```powershell
python scripts/endfield/build_endfield_icon_catalog.py `
  --input '.runtime\endfield-all-icons\png' `
  --output '.runtime\endfield-all-icons\icon_catalog.html'
```

游戏更新后，删除旧的 `endfield-extract` 元数据和输出目录，再从第 2 步执行。若 `.blc` 布局再次改变，补丁可能需要同步调整；不要在校验失败时继续使用输出。
