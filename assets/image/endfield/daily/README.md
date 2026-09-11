# 日常仪表盘图标

活跃度、每周事务、通行证直接裁自用户提供的森空岛截图
`SeaTalk_IMG_20260911_110604.jpg`（1290×867，2026-09-11）。
使用原图像素，不重绘；裁为 PNG 后统一放入 44×44px 图标槽。
CSS 灰度、轻微对比度和正片叠底去除截图近白底的方块边界。

对应关系按用户最初前两张截图：活跃度为 ENDFIELD 标识、每周事务为斜线三角、
通行证为 P.P. 徽记。第三张截图的后两项图标位置相反，仅从中裁取更大的图标区域。

理智背景仍使用 AKEData 的游戏 UI 提取素材，2026-09-11 下载，原始 PNG 未修改。
该游戏素材版权归原权利方，AKEData 为资源来源。
所有图标读取本地文件并内嵌为 data URL，不依赖在线素材请求。

源目录：`https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/`

| 本地文件 | 来源 / 裁剪矩形 (x, y, width, height) | 用途 |
|---|---|---|
| sanity.png | itemiconbig/item_ap.png | 理智背景闪电 |
| activity.png | 用户截图 (92, 268, 108, 96) | 活跃度 |
| weekly.png | 用户截图 (92, 674, 132, 104) | 每周事务 |
| pass.png | 用户截图 (88, 472, 116, 99) | 通行证等级 |

不因进度值或完成状态改变图标颜色。
