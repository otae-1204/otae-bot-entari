# 森空岛终末地 API：UI 数据元素完整清单

更新时间：2026-08-19（Asia/Shanghai）

用途：为新的终末地账号资料 UI 设计提供数据边界、字段清单、当前样本规模和页面元素建议。

本清单来自本机已经成功返回 `code: 0` 的脱敏响应，不包含登录凭据、签名材料、手机号、内部用户映射或原始敏感 JSON。基础账号、危机合约和影拓丰碑规模仍以 2026-07 样本为准；战争回响使用 2026-08-19 森空岛 `1.62.0` MuMu 当前账号实测响应。

## 1. 当前真正可用的接口

| 状态 | 接口 | 当前用途 |
|---|---|---|
| 已实测 | `GET /api/v1/game/player/binding` | 获取游戏绑定、终末地角色、服务器和默认角色 |
| 已实测 | `GET /api/v1/game/endfield/card/detail` | 获取账号概览、干员、配装、成就、航天器、地区探索、任务和活动摘要 |
| 已实测 | `GET /api/v1/game/endfield/card/war-echoes` | 获取战争回响赛季、轮换、普通/困难/残酷关卡、最佳记录、敌人与荣誉 |
| 已实测 | `GET /api/v1/game/endfield/card/crisis-contract` | 获取危机合约完整状态、历史、指标、关卡和敌人 |
| 已实测 | `GET /api/v1/game/endfield/card/crisis-contract/record` | 按 `recordId` 获取合约当次武器、三词条等级、装备和实际指标 |
| 已实测 | `GET /api/v1/game/endfield/card/indie-hard` | 获取影拓丰碑全部主题、关卡、最佳队伍和敌人 |
| 已实测 | `GET /api/v1/user/search` | 按昵称搜索森空岛用户，获得用于后续查询的内部用户标识 |
| 已留存响应 | 森空岛用户主页数据 | 昵称、头像、背景、简介、等级积分、社交统计等；尚未纳入当前终末地查询脚本的稳定接口层 |

当前脚本 `scripts/endfield/query_skland_endfield.py` 已稳定封装绑定、详情、战争回响、危机合约、最佳合约记录详情和影拓丰碑。

以下路径已从客户端静态代码确认，但不能按“当前已稳定获取”设计硬依赖：

```text
GET  /api/v1/game/endfield/card/char
POST /api/v1/game/endfield/card/detail/char-sort
POST /api/v1/game/endfield/card/detail/show-config
GET  /api/v1/gameplat/list
GET  /api/v1/game/player/info
GET  /api/v1/game/player/show-config
POST /api/v1/game/player/show-config
POST /api/v1/game/player/asset-show
POST /api/v1/gameplat/game/refresh
```

## 2. 当前样本的数据规模

最新自有账号样本可支持以下 UI 密度：

| 模块 | 当前样本 |
|---|---:|
| 账号等级 | 60 |
| 世界等级 | 7 |
| 干员 | 26 名 |
| 武器总数 | 54（详情接口只给总数及各干员当前装备，不给完整武器仓库） |
| 档案总数 | 222（只给总数，不给 222 条档案明细） |
| 成就奖牌 | 96 枚 |
| 成就展示槽 | 10 个 |
| 航天器房间 | 6 个 |
| 入驻槽位记录 | 15 条 |
| 地区 | 2 个：四号谷地、武陵 |
| 聚落 | 5 个 |
| 地图区域 | 13 个 |
| 危机合约历史 | 32 条 |
| 危机合约指标 | 46 个 |
| 危机合约敌人 | 12 种 |
| 影拓丰碑主题 | 5 个 |
| 影拓丰碑关卡 | 42 个普通/困难关卡 |
| 战争回响赛季 / 轮换 | 2 个赛季 / 5 个轮换 |
| 战争回响关卡轮换项 | 15 项（8 个唯一关卡名） |
| 战争回响难度对象 | 45 个普通/困难/残酷关卡对象 |
| 战争回响荣誉记录 | 8 条 |

当前概览还包含：主线进度、经验值、创建/保存/最近登录时间、体力、通行证、日常、周常、疑案进度和四个官方快捷入口。

