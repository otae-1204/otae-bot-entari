# 终末地干员持有率展示层接口

本文档只描述展示层接入方式和数据语义。统计、快照、权限与命令已由后端实现；展示层不应重新计算分母、持有率、潜能分布或排序。

## 接入入口

接口位于 `plugins.endfield.ownership_stats`：

```python
from plugins.endfield.ownership_stats import (
    OwnershipStatsReport,
    register_ownership_stats_renderer,
)


async def my_renderer(report: OwnershipStatsReport) -> bytes | tuple[bytes, ...]:
    # 在这里生成图片；不要访问账号接口或绑定身份数据。
    ...


register_ownership_stats_renderer(my_renderer)
```

普通查看命令只调用：

```python
await render_ownership_stats(report)
```

渲染器可以是同步或异步函数。当前命令适配器接受以下返回值：

- 单张 PNG 的 `bytes`；
- 多张 PNG 的 `list[bytes]` 或 `tuple[bytes, ...]`；
- Entari 可直接发送的消息对象或文本。

未注册渲染器时，命令会提示“统计数据已生成，但展示组件尚未接入”，不会使用临时视觉实现。

刷新命令只返回文字批次结果，不构建 `OwnershipStatsReport`，也不会调用渲染器。展示层无需处理刷新结果图片。

## 命令约定

- `/ef 持有率 [群内|全局]`
- `/ef 干员占比 [群内|全局]`
- `/ef 干员统计 [群内|全局]`
- `/ef 持有率 刷新 [群内|全局]`
- `刷新` 和范围参数可交换顺序，例如 `/ef 持有率 全局 刷新`。
- `/zmd` 仍作为兼容命令前缀，但所有提示统一使用 `/ef`。
- 群聊未写范围时默认 `group`，私聊未写范围时默认 `global`。
- 私聊指定群内会直接报错。
- 所有人可查看；群主/群管理员可刷新当前群；`SUPERUSER` 可刷新群内或全局。

## 报告结构

### `OwnershipStatsReport`

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `scope` | `Literal["global", "group"]` | 本次统计范围 |
| `generated_at` | `int` | Unix 秒，报告生成时间 |
| `snapshot_updated_at` | `int \| None` | Unix 秒，有效样本中最新一次完整快照的抓取时间；无有效样本时为 `None` |
| `catalog_version` | `str` | AKEData 目录版本；目录不可用时可能为空 |
| `segments` | `tuple[OwnershipStatsSegment, ...]` | 固定依次包含 `all`、`cn`、`asia` |
| `refresh` | `OwnershipRefreshResult | None` | 为服务层保留的可选批次信息；当前聊天刷新命令直接返回文字，不渲染报告 |

可以用 `report.segment("all")`、`report.segment("cn")`、`report.segment("asia")` 取对应统计段；没有匹配项时返回 `None`。

### `OwnershipStatsSegment`

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `region` | `Literal["all", "cn", "asia"]` | 总计、国服或亚服 |
| `eligible_sample_count` | `int` | 当前仍有绑定的唯一游戏角色数 |
| `valid_sample_count` | `int` | 最近 48 小时内有成功完整快照的样本数，即本段分母 |
| `excluded_sample_count` | `int` | 过期、失败或从未成功快照的样本数 |
| `operators` | `tuple[OperatorOwnership, ...]` | 已按默认规则排序的全目录干员统计 |
| `professions` | `tuple[CollectionSummary, ...]` | 按职业汇总的平均收集率 |
| `rarities` | `tuple[CollectionSummary, ...]` | 按稀有度汇总的平均收集率 |

样本按 `(server_id, role_id)` 去重。一个游戏角色即使被多个 QQ 重复绑定，也只计一个样本。`all`、`cn`、`asia` 各自使用自己的 `valid_sample_count` 作为分母。

### `OperatorOwnership`

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `operator_key` | `str` | 后端归一化后的稳定 MD5 ID；通常等于账号接口 ID，男女管理员使用各自 AKEData `charId` 的 MD5 |
| `source_id` | `str` | AKEData 原始 `charId`；观测补录干员可能为空 |
| `name` | `str` | 中文名；无法识别时为“未知干员” |
| `rarity` | `int` | 稀有度 |
| `profession` | `str` | 中文职业名 |
| `sort_order` | `int` | AKEData 官方顺序 |
| `owned_count` | `int` | 本统计段拥有该干员的有效样本数 |
| `sample_count` | `int` | 本统计段有效样本数，与段的 `valid_sample_count` 一致 |
| `ownership_rate` | `float | None` | `owned_count / sample_count`；空样本时为 `None` |
| `potential_buckets` | `tuple[PotentialBucket, ...]` | 固定顺序的潜能分布 |

