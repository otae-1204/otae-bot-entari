# 终末地 Wiki（fz.wiki / api.fz.wiki）开放 API 调研

调研时间：2026-07-06  
目标样例：

```text
https://api.fz.wiki/api/v1/articles/by-title?ns=0&title=%E5%B9%B2%E5%91%98%2F%E5%8D%A1%E7%BC%AA&withRevision=1
```

## 1. 调研结论

- API 基址：`https://api.fz.wiki/api/v1`
- 前端站点：`https://fz.wiki/`
- 资源/CDN 域：
  - `https://assets.fz.wiki`
  - `https://assets.fz.wiki/upload`
- 未发现公开 Swagger / OpenAPI 文档。以下路径实测均为 `404`：
  - `https://api.fz.wiki/`
  - `https://api.fz.wiki/docs`
  - `https://api.fz.wiki/openapi.json`
  - `https://api.fz.wiki/swagger.json`
  - `https://api.fz.wiki/api-docs`
  - `https://api.fz.wiki/api/v1`
- 本文档的 API 清单来自：
  1. 对样例 URL 和常用接口的实际只读请求；
  2. 对 `https://fz.wiki/` Next.js 前端 JS chunk 的接口字符串枚举；
  3. 对枚举出的接口进行 GET / read-like POST 探测。
- 对 bot-entari 的 Endfield 插件而言，最有价值的是 `GET /articles/by-title?withRevision=1`：它会返回条目当前 revision 的 `contentJson`，其中干员页包含结构化的 `hero`、`skills`、`talents`、`potentials`、`weapons`、`materials`、`archive` 等数据。

## 2. 认证与通用行为

前端封装显示：

```text
API_BASE = https://api.fz.wiki
请求实际路径 = ${API_BASE}/api/v1${path}
默认 credentials: include
若有 accessToken，则附加 Authorization: Bearer <token>
默认超时约 30s
```

认证相关：

| 接口 | 实测状态 | 说明 |
|---|---:|---|
| `GET /auth/me` | `401` | 未登录时返回 `UNAUTHORIZED` |
| `POST /auth/refresh` | `401` | 无 refresh cookie 时返回 `NO_REFRESH` |
| `POST /auth/logout` | 未做破坏性探测 | 前端登出使用 |