## 3. 绑定与角色选择

### 3.1 游戏分组

`data.list[]`：

```text
appCode
appName
bindingList[]
defaultUid
```

响应还包含顶层 `data.serverDefaultBinding`；当前样本中它是空对象，UI 不应依赖其一定存在有效内容。

当前响应可能同时包含明日方舟、来自星尘和终末地，UI 必须筛选 `appCode == "endfield"`。

### 3.2 账号绑定

`bindingList[]`：

```text
uid
isOfficial
isDefault
channelMasterId
channelName
nickName
isDelete
gameName
gameId
roles[]
defaultRole
```

### 3.3 终末地角色

`roles[]` / `defaultRole`：

```text
serverId
roleId
nickname
level
isDefault
isBanned
serverType
serverName
```

适合 UI：账号切换器、服务器标签、默认角色标记、角色等级、封禁/失效状态。`uid`、`roleId` 和内部用户 ID 不应默认公开显示。

## 4. 账号详情 `card/detail`

`data.detail` 当前有 16 个模块：

```text
base
chars[]
achieve
spaceShip
domain[]
dungeon
bpSystem
dailyMission
weeklyMission
config
currentTs
quickaccess[]
indieHard
warEchoes
seekSuspicion
crisisContract[]
```

### 4.1 账号概览 `base`

| 字段 | 可视元素 |
|---|---|
| `serverName` | 服务器名称；可能为空，需隐藏空标签 |
| `roleId` | 游戏角色 ID；建议仅在“复制 UID”操作中显示 |
| `name` | 角色名 |
| `createTime` | 角色创建时间 |
| `saveTime` | 游戏数据保存时间 |
| `lastLoginTime` | 最近登录时间 |
| `exp` | 当前经验值；接口未给下一级经验上限 |
| `level` | 账号等级 |
| `worldLevel` | 世界等级 |
| `gender` | 性别枚举；需要客户端映射，不宜直接显示数字 |
| `avatarUrl` | 账号头像 |
| `mainMission.id` | 主线任务内部 ID |
| `mainMission.description` | 主线进度名称 |
| `charNum` | 干员总数 |
| `weaponNum` | 武器总数 |
| `docNum` | 档案总数 |

建议首屏元素：头像、角色名、等级、世界等级、主线进度、干员/武器/档案三项统计、最近登录或数据更新时间。

### 4.2 干员列表 `chars[]`

每名干员的账号侧字段：

```text
id
wikiItemId
level
evolvePhase
potentialLevel
gender
ownTs
userSkills
bodyEquip
armEquip
firstAccessory
secondAccessory
tacticalItem
weapon
talent
```

适合列表卡片：头像、名称、稀有度、职业、属性、等级、精英/突破阶段、潜能、武器、配装完成度。

#### 静态干员资料 `charData`

```text
id
name
avatarSqUrl
avatarRtUrl
illustrationUrl
rarity.key
rarity.value
profession.key
profession.value
property.key
property.value
weaponType.key
weaponType.value
tags[]
skills[]
abilityTalents[]
combatTalents[]
cultivationTalents[]
labelType（部分干员存在）
```

这些字段足以制作头像卡、立绘详情页、职业/属性/武器类型徽章和标签筛选。

#### 技能 `charData.skills[]`

```text
id
name
type.key
type.value
property.key
property.value
iconUrl
desc
descParams
descLevelParams
```

`descParams` 和 `descLevelParams` 是动态参数表，键名随技能变化。UI 需要做模板参数替换或保留富文本，而不是把对象直接展示给用户。

#### 三类天赋

`abilityTalents[]`、`combatTalents[]`、`cultivationTalents[]` 共享主体结构：

```text
id
name
iconUrl
desc
descParams
lockedIconUrl
```

#### 玩家技能等级 `userSkills`

`userSkills` 是以技能 ID 为动态键的对象，每项包含：

```text
skillId
level
maxLevel
```

可用于技能等级条、满级标记和未培养提示。

### 4.3 干员装备

四个装备槽：

```text
bodyEquip
armEquip
firstAccessory
secondAccessory
```

每槽包含 `equipId` 和 `equipData`。`equipData`：

