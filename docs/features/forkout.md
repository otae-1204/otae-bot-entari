# 叉出去（plugins/forkout）

把被 @ 的群友头像贴到「叉出去」底图上发出，并按群统计被叉次数。用户说明见 `/help`（见 [帮助](help_plugin.md)）。

## 用法

| 触发 | 说明 |
| --- | --- |
| 消息里含 `/forkout`、`/叉出去`、`/叉`、`/X` 或 `/x`，并 @ 某人 | 用第一个被 @ 的人的头像生成图片，该人在本群被叉次数 +1 |
| 同样的文字，回复某条消息但不 @ | 发不带头像的底图，引用被回复的消息；不计数 |
| `/forkrank` | 本群被叉排行前 15，仅群聊 |

`/forkrank` 别名：`/叉排行`、`/被叉排行榜`、`/叉人排行`、`/Xrank`、`/XR`、`/xrank`、`/xr`。

触发词不是注册的命令，而是消息监听：在任意文本段里匹配 `/(forkout|叉出去|叉|X|x)` 后接空白或结尾。

头像优先用 Satori 成员资料里的头像地址，失败时依次尝试几个公开的 QQ 头像接口。

## 配置

无。

## 数据

- `data/forkout_counts.json`：键为 `<群ID>.<用户ID>` 的被叉次数。
- 底图 `assets/image/forkout/forkout.jpg`；缺失时回复「底图资源缺失」。合成结果每次写到 `assets/image/forkout/forkout_result.jpg`（不进 Git）再读出发送。
