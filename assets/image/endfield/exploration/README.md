# 森空岛地区探索图标

2026-10-07 从[森空岛地区探索组件](https://assets.skland.com/_static_assets/game-tools/RegionExploreTable-D36RE3Dz.js)提取。入口为[终末地游戏数据页](https://game.skland.com/endfield/game-data?header=0)，页面的动态依赖表指向该组件。

`collections/` 保存组件内嵌的六个 48 × 48 px PNG；`regions/` 保存 16 个地区图标，覆盖组件映射中的 17 个区域。首墩与首墩内部按官方映射共用一张图。雪松林和遂明通过组件给出的独立 PNG 地址下载，其余图片直接解码组件中的 Base64，均保留原始字节，没有从截图裁剪或重绘。

| 本地收集字段 | 图标含义 | 组件中的内嵌图片序号（从 0 开始） |
| --- | --- | --- |
| `trchestCount` | 储藏箱 | 4 |
| `puzzleCount` | 醚质 | 3 |
| `blackboxCount` | 工业点数 | 0 |
| `pieceCount` | 维修灵感点 | 2 |
| `equipTrchestCount` | 装备模板箱 | 1 |
| `trstarCount` | 塔晶 | 5 |

以上顺序依据组件的表头和数据单元格逐列核对。地区 ID 与图像的对应关系来自组件中的映射；中文名称及所属大地区由[森空岛公开地图树](https://zonai.skland.com/web/v1/game/endfield/map/tree)补充，用于兼容名称匹配。

`manifest.json` 记录组件地址、采集日期、文件 SHA-256、每张图的来源，以及地区映射。`embedded_image_index` 指按组件中 `data:image/png;base64,` 的出现顺序编号。后续更新时需从页面依赖表确认新组件地址，再核对映射和列序，不能直接套用旧序号。

这些图标的权利归原权利方所有。本地保存用于机器人出图，运行时直接内嵌，不依赖官方脚本地址持续可用。未收录的新地区继续使用公开平面地图资源回退；收集统计始终来自账号接口。