```text
id
name
iconUrl
rarity.key
rarity.value
type.key
type.value
level.key
level.value
properties[]
isAccessory
suit
function
pkg
```

套装 `suit` 可能为 `null`，非空时包含：

```text
id
name
skillId
skillDesc
skillDescParams
```

适合 UI：四槽装备网格、稀有度边框、装备类型、属性词条、套装数量/套装效果、空槽状态。

### 4.4 战术物品

`tacticalItem.tacticalItemData`：

```text
id
name
iconUrl
rarity.key
rarity.value
activeEffectType.key
activeEffectType.value
activeEffect
passiveEffect
activeEffectParams
passiveEffectParams
```

外层 `tacticalItem` 还包含 `tacticalItemId`。

适合 UI：图标、名称、品质、主动效果、被动效果。未装备时整个对象可能为空。

### 4.5 当前武器

`weapon`：

```text
level
refineLevel
breakthroughLevel
gem
weaponData
wikiItemId
```

`weaponData`：

```text
id
name
iconUrl
rarity.key
rarity.value
type.key
type.value
function
description
skills[] -> key, value
```

武器嵌晶 `gem`：

```text
id
icon
gemData.termId
gemData.name
gemData.icon
gemData.templateId
```

适合 UI：武器大卡、等级、精炼、突破、武器技能、嵌晶。注意：该接口仅返回每名干员当前携带的武器，不能据此构建完整武器仓库。

### 4.6 干员成长节点 `talent`

```text
latestBreakNode
attrNodes[]
latestPassiveSkillNodes[]
latestFactorySkillNodes[]
latestSpaceshipSkillNodes[]
```

这些字段目前主要是节点 ID，没有同层可读名称。新 UI 可以显示完成数量或节点进度，但若要画完整天赋树，需要额外静态表映射。

### 4.7 成就 `achieve`

```text
achieveMedals[]
display
count
```

每枚奖牌：

```text
achievementData
level
isPlated
obtainTs
```

`achievementData`：

```text
id
name
initIcon
reforge2Icon
reforge3Icon
platedIcon
cateName
canCertify
cate
initLevel
```

当前样本的奖章分类：建设 18、技艺 26、奇想 6、章节 19、社交 4、活动 6、地区 10、锤炼 7。

适合 UI：奖章墙、分类筛选、等级/重铸状态、镀层状态、获得时间、10 枚展示奖章。图标应按 `level` 与 `isPlated` 选择对应资源。

### 4.8 航天器 `spaceShip.rooms[]`

房间：

```text
id
type
level
chars[]
reports[]
```

入驻干员：

```text
charId
physicalStrength
favorability
avatarUrl
```

当前样本有 6 个房间、15 条入驻记录，`reports[]` 为空。房间 `type` 是枚举，需要映射为可读房间名。

适合 UI：舰船平面/房间卡、房间等级、入驻头像、体力、好感度、空槽。报告模块必须允许空状态。

### 4.9 地区建设与探索 `domain[]`

地区：

```text
domainId
name
level
settlements[]
moneyMgr
collections[]
levels[]
factory
```

聚落 `settlements[]`：

```text
id
name
level
exp
expToLevelUp
remainMoney
moneyMax
officerCharIds[]
officerCharAvatar[]
lastTickTime
isFinalMaxLevel
```

地区资金 `moneyMgr`：

```text
total
count
```

地图收集 `collections[]` 与带总量的 `levels[]`：

```text
levelId
name（仅 levels）
puzzleCount
trchestCount
equipTrchestCount
pieceCount
blackboxCount
```

`levels[]` 中每项计数是 `{count, total}`，可以直接画探索进度；`collections[]` 是玩家已收集数。

当前样本：

- 四号谷地：地区等级 12，3 个聚落，6 个地图区域。
- 武陵：地区等级 18，2 个聚落，7 个地图区域。
- 两个地区的 `factory` 当前均为 `null`，不能假设工厂详情始终存在。

适合 UI：地区总览、聚落等级与资金进度、负责人头像、结算倒计时、地图探索条、谜题/宝箱/装备箱/碎片/黑匣子分类进度。

### 4.10 日常进度与资源

