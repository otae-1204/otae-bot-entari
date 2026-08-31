# Endfield challenge page facts

这份事实记录仅服务 Endfield 个人挑战页的原型和数据边界，不把活动文案当作关卡静态数据。

- 官方公告将「影拓丰碑」描述为挑战玩法，并持续以系列关卡更新；「战争回响」为赛季制常驻挑战玩法。
- 森空岛个人接口的完整记录分别来自 `GET /api/v1/game/endfield/card/indie-hard` 与 `GET /api/v1/game/endfield/card/war-echoes`。
- 影拓响应包含主题、普通/苦难关卡、最佳记录、队伍快照、机制和敌人；战争回响响应包含赛季、轮换、普通/困难/残酷关卡、星数、追加目标、最佳记录、敌人和荣誉。
- 页面只呈现绑定角色自己的公开游戏数据；不显示森空岛内部用户 ID、凭据、签名或设备信息。
- 官方活动名称与时间可能更新，页面只使用本次接口返回的实时字段，不硬编码当前主题/赛季名称。

事实来源：

- [明日方舟：终末地官方网站公告](https://endfield.hypergryph.com/news/9335)
- `docs/skland_endfield_personal_api.md` 中的本地接口核验记录