典型错误体：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Authentication required"
  }
}
```

## 3. 已实测可公开读取的 API

### 3.1 版本与公告

| Method | Path | 参数 | 返回 |
|---|---|---|---|
| `GET` | `/version` | 无 | `version`, `gitCommit`, `buildTime` |
| `GET` | `/announcements/latest` | 无 | `{ "announcement": ... }` |
| `GET` | `/announcements` | `all=1` 可选 | `{ "announcements": [...] }` |

实测 `/version`：

```json
{
  "version": "a2f1db7",
  "gitCommit": "a2f1db7",
  "buildTime": "2026-07-02T18:16:56Z"
}
```

### 3.2 条目列表与条目读取

#### `GET /articles`

用途：列出条目。

前端用法：

```text
GET /articles?ns=<namespace>&limit=<n>
GET /articles?ns=<namespace>&all=1
GET /articles?category=<category>
```

实测：

| 请求 | 状态 | 结果 |
|---|---:|---|
| `/articles?ns=0&limit=5` | `200` | 返回 5 条 |
| `/articles?ns=0&all=1` | `200` | 返回 2859 条 |
| `/articles?category=干员` | `200` | 返回 33 条 |

返回项主要字段：

```text
id, namespace, title, titleNormalized, currentRevisionId,
editProtection, moveProtection, protectionReason,
status, moderationStatus, categories, gameEntityId,
templateSpec, description, createdAt, updatedAt
```

#### `GET /articles/by-title`

用途：按 namespace + title 获取条目；可选带当前 revision。

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `ns` | 是 | namespace 数字；主命名空间为 `0` |
| `title` | 是 | 条目标题，例如 `干员/卡缪` |
| `withRevision` | 否 | `1` 时同时返回当前 revision |

样例：

```text
GET /articles/by-title?ns=0&title=干员/卡缪
GET /articles/by-title?ns=0&title=干员/卡缪&withRevision=1
```

不带 revision 返回：

```json
{
  "article": {
    "id": "019f01e4-f3e0-73fb-8345-c103894d1e42",
    "namespace": 0,
    "title": "干员/卡缪",
    "titleNormalized": "干员/卡缪",
    "currentRevisionId": "019f22b1-2ff3-785d-94cd-ab49ee5d367f",
    "editProtection": "SEMI",
    "moveProtection": "SEMI",
    "protectionReason": "责任编辑：宏山科学院",
    "status": "ACTIVE",
    "moderationStatus": "APPROVED",
    "categories": ["先锋", "六星干员", "干员"],
    "gameEntityId": null,
    "templateSpec": null,
    "description": "卡缪，6星先锋，长柄武器，技力恢复、灼热附着、连击",
    "createdAt": "2026-06-26T03:04:37.300Z",
    "updatedAt": "2026-07-02T11:55:26.045Z"
  }
}
```

带 `withRevision=1` 时多返回：

```text
revision.id
revision.articleId
revision.parentRevisionId
revision.contentJson
revision.contentText
revision.editSummary
revision.isMinorEdit
revision.authorId
revision.authorName
revision.createdAt
```

其中干员页 `revision.contentJson.content[0].attrs` 可直接拿到卡片结构化数据：

```text
hero
attributes
skills
talents
potentials
weapons
materials
archive
```

#### `GET /articles/by-title/meta`

用途：轻量检查条目元信息。

参数同 `by-title`：

```text
GET /articles/by-title/meta?ns=0&title=干员/卡缪
```

返回字段：

```text
exists, namespace, title, editProtection, categories,
description, authorName, publishedAt, modifiedAt
```

注意：即使条目不存在，实测也会返回 `200`，需看 `exists` 字段。

#### `POST /articles/exists`

用途：批量检查条目是否存在。虽然是 POST，但语义是读取。

请求体格式来自前端：

```json
{
  "refs": [
    {"namespace": 0, "title": "干员/卡缪"},
    {"namespace": 0, "title": "不存在页面"}
  ]
}
```

返回：

```json
{
  "results": [...]
}
```

#### `GET /articles/summaries`

用途：按 namespace + prefix 获取条目摘要列表。

参数：

| 参数 | 说明 |
|---|---|
| `ns` | namespace |
| `prefix` | 标题前缀 |

样例：

```text
GET /articles/summaries?ns=0&prefix=干员/
```

实测返回项：

```json
{
  "namespace": 0,
  "title": "干员/卡缪",
  "updatedAt": "2026-07-02T11:55:26.045Z",
  "summaryJson": null
}
```

#### `GET /articles/random`

用途：随机条目。

返回：

```json
{
  "article": { "...": "..." }
}
```

### 3.3 Revision / 历史

#### `GET /articles/{articleId}/history`

用途：获取条目历史 revision 列表。

样例：

```text
GET /articles/019f01e4-f3e0-73fb-8345-c103894d1e42/history
```

返回字段：

```text
revisions[].id
revisions[].articleId
revisions[].parentRevisionId
revisions[].editSummary
revisions[].isMinorEdit
revisions[].contentSize
revisions[].authorId
revisions[].authorName
revisions[].createdAt
```

#### `GET /revisions/{revisionId}`

用途：按 revision id 获取完整 revision 内容。

样例：

```text
GET /revisions/019f22b1-2ff3-785d-94cd-ab49ee5d367f
```

返回：

```json
{
  "revision": {
    "id": "...",
    "articleId": "...",
    "parentRevisionId": "...",
    "contentJson": { "type": "doc", "content": [...] },
    "contentText": "...",
    "editSummary": "...",
    "isMinorEdit": false,
    "authorId": "...",
    "authorName": "...",
    "createdAt": "..."
  }
}
```

#### `GET /revisions/recent`

用途：最近修订。

参数：

| 参数 | 说明 |
|---|---|
| `limit` | 数量 |
| `author` | 可选，按作者过滤 |

样例：

```text
GET /revisions/recent?limit=5
```

### 3.4 链接关系、重定向、讨论

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/backlinks?ns=<ns>&title=<title>` | 反向链接 |
| `GET` | `/articles/{articleId}/redirect-terms` | 条目重定向词 |
| `GET` | `/articles/{articleId}/discussions?cursor=<cursor>&limit=<n>` | 条目讨论列表 |