```text
dungeon.curStamina
dungeon.maxStamina
dungeon.maxTs

bpSystem.curLevel
bpSystem.maxLevel

dailyMission.dailyActivation
dailyMission.maxDailyActivation

weeklyMission.score
weeklyMission.total

seekSuspicion.count
seekSuspicion.total
```

适合 UI：体力环、完全恢复倒计时、通行证进度、日常/周常进度、疑案进度。数值有字符串和整数混用，视图模型应先统一转换。

### 4.11 展示配置

```text
config.charSwitch
config.standingsSwitch
config.charIds[]
```

`charIds[]` 是用户选定的展示干员。UI 可以把它们作为首屏阵容；必须尊重 `charSwitch`。`standingsSwitch == false` 时不应擅自展示排名信息。

### 4.12 快捷入口

`quickaccess[]`：

```text
name
icon
link
```

当前返回：每日签到、配队工具、养成建议、地图工具。链接多为 `skland://` 深链，网页或图片 UI 不能直接假设可打开。

### 4.13 详情接口内的活动摘要

`indieHard`、`warEchoes` 和 `crisisContract[]` 在详情接口中只适合做入口卡/摘要。完整页面应使用各自独立接口。

危机合约摘要：活动名称、最高指标、挑战次数、奖章、周任务/指标任务/关卡任务进度、活动时间、KV 和头图。

影拓丰碑摘要：主题、封面、普通/困难关卡名称和是否通过、活动时间、活动状态。最佳队伍、敌人和详细描述来自独立接口。

战争回响摘要：`seasons[]` 和 `activity`，当前样本只返回 1 个首页赛季摘要；完整赛季列表、轮换、三档关卡、最佳记录、敌人和荣誉来自独立 `card/war-echoes`。

## 5. 危机合约完整数据

`data.crisisContract`：

```text
status
history
indicators[]
dungeon
```

### 5.1 活动状态 `status`

```text
id
name
highest
challengeCount
achieve
weeklyMission.count / total
indicatorMission.count / total
stageMission.count / total
kvImage
headerImage
startAtTs
endAtTs
gameplayEndAtTs
```

`achieve` 使用与普通奖章相同的奖章结构，可显示奖章名、等级、镀层、获得时间和对应图标。

### 5.2 历史记录 `history`

```text
records[]
bestRecord
```

每条记录：

```text
id
chars[]
ts
passTs
isPass
indicatorCount
passWave
isBest
```

记录干员：

```text
charId
level
potentialLevel
avatarUrl
```

列表记录只提供上述字段。使用 `bestRecord.id` 请求记录详情后，`recordDetail.chars[]` 还提供：

```text
weapon.id
weapon.icon
weapon.level
weapon.refineLevel
weapon.weaponTerms[3]
weapon.rarity
equips.bodyEquip
equips.armEquip
equips.firstAccessory
equips.secondAccessory
```

四件装备均包含记录当时的 `id`、`icon`、`enhanceStatus` 和 `rarity`。UI 必须使用这份历史快照，不能用当前个人详情的武器或装备代替。

适合 UI：最高指标、挑战次数、最佳耗时、通过波次、最佳队伍、32 次历史趋势/列表、最佳记录标记。

### 5.3 指标 `indicators[]`

```text
id
icon
name
desc
descParams
hasAward
type
depends[]
openTs
score
isUnlock
unlockScore
```

适合 UI：指标网格/路线图、依赖关系、分值、解锁条件、开放时间、奖励标记、锁定状态。`descParams` 需要模板替换。

### 5.4 关卡与敌人 `dungeon`

```text
id
name
desc
feature
recommendLevel
enemies[]
```

敌人：

```text
id
name
desc
level
imageUrl
ability
```

适合 UI：关卡说明、环境特性、推荐等级、敌人图鉴和能力说明。

当前样本：活动“危机合约 重燃测试作战”，最高指标 44、挑战 32 次、46 个指标、12 种敌人。

## 6. 影拓丰碑完整数据

`data.indieHard.indieHardGroups[]`：

```text
id
name
pic
dungeonGroups[]
activityStartTs
activityEndTs
activityName
achieve
isInActivity
```

当前五个主题：死寂争鸣、浊流具现、灼痛疤痕、无机造物、大地的弃子。

