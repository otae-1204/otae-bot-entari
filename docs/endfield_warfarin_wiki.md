# Warfarin Wiki 数据源记录

记录时间：2026-06-17

Warfarin Wiki 是《明日方舟：终末地》的同人资料站，站点前端为 Remix 应用。当前可直接使用其公开 JSON API 作为终末地插件的主数据源。

## 基本信息

- 站点首页：https://warfarin.wiki/cn
- API 根地址：https://api.warfarin.wiki
- API 版本：v1
- 中文语言代码：cn
- 当前游戏数据版本：1.3
- 站点首页显示最后更新：2026-06-05

## 搜索接口

```text
GET https://api.warfarin.wiki/v1/{lang}/search?q={query}
```

中文示例：

```text
GET https://api.warfarin.wiki/v1/cn/search?q=陈
```

已验证返回结构：

```json
{
  "query": "陈",
  "results": [
    {
      "slug": "chen-qianyu",
      "name": "陈千语",
      "type": "operators",
      "category": "近卫",
      "snippet": "...",
      "score": 42.10282055808267
    }
  ]
}
```

插件用途：

- 用户输入模糊名称时先走搜索接口。
- 根据 `type + slug` 路由到具体详情接口。
- `snippet` 可用于搜索结果预览。

## 列表接口

```text
GET https://api.warfarin.wiki/v1/{lang}/{category}
```

已验证中文分类：

| 分类 | URL | count | 说明 |
| --- | --- | ---: | --- |
| operators | `/v1/cn/operators` | 27 | 干员 |
| weapons | `/v1/cn/weapons` | 71 | 武器 |
| enemies | `/v1/cn/enemies` | 75 | 敌人 |
| facilities | `/v1/cn/facilities` | 94 | 设施 |
| items | `/v1/cn/items` | 2376 | 物品 |
| gear | `/v1/cn/gear` | 220 | 装备 |
| tutorials | `/v1/cn/tutorials` | 308 | 教程 |
| baker | `/v1/cn/baker` | 244 | Baker 消息 |
| medals | `/v1/cn/medals` | 114 | 奖章 |
| missions | `/v1/cn/missions` | 317 | 任务 |

`lore` 分类在 API 中暂时返回 404，不能作为可用接口依赖。

列表接口通用返回结构：

```json
{
  "meta": {
    "lang": "cn",
    "type": "operator",
    "version": "1.3",
    "count": 27
  },
  "data": [],
  "refs": {}
}
```

## 详情接口

```text
GET https://api.warfarin.wiki/v1/{lang}/{category}/{slug}
```

已验证示例：

```text
GET https://api.warfarin.wiki/v1/cn/operators/chen-qianyu
GET https://api.warfarin.wiki/v1/cn/operators/endministrator
GET https://api.warfarin.wiki/v1/cn/weapons/exemplar
GET https://api.warfarin.wiki/v1/cn/items/item_originium_ore
GET https://api.warfarin.wiki/v1/cn/enemies/eny_0021_agmelee
```

详情接口通用返回结构：

```json
{
  "meta": {
    "id": "chr_0005_chen",
    "slug": "chen-qianyu",
    "name": "陈千语",
    "lang": "cn",
    "type": "operator",
    "version": "1.3"
  },
  "data": {},
  "refs": {}
}
```

注意：

- 详情数据量较大，干员详情约数百 KB。
- `data` 内部字段贴近游戏表结构，渲染前需要做业务层整理。
- `refs` 可用于翻译、类型名、标签等引用数据补全。

## 静态资源

Warfarin 前端使用以下静态资源域名：

```text
https://static.warfarin.wiki
https://assets.warfarin.wiki
```

常见图标路径可从接口字段中组合，例如：

- 干员头像：`https://static.warfarin.wiki/v4/charicon/icon_{id}.webp`
- 物品图标：`https://static.warfarin.wiki/v4/itemicon/{iconId}.webp`
- 敌人图标：`https://static.warfarin.wiki/v4/monstericon/{id}.webp`

实际路径应优先按接口或前端资源工具函数验证，不要在业务层硬编码过多分类规则。

## 插件设计建议

第一版终末地插件建议将 Warfarin Wiki 作为主数据源：

1. `client.py` 封装 HTTP 请求、超时、重试和 User-Agent。
2. `service.py` 封装搜索、分类列表、详情查询和字段整理。
3. `store.py` 做缓存，缓存键建议为 `warfarin:{lang}:{category}:{slug}`。
4. 缓存版本使用 `meta.version`，当前为 `1.3`。
5. 命令入口优先支持搜索式查询，例如“终末地 陈千语”、“终末地 武器 典范”。
6. 图片渲染只消费整理后的 ViewModel，不直接依赖原始 API 字段。

初期可支持：

- 干员查询
- 武器查询
- 物品查询
- 敌人查询
- 通用搜索

后续再扩展：

- 装备词条
- 奖章
- 任务
- Baker 消息
- 设施/工业相关信息

## 风险和约束

- 这是非官方同人站 API，路径和字段可能变化。
- 需要设置合理超时，避免机器人命令阻塞。
- 需要做本地缓存，减少对源站压力。
- 不应收录或传播站点声明中排除的 NDA/泄露内容。
- 输出中应标注数据来源为 Warfarin Wiki。