实测：

```text
GET /backlinks?ns=0&title=干员/卡缪
```

返回：

```json
{
  "articles": [...]
}
```

讨论列表返回：

```json
{
  "discussions": [],
  "nextCursor": null
}
```

### 3.5 搜索

#### `GET /search`

用途：基础搜索，未登录可用。

参数：

| 参数 | 说明 |
|---|---|
| `q` | 搜索词 |
| `limit` | 数量 |
| `track` | 前端可传 `1`，用于统计搜索行为 |

样例：

```text
GET /search?q=卡缪&limit=5
```

返回项字段：

```text
articleId, namespace, title, snippet, createdAt, updatedAt
```

#### `GET /search/suggest`

用途：标题补全。

```text
GET /search/suggest?prefix=卡&limit=5
```

返回：

```json
{
  "suggestions": ["卡池/染赤申领", "..."]
}
```

#### `GET /search/popular`

用途：热门搜索词。

```text
GET /search/popular?limit=6
```

实测当前返回空列表：

```json
{
  "terms": []
}
```

#### `GET /search/fulltext`

用途：全文搜索。

前端路径：

```text
GET /search/fulltext?q=<q>&limit=<n>&track=1
```

实测未登录状态为 `403`：

```json
{
  "error": {
    "code": "FULL_TEXT_GUEST_DISABLED",
    "message": "full-text search requires login"
  }
}
```

站点配置 `/site-settings` 也显示：

```json
{
  "registeredUserEditEnabled": false,
  "fullTextSearchGuestEnabled": false
}
```

### 3.6 分类、孤链、缺页分析

| Method | Path | 返回 |
|---|---|---|
| `GET` | `/analysis/dead-ends` | `{ "articles": [...] }` |
| `GET` | `/analysis/lonely-pages` | `{ "articles": [...] }` |
| `GET` | `/analysis/wanted-pages` | `{ "pages": [...] }` |

实测数量：

| 接口 | 数量 |
|---|---:|
| `/analysis/dead-ends` | 103 |
| `/analysis/lonely-pages` | 200 |
| `/analysis/wanted-pages` | 200 |

### 3.7 模板

#### `GET /templates`

用途：列出模板规格。

返回项字段：

```text
templateId, label, description, paramCount, providerPluginId, updatedAt
```

实测返回 33 个模板。与 Endfield 干员页相关的模板为：

```text
templateId = 干员档案
providerPluginId = endfield-character-cards
```

#### `GET /templates/{templateId}/spec`

用途：获取模板参数与 slot 规格。

样例：

```text
GET /templates/干员档案/spec
```

返回字段：

```text
slots[]
parameters[]
templateId
providerPluginId
```

`干员档案` 的 slots 包括：

```text
hero
attributes
skills
talents
potentials
weapons
materials
archive
```

### 3.8 统计

#### `GET /stats`

用途：站点统计。

参数：

| 参数 | 说明 |
|---|---|
| `days` | 统计天数，前端默认 14 |
| `full` | `1` 时返回更完整统计 |

样例：

```text
GET /stats?days=14
GET /stats?days=14&full=1
```

`full=1` 实测字段：

```text
articleCount
revisionCount
editorCount
activityBuckets
nsBreakdown
topEditors
totalContentBytes
```

实测总体：

```text
articleCount = 3067
revisionCount = 27104
editorCount = 7
totalContentBytes = 409673011
```

命名空间分布：

```text
0  -> 2859
4  -> 15
10 -> 40
12 -> 7
14 -> 146
```

#### `GET /contributors`

用途：贡献者统计。

返回：

```json
{
  "contributors": [...]
}
```