### 6.1 普通/困难关卡

每个 `dungeonGroups[]` 包含：

```text
normalDungeon
hardDungeon
```

两种关卡共享：

```text
id
name
isPass
bestRecord
desc
feature
enemies[]
recommendLevel
```

### 6.2 最佳记录

```text
bestRecord.chars[]
bestRecord.ts
bestRecord.passTs
```

记录干员除 `charId`、等级、潜能、头像外，还可能包含：

```text
evolvePhase
property
rarity
```

### 6.3 主题奖章与敌人

主题 `achieve` 使用通用奖章结构。敌人结构与危机合约相同：名称、描述、等级、图片、能力。

适合 UI：主题横向导航、活动中标记、普通/困难双轨、通关率、最佳时间、最佳队伍、关卡特性、敌人列表、主题奖章。

## 7. 战争回响完整数据

`data.warEchoes`：

```text
seasons[]
achieves[]
activity
```

`seasons[]` 提供 `id/name/kvImage/headerImage/startTs/endTs/stars/allPlusTasks/weeks[]`；`weeks[]` 提供轮换时间、账号星数、附加目标完成状态和 `dungeonGroups[]`。

每个 `dungeonGroups[]` 包含 `name/star/plusTask`，以及：

```text
normalDungeon
hardDungeon
cruelDungeon
```

三档关卡共享 `id/name/desc/feature/recommendLevel/plusTask/additionalChallengeTarget/isPass/firstPassTs/bestRecord/enemies[]`。最佳记录为 `chars[]/ts/passTs`，队伍成员含角色 ID、等级、潜能、头像、属性、稀有度和突破阶段；敌人结构与危机合约相同。

当前账号样本含“谵妄赛季”“追忆赛季”2 个赛季、5 个轮换、15 个关卡轮换项和 45 个难度对象；两季汇总均为 9 星且 `allPlusTasks: true`。`achieves[]` 有 8 条，字段为 `name/star/firstPassTs`。

适合 UI：赛季切换、轮换时间轴、9 星/附加目标状态、普通/困难/残酷三档通关状态、最佳时间和队伍、关卡特性、额外挑战目标、敌人图鉴、荣誉墙。

## 8. 森空岛用户主页数据（可选社交层）

本地留存的用户主页响应包含以下字段，但这部分尚未加入当前终末地查询脚本的稳定客户端。若新 UI 只做游戏资料，可暂不纳入第一版。

### 8.1 用户资料 `user`

```text
id
nickname
profile
avatarCode
avatar
backgroundCode
isCreator
status
operationStatus
identity
kind
latestIpLocation
moderatorStatus
moderatorChangeTime
gender
birthday
hgId
creatorIdentifiers[]
scoreInfoList[]
showId
```

创作者标识 `creatorIdentifiers[]`：

```text
id
name
description
status
applicable
createdAtTs
updatedAtTs
i18nName
i18nDescription
```

游戏社区等级 `scoreInfoList[]`：

```text
gameId
gameName
level
iconUrl
checkedDays
score
levelUrl
```

### 8.2 社交统计与关系

```text
userRts.liked
userRts.collect
userRts.comment
userRts.follow
userRts.fans
userRts.black
userRts.pub

relation.follow
relation.fans
relation.black
relation.blacked
relation.blocked
relation.fansAtTs
```

另有版主状态、背景图和用户处罚列表。`moderator`：

```text
isModerator
operations[]
role
since
status
gameOperations
```

其余结构：

```text
moderator
background.id
background.url
background.resourceKind
userSanctionList[]
```

建议只把昵称、头像、背景、简介、创作者标识、社区等级和公开统计作为可选头部；生日、IP 地区、内部 ID、处罚/拉黑状态不应进入公开图片。

## 9. API 直接提供的视觉资源

可以直接用于新 UI 的远程资源：

- 账号头像。
- 干员方形头像、半身/横向头像、完整立绘。
- 技能、天赋、装备、战术物品、武器、嵌晶图标。
- 成就初始、二阶、三阶、镀层图标。
- 航天器入驻干员头像。
- 地区负责人头像。
- 危机合约 KV、头图、指标图标、队伍头像和敌人图片。
- 影拓丰碑主题图、队伍头像和敌人图片。
- 快捷入口图标。
- 森空岛用户头像、主页背景、社区等级图标。

