# 森空岛终末地公开数据独立查询

独立脚本：`scripts/endfield/query_skland_endfield.py`。

脚本复用现有 `.runtime/skland_reverse/session_sensitive.json`，自动刷新签名上下文，查询终末地个人详情、战争回响、危机合约和影拓丰碑，并生成原始 JSON、脱敏 JSON 与 Markdown 摘要。

2026-08-19 使用森空岛 `1.62.0` MuMu 当前登录态复查后，会话文件除 `cred` 外还需要保存与其匹配的完整 `dId`。本次 App 登录态对应的 `dId` 来源是 WebView 的 `smidV2` Cookie；它与 `cred` 同属敏感会话材料，不得写入命令行、日志、Git 或公开摘要。旧会话若不要求设备绑定，`dId` 可以为空；不要把两种会话混用。

## 通过昵称定位内部用户 ID

不能把 Web 公开主页 ID 直接传给绑定接口。先调用原生用户搜索：

```http
GET /api/v1/user/search?keyword=<昵称>&kind=1&pageNumber=1&pageSize=20
```

从精确昵称结果中取得内部用户 ID，再执行：

```powershell
python scripts\endfield\query_skland_endfield.py `
  --user-id <INTERNAL_USER_ID> `
  --label target
```

`--user-id` 是森空岛原生内部用户 ID，不是 Web/IM 公开 ID、森空岛 showId、终末地游戏 UID 或 roleId。

## 已知角色时直接查询

```powershell
python scripts\endfield\query_skland_endfield.py `
  --role-id <ROLE_ID> `
  --server-id 1 `
  --label target
```

查询其他用户时推荐使用 `--user-id`，脚本会先查询绑定并在详情请求中附带 `userId=<INTERNAL_USER_ID>`。

## 血狼破军复查结果

- 森空岛昵称：`血狼破军`
- 原生内部用户 ID：`101126`
- Web/IM 公开 ID：`3002521479368`
- hgId：`893109180714`
- 终末地游戏 UID：`618965353`
- 终末地 roleId：`1334382133`
- serverId：`1`

个人详情接口返回 `code: 0`，角色等级 60、世界等级 7、主线“志同道合”，拥有 28 名干员、59 件武器、321 项档案。

危机合约与影拓丰碑接口也返回 `code: 0`，但当前业务字段分别为 `crisisContract: null` 和 `indieHard: null`。这表示接口和账号定位均正常，只是当前没有可公开展示的记录，或对应展示配置尚未开启。

## 当前账号战争回响复查（2026-08-19）

独立详情接口：

```http
GET /api/v1/game/endfield/card/war-echoes?roleId=<ROLE_ID>&serverId=<SERVER_ID>
```

当前账号实测返回 `code: 0`，`data.warEchoes` 含 2 个赛季、5 个轮换、15 个关卡轮换项、45 个普通/困难/残酷难度对象和 8 条荣誉记录。当前可见赛季为“谵妄赛季”和“追忆赛季”，两季账号汇总均为 9 星且 `allPlusTasks: true`。

完整响应与脱敏副本保存在 `.runtime/skland_reverse/responses/`；正式文档只记录字段结构和聚合结论，不公开角色 UID、内部用户 ID、`cred`、`dId`、签名 token 或请求头。

## 输出

```text
.runtime/skland_reverse/responses/public_<label>_binding_sensitive.json
.runtime/skland_reverse/responses/public_<label>_detail_sensitive.json
.runtime/skland_reverse/responses/public_<label>_war_echoes_sensitive.json
.runtime/skland_reverse/responses/public_<label>_crisis_contract_sensitive.json
.runtime/skland_reverse/responses/public_<label>_indie_hard_sensitive.json
.runtime/skland_reverse/responses/redacted_public_<label>_*.json
.runtime/skland_reverse/responses/public_<label>_summary.md
```

真实会话、原始响应和内部用户 ID 只保存在 `.runtime`，不要提交到 Git 或发送到 Bot 公共消息。