默认排序已经由后端完成：稀有度降序、同稀有度持有率降序、AKEData 顺序升序、稳定 ID 升序。展示层应保留此顺序。

账号接口会对男女管理员统一返回 `md5("chr_9000_endmin")`，它是共享占位 ID，不代表女管理员，也不是第三名干员。后端根据档案条目的 `gender`（缺失时使用 `base.gender`）归一化为两个独立条目：男管理员使用 `chr_0002_endminm`，女管理员使用 `chr_0003_endminf`，`operator_key` 分别是各自 `charId` 的 MD5。无法判定性别的旧快照不会参与统计，并会在后台刷新时优先重建。

### `PotentialBucket`

每名干员固定输出 8 个桶，顺序如下：

| `key` | `label` | 含义 |
| --- | --- | --- |
| `unowned` | `未持有` | 有效样本中未拥有该干员 |
| `potential_0` | `潜能 0` | 已拥有，潜能等级为 0 |
| `potential_1` | `潜能 1` | 已拥有，潜能等级为 1 |
| `potential_2` | `潜能 2` | 已拥有，潜能等级为 2 |
| `potential_3` | `潜能 3` | 已拥有，潜能等级为 3 |
| `potential_4` | `潜能 4` | 已拥有，潜能等级为 4 |
| `potential_5` | `潜能 5` | 已拥有，潜能等级为 5 |
| `unknown` | `未知` | 已拥有，但官方数据未给出可识别潜能 |

每个桶还有：

- `count: int`：桶内样本数；
- `rate: float | None`：`count / valid_sample_count`，空样本时为 `None`。

八个桶的 `count` 之和始终等于该统计段的 `valid_sample_count`。所有桶都以全部有效样本为分母，不以“已拥有该干员的人数”为分母。

### `CollectionSummary`

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `kind` | `Literal["profession", "rarity"]` | 汇总维度 |
| `label` | `str` | 职业中文名，或稀有度数字字符串 |
| `operator_count` | `int` | 该组目录干员数 |
| `owned_slots` | `int` | 该组所有“样本-干员”已持有格数 |
| `possible_slots` | `int` | `valid_sample_count * operator_count` |
| `collection_rate` | `float | None` | `owned_slots / possible_slots`；分母为 0 时是 `None` |

### `OwnershipRefreshResult`

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `attempted` | `int` | 本批选中的唯一游戏角色数，包含之后因保护性停止而跳过的角色 |
| `succeeded` | `int` | 成功写入完整快照数 |
| `failed` | `int` | 本次失败数；48 小时内的旧成功快照仍可继续参与 |
| `skipped` | `int` | 因同角色已有任务，或批次触发官方接口保护性停止而跳过的数量 |
| `catalog_updated` | `bool` | 本批是否更新了 AKEData 目录 |
| `started_at` | `int` | Unix 秒，批次开始时间 |
| `finished_at` | `int` | Unix 秒，批次结束时间 |
| `stopped_early` | `bool` | 是否因官方社区接口连续出现系统性错误而提前停止 |
| `stop_reason` | `str` | 提前停止的匿名批次原因；未提前停止时为空字符串 |

刷新优先复用进程内尚未过期的社区签名上下文。缓存签名明确失效时只重新获取一次；冷缓存凭据交换会限速，连续出现 `405`、`429` 或服务端错误时会指数退避并保护性停止剩余任务。保护性停止不会删除成功快照，48 小时内的旧快照仍可参与查看统计。

## 空样本与异常状态

- `valid_sample_count == 0` 时，所有比例字段都是 `None`，不是 `0.0`。
- 新干员即使无人持有，仍会出现在 `operators`；只要分母非零，其持有率为 `0.0`。
- 目录未收录但在亚服快照中出现的干员，会作为观测干员补入，名称、职业或稀有度可能是兜底值。
- 群成员列表获取失败时，命令直接失败，不会回退到全局报告。
- 展示层不应根据 `excluded_sample_count` 猜测或展示任何个人身份。

## 隐私边界

`OwnershipStatsReport` 不包含 QQ、QQ 昵称、游戏 UID、游戏昵称、绑定凭证或任何样本级列表。展示层只应消费报告对象，不应直接读取 `EndfieldStore` 的绑定表或快照表，也不应访问官方账号接口。
