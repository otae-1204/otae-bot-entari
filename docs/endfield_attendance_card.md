# 终末地签到卡片

`/zmd 签到` 与 `/ef 签到` 使用 `plugins/endfield/rendering/cards.py` 的
`draw_attendance_card()`。保留一行一个账号的结构，以及
昵称、服务器、脱敏 UID、签到消息、奖励名称/数量、当月累签等原有信息。

配色参考档案卡：灰白底、深灰标题、白色账号行，仅标题下沿使用黄绿强调色
`#dfec32`。签到成功、已签到、失败统一使用灰阶，通过消息文字区分状态。
文本和奖励自动换行，累签区域在空间不足时换行；未增加汇总面板或其他数据模块。

奖励图标继续来自 `AttendanceRewardView.icon_url`，通过原有 `_image_data_urls()`
加载并内嵌。只等比显示真实原图，不生成图标、不改色；获取失败时只显示名称与数量。
账号选择和其他卡片样式保持原样。

## 森空岛奖励核对（2026-09-21）

只读请求 `GET https://zonai.skland.com/web/v1/game/endfield/attendance`，
返回当月 30 天日历、22 个奖励配置，按图标归为 8 类道具。22 个配置的 `name`
均为空，但 `id`、`count`、`icon` 均存在，8 张原图均能成功下载。
这能获得当前月历的完整奖励配置，并非森空岛历年或未来奖励总表。

使用 AKEData `1.5.3@10024360-6` 的
[ItemTable](https://data.akedata.wiki/public/1.5.3/10024360-6/TableCfg/ItemTable.json)、
[I18nTextTable_CN](https://data.akedata.wiki/public/1.5.3/10024360-6/TableCfg/I18nTextTable_CN.json)
核对名称，并人工比对其 `iconId` 原图与森空岛原图。

| 道具 | 当月配置数量 | ItemTable ID | 森空岛原图 |
| --- | --- | --- | --- |
| 中级作战记录 | 2、3、5 | `item_expcard_stage1_mid` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/921A397E2765462C009B939E0CD92606.png) |
| 初级认知载体 | 5 | `item_expcard_stage2_low` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/ed34d5a121dbc81da6f6991792ff97f6.png) |
| 高级作战记录 | 2、3、5 | `item_expcard_stage1_high` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/7f6ce8ae13546508255b89a4d8571fe3.png) |
| 武器检查装置 | 2、3、5 | `item_weapon_expcard_mid` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/0dea0bc0fd87138df322e8a254a6999f.png) |
| 武器检查套组 | 2、3、5 | `item_weapon_expcard_high` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/90403112f070c14859d107b530ae5851.png) |
| 协议棱柱 | 2、3、5 | `item_char_skill_level_1_6` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/0c1e8cf15711a7d59aa7d8786b533cf1.png) |
| 折金票 | 2000、3000 | `item_gold` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/2a58a0e85f39092433842ccd62324785.png) |
| 嵌晶玉 | 80、90、100、120 | `item_diamond` | [原图](https://bbs.hycdn.cn/asset/endfield_attendance/8ed434a6cdb173c96ed0572115112f93.png) |

公开响应样本保存在 `tests/fixtures/endfield_attendance_rewards.json`，仅保留奖励配置、
日历的 `awardId` 和经核对的预期名称，不含凭据、角色标识、个人签到状态。

### 名称显示与修正

原逻辑读取签到 POST 的 `awardIds`，合并 POST / 月历 GET 的 `resourceInfoMap`，
优先使用有效的 `name` / `itemName` / `resourceName`；缺名称时尝试 6 个已知
AKEData 道具 ID，最终降级为“签到奖励”。当前森空岛使用签到别名和哈希图标 URL，
无法匹配原有 6 个 ID，因此当前这批奖励都会丢失具体名称。

修正后的规则：

1. 本次奖励对象、POST 资源表、GET 月历资源表按此顺序补充信息；选择第一个有效名称。
   空名称、内部 ID、泛称“签到奖励”不会覆盖已有名称，月历数量也不会覆盖本次奖励数量。
2. 缺名称时，按已核实的游戏道具 ID、图标 ID 或森空岛图标文件标识补齐名称。
   8 类道具覆盖本次实查的全部 22 个配置，不依赖签到编号猜测名称。
3. 优先显示接口返回的真实图标 URL；缺 URL 时才使用已知 AKEData 图标原图。
   中级/高级作战记录的道具 ID 与 `iconId` 不同，已分别处理；兼容 `iconUrl`、
   `imageUrl` 等字段，前面的纯图标 ID 不再挡住后面的有效 URL。
4. 陌生奖励且无有效名称时仍显示“签到奖励 × 数量”，有原图则继续显示原图。
   未来新增图标需核对后扩充映射，不能保证未知奖励自动获得名称。

只遍历本次 POST 的 `awardIds`，不会把整个月历当成本次已领取奖励；解析和卡片绘制
均未对奖励条数作截断，所有奖励自动换行。运行时不新增 AKEData 全表请求或额外签到请求。

## 本地预览

```powershell
.venv/Scripts/python.exe scripts/render_endfield_attendance_preview.py --stress
```

生成 `output/attendance-card/` 下的 PNG、HTML 和 `validation.json`。
预览使用合成账号与数量，不调用签到接口。检查单账号、多账号、已签到、失败、空列表、
长昵称/长奖励/长错误消息与 15 账号场景。

预览脚本从 `--icon-dir` 指定的本地目录读取 `item_diamond.png` 与 `item_gold.png`，
默认目录为 `output/attendance-real-icons/`；缺文件时展示文本降级，不绘制替代图标。
本次预览使用 AKEData 上的真实游戏原图：

- [嵌晶玉](https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/itemiconbig/item_diamond.png)
- [折金票](https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/itemiconbig/item_gold.png)

额外验证全部道具时，将上表 8 张森空岛原图按原文件名保存至 `--icon-dir`，然后运行：

```powershell
.venv/Scripts/python.exe scripts/render_endfield_attendance_preview.py --stress --all-rewards
```

增加 `all-rewards`（8 类道具）与 `all-22-variants`（22 个配置）两个显示压力场景，
名称通过正式解析逻辑产生；它们仅用于展示验证，不代表一次签到实际可领全部道具。
检查内容包括文字超框、行重叠、断图和总高度，原图不做重绘或改色。

该目录只用于预览，正式机器人仍按签到接口返回的奖励动态取图。本次核对未执行真实签到。