### 3.9 用户公开信息

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/users/search?q=<q>&limit=<n>` | 搜索用户 |
| `GET` | `/users/{username}/profile` | 用户公开资料 |
| `GET` | `/users/{username}/activity?days=<n>` | 用户公开活动 |

实测：

```text
GET /users/search?q=otae&limit=5 -> 200
GET /users/otae/profile -> 200
GET /users/otae/activity?days=30 -> 200
```

### 3.10 站点配置、导航、白名单

| Method | Path | 实测状态 | 说明 |
|---|---|---:|---|
| `GET` | `/nav` | `200` | 站点导航配置 |
| `GET` | `/nav/entitlements` | `200` | 当前访客可见权限项；未登录为空数组 |
| `GET` | `/site-settings` | `200` | 站点开关 |
| `GET` | `/iframe-embed/whitelist` | `200` | iframe 白名单 |
| `GET` | `/plugins/config` | `200` | 插件配置，当前为空对象 |

实测：

```json
{
  "registeredUserEditEnabled": false,
  "fullTextSearchGuestEnabled": false
}
```

`/iframe-embed/whitelist`：

```json
{
  "allowedHosts": ["www.zmdmap.com"]
}
```

### 3.11 Watchlist 检查

#### `POST /watchlist/check`

用途：检查当前用户/访客 watchlist 状态。实测未登录也返回 `200`。

请求体：

```json
{
  "entries": [
    {"namespace": 0, "title": "干员/卡缪"}
  ]
}
```

返回：

```json
{
  "results": [...]
}
```

## 4. 前端发现但需要登录、写权限或管理权限的接口

以下接口由前端 JS chunk 枚举得到；未进行会改变服务端状态的请求，只记录用途与推测权限。

### 4.1 条目写入、移动、保护

| Method | Path | 用途 | 权限推测 |
|---|---|---|---|
| `PUT` | `/articles/by-title?ns=<ns>&title=<title>` | 保存条目内容 | 登录 + 编辑权限 |
| `POST` | `/articles/rename?ns=<ns>&fromTitle=<old>&newTitle=<new>` | 移动/重命名条目 | 编辑权限 |
| `PATCH` | `/articles/by-title/protection` | 修改编辑/移动保护 | 管理/版主权限 |
| `PUT` | `/articles/{articleId}/redirect-terms` | 设置重定向词 | 编辑权限 |

保存条目前端请求体：

```json
{
  "contentJson": {},
  "baseRevisionId": "...",
  "editSummary": "...",
  "isMinorEdit": false,
  "description": "...",
  "force": false
}
```

### 4.2 讨论

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/articles/{articleId}/discussions` | 新增讨论 |
| `PUT` | `/discussions/{discussionId}` | 编辑讨论 |
| `PATCH` | `/discussions/{discussionId}/status` | 修改讨论状态 |
| `DELETE` | `/discussions/{discussionId}` | 删除讨论 |

### 4.3 上传

| Method | Path | 用途 | 备注 |
|---|---|---|---|
| `POST` | `/upload` | 上传文件 | 前端使用 `FormData(file)` |

前端限制：

```text
默认最大文件大小：0xa00000 = 10 MiB
图片：png / jpg / jpeg / gif / webp
其他：pdf / doc / docx / xls / xlsx / ppt / pptx / txt / zip
```

上传后的展示 URL 由 `https://assets.fz.wiki/upload/<path>` 拼出。