所有远程图片都必须提供加载失败占位图和尺寸裁切策略，不能让单个 CDN 失败阻断整张卡片。

## 10. 第一版 UI 建议的信息架构

### 页面 A：账号总览

- 头像、角色名、等级、世界等级、主线进度。
- 干员/武器/档案总数。
- 体力、通行证、日常、周常、疑案五项进度。
- 用户展示的最多 8 名干员。
- 当前危机合约、当前影拓丰碑活动入口。
- 当前战争回响赛季、星数和下一轮换入口。
- 两个地区的建设/探索概览。

### 页面 B：干员与配装

- 筛选：稀有度、职业、属性、武器类型、等级、培养状态。
- 列表卡：头像、等级、突破、潜能、武器、四装备槽完成度。
- 详情：立绘、技能等级、三类天赋、四装备槽、套装、战术物品、武器、嵌晶、成长节点。

### 页面 C：成就墙

- 总数、八类分类统计、10 个展示槽。
- 奖章等级、镀层、获得时间和分类筛选。

### 页面 D：地区与航天器

- 地区等级、聚落资金/等级、负责人和结算时间。
- 13 个地图区域的五类收集进度。
- 6 个航天器房间、入驻干员体力与好感度。

### 页面 E：危机合约

- 活动头图、最高指标、挑战次数、任务进度和奖章。
- 最佳记录、历史列表/趋势、队伍。
- 46 个指标的网格或依赖图。
- 关卡特性和敌人图鉴。

### 页面 F：影拓丰碑

- 五主题切换、活动状态、主题奖章。
- 普通/困难双轨、通关状态、最佳时间与队伍。
- 关卡说明、特性和敌人。

### 页面 G：战争回响

- 赛季/轮换切换、赛季星数和附加目标完成状态。
- 普通/困难/残酷三档通关状态、最佳时间与队伍。
- 关卡特性、额外挑战目标、敌人图鉴和荣誉记录。

不建议把全部内容塞进单张长图。总览、干员详情、危机合约、影拓丰碑和战争回响的数据密度足以各自成为独立页面或独立图片模板。

## 11. 必须设计的空状态和边界

- 其他用户可能返回 `crisisContract: null` 或 `indieHard: null`，即接口成功但没有公开记录。
- 战争回响也可能返回 `warEchoes: null`、空赛季或只有首页摘要；UI 必须区分“接口失败”和“当前无公开数据”。
- `serverName`、主线 ID、工厂、套装、战术物品、嵌晶、装备槽、报告可能为空。
- 未培养干员可能等级 1、无装备、无战术物品，但仍有静态技能/天赋资料。
- 公开账号的干员/排名展示受 `config` 开关控制。
- 时间戳是字符串或整数混合，需要统一转换并处理无效值。
- 进度值可能是字符串，需要先转数值并限制在合理范围。
- 动态参数对象的键名不固定，技能/天赋/套装/指标描述必须经过模板渲染。
- 当前样本中共有数百个动态参数键，例如攻击倍率、持续时间、韧性、治疗量和触发概率。这些键属于 `descParams`、`descLevelParams`、`skillDescParams`、`activeEffectParams`、`passiveEffectParams` 或 `params` 的模板变量，不应被设计成固定 UI 字段。
- `roleId`、内部用户 ID、`hgId`、绑定 UID 等不能默认出现在公开图片。
- 当前接口没有完整武器仓库、档案列表、抽卡记录、背包物资、货币余额或战斗面板属性，不应为这些模块伪造数据。

## 12. 推荐优先级

第一版最值得做：

1. 账号总览。
2. 干员列表与单干员配装详情。
3. 危机合约活动页。
4. 影拓丰碑主题页。
5. 战争回响赛季页。

第二版再做：

1. 成就墙。
2. 地区探索与聚落建设。
3. 航天器房间。
4. 森空岛社交头部。

原因：前四项的数据完整度、视觉资源和用户辨识度最高；地区/航天器字段虽然丰富，但枚举与静态映射仍需补齐。