### 4.4 公告、配置、插件、导航

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/announcements` | 创建公告 |
| `PUT` | `/announcements/{id}` | 修改公告 |
| `DELETE` | `/announcements/{id}` | 删除公告 |
| `PUT` | `/nav` | 修改导航 |
| `PUT` | `/site-settings` | 修改站点配置 |
| `PUT` | `/iframe-embed/whitelist` | 修改 iframe 白名单 |
| `PUT` | `/plugins/config/{pluginId}` | 修改插件启用状态 |

### 4.5 积分、个人资料

| Method | Path | 实测/推测 |
|---|---|---|
| `GET` | `/points/me` | 未登录 `401` |
| `POST` | `/points/sign-in` | 签到 |
| `POST` | `/points/article-reads/{articleId}` | 阅读记录/积分 |
| `PUT` | `/users/me/profile` | 修改当前用户资料 |
| `GET` | `/users/me/created-articles` | 未登录 `401` |

### 4.6 审核与维护

| Method | Path | 实测/推测 |
|---|---|---|
| `GET` | `/moderation/stats` | 未登录 `401` |
| `GET` | `/moderation/queue?page=<p>&size=<n>&status=<status>` | 未登录 `401` |
| `GET` | `/moderation/queue/{id}` | 审核详情 |
| `POST` | `/moderation/queue/{id}` | 审核操作 |
| `GET` | `/moderation/audit-log?page=<p>&size=<n>` | 审核日志 |
| `GET` | `/moderation/discussions?page=<p>&size=<n>&status=<status>` | 评论审核列表 |
| `POST` | `/moderation/discussions/{id}` | 评论审核操作 |
| `GET` | `/admin/maintenance/duplicate-revisions` | 未登录 `401` |
| `POST` | `/admin/maintenance/duplicate-revisions/cleanup` | 重复 revision 清理 |
| `GET` | `/admin/maintenance/duplicate-revisions/cleanup/status` | 清理状态 |

## 5. 资源 URL 与图片

条目结构化数据中大量图片字段已是完整 URL，例如：

```text
https://assets.fz.wiki/ed558e5d74bb2b1d/15f8cafda003a820.png
https://assets.fz.wiki/upload/characters/illust/camille.png
```

前端资源基址：

```text
NEXT_PUBLIC_GAME_ASSETS_BASE_URL -> 默认 https://assets.fz.wiki
NEXT_PUBLIC_UPLOAD_BASE_URL      -> 默认 https://assets.fz.wiki/upload
```

对 bot 拉取图片时，不需要额外 API，直接请求这些 URL 即可。

## 6. 对 bot-entari Endfield 插件的数据接入建议

如果后续要替换或补充现有数据源，建议优先使用：

```text
GET /articles/by-title?ns=0&title=干员/<中文名>&withRevision=1
```

解析路径：

```text
response.revision.contentJson.content[]
  -> 找 type == "wikiTemplateInstance"
  -> attrs.hero
  -> attrs.attributes
  -> attrs.skills
  -> attrs.talents
  -> attrs.potentials
  -> attrs.weapons
  -> attrs.materials
  -> attrs.archive
```

可先用这些索引接口做名称发现：

```text
GET /articles/summaries?ns=0&prefix=干员/
GET /articles?category=干员
GET /search?q=<关键词>&limit=5
GET /search/suggest?prefix=<前缀>&limit=5
```

注意点：

- `contentJson` 很大，卡缪样例 revision 内容约 130KB，适合做缓存。
- `withRevision=1` 返回内容最完整，但不适合高频无缓存轮询。
- 全文搜索未登录当前不可用，基础搜索可用。
- 编辑、上传、管理接口均不应在 bot 的普通查询功能中调用。

## 7. 已枚举 API 总表

### 7.1 公开读取/只读型

| Method | Path |
|---|---|
| `GET` | `/version` |
| `GET` | `/announcements/latest` |
| `GET` | `/announcements` |
| `GET` | `/articles` |
| `GET` | `/articles/by-title` |
| `GET` | `/articles/by-title/meta` |
| `POST` | `/articles/exists` |
| `GET` | `/articles/summaries` |
| `GET` | `/articles/random` |
| `GET` | `/articles/{articleId}/history` |
| `GET` | `/articles/{articleId}/redirect-terms` |
| `GET` | `/articles/{articleId}/discussions` |
| `GET` | `/revisions/{revisionId}` |
| `GET` | `/revisions/recent` |
| `GET` | `/backlinks` |
| `GET` | `/search` |
| `GET` | `/search/suggest` |
| `GET` | `/search/popular` |
| `GET` | `/stats` |
| `GET` | `/contributors` |
| `GET` | `/analysis/dead-ends` |
| `GET` | `/analysis/lonely-pages` |
| `GET` | `/analysis/wanted-pages` |
| `GET` | `/templates` |
| `GET` | `/templates/{templateId}/spec` |
| `GET` | `/users/search` |
| `GET` | `/users/{username}/profile` |
| `GET` | `/users/{username}/activity` |
| `POST` | `/watchlist/check` |
| `GET` | `/nav` |
| `GET` | `/nav/entitlements` |
| `GET` | `/site-settings` |
| `GET` | `/iframe-embed/whitelist` |
| `GET` | `/plugins/config` |

### 7.2 存在但未登录不可用或需要权限

| Method | Path | 未登录实测/权限 |
|---|---|---|
| `GET` | `/search/fulltext` | `403 FULL_TEXT_GUEST_DISABLED` |
| `GET` | `/auth/me` | `401 UNAUTHORIZED` |
| `POST` | `/auth/refresh` | `401 NO_REFRESH` |
| `POST` | `/auth/logout` | 登录态接口 |
| `PUT` | `/articles/by-title` | 编辑权限 |
| `POST` | `/articles/rename` | 编辑权限 |
| `PATCH` | `/articles/by-title/protection` | 管理/版主权限 |
| `PUT` | `/articles/{articleId}/redirect-terms` | 编辑权限 |
| `POST` | `/articles/{articleId}/discussions` | 登录/讨论权限 |
| `PUT` | `/discussions/{discussionId}` | 作者/管理权限 |
| `PATCH` | `/discussions/{discussionId}/status` | 管理/审核权限 |
| `DELETE` | `/discussions/{discussionId}` | 作者/管理权限 |
| `POST` | `/upload` | 上传权限 |
| `POST` | `/announcements` | 管理权限 |
| `PUT` | `/announcements/{id}` | 管理权限 |
| `DELETE` | `/announcements/{id}` | 管理权限 |
| `PUT` | `/nav` | 管理权限 |
| `PUT` | `/site-settings` | 管理权限 |
| `PUT` | `/iframe-embed/whitelist` | 管理权限 |
| `PUT` | `/plugins/config/{pluginId}` | 管理权限 |
| `GET` | `/points/me` | `401 UNAUTHORIZED` |
| `POST` | `/points/sign-in` | 登录 |
| `POST` | `/points/article-reads/{articleId}` | 登录/静默记录 |
| `PUT` | `/users/me/profile` | 登录 |
| `GET` | `/users/me/created-articles` | `401 UNAUTHORIZED` |
| `GET` | `/moderation/stats` | `401 UNAUTHORIZED` |
| `GET` | `/moderation/queue` | `401 UNAUTHORIZED` |
| `GET` | `/moderation/queue/{id}` | 审核权限 |
| `POST` | `/moderation/queue/{id}` | 审核权限 |
| `GET` | `/moderation/audit-log` | 审核权限 |
| `GET` | `/moderation/discussions` | 审核权限 |
| `POST` | `/moderation/discussions/{id}` | 审核权限 |
| `GET` | `/admin/maintenance/duplicate-revisions` | `401 UNAUTHORIZED` |
| `POST` | `/admin/maintenance/duplicate-revisions/cleanup` | 管理权限 |
| `GET` | `/admin/maintenance/duplicate-revisions/cleanup/status` | 管理权限 |

## 8. 附：本次探测样例命令

```powershell
Invoke-WebRequest -Uri 'https://api.fz.wiki/api/v1/articles/by-title?ns=0&title=%E5%B9%B2%E5%91%98%2F%E5%8D%A1%E7%BC%AA&withRevision=1'
```

Python 侧枚举思路：

```python
import re
from urllib.parse import urljoin
from urllib.request import Request, urlopen

html = urlopen(Request("https://fz.wiki/", headers={"User-Agent": "Mozilla/5.0"})).read().decode("utf-8", "replace")
scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+\\.js[^"]*)"', html)))

for script in scripts:
    url = urljoin("https://fz.wiki/", script)
    text = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"})).read().decode("utf-8", "replace")
    if "/api/v1/" in text or "api.fz.wiki" in text:
        print(url)
```

