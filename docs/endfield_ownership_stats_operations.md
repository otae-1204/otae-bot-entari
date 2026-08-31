# 终末地持有率刷新运维日志

持有率刷新日志只记录批次、数量、耗时、接口操作和错误码，不记录 QQ、昵称、UID、角色 ID、凭据或官方错误正文。主要前缀为 `[endfield-ownership]` 和 `[endfield-community]`。

## 一次刷新应出现的日志

有待刷新角色时，正常批次至少包含：

```text
[endfield-ownership] refresh batch selected batch_id=... trigger=scheduled force=false input_bindings=... unique_roles=... eligible=... queued=... retry_deferred=... limit_deferred=... limit=20 concurrency=2 lock_wait_seconds=...
[endfield-ownership] refresh batch complete batch_id=... trigger=scheduled force=false eligible=... queued=... requested=... succeeded=... failed=... skipped=... deferred=... stopped_early=false catalog_status=... cache_hits=... singleflight_reuses=... exchange_attempts=... exchange_succeeded=... exchange_failed=... circuit_rejections=... issues=... lock_wait_seconds=... elapsed_seconds=...
```

每 10 分钟的空轮询只写 `DEBUG`，默认生产日志级别下不会刷屏。手动刷新、绑定顺带刷新和有候选角色的定时刷新写 `INFO`；保护性停止写 `WARNING`。

## 字段解释

- `batch_id`：同一批筛选、目录错误、熔断和完成日志的关联 ID。
- `trigger`：`scheduled`、`binding`、`manual-global` 或 `manual-group`。
- `input_bindings`：输入绑定行数；可能包含重复绑定。
- `unique_roles`：按 `(server_id, role_id)` 去重后的游戏角色数。
- `eligible`：当前应刷新总数。
- `queued`：本批进入固定 worker 队列的数量。
- `retry_deferred`：仍处于角色级失败退避的数量。
- `limit_deferred`：超过本批上限、留到后续轮次的数量。
- `requested`：本批调用角色档案请求的次数；回退第二份绑定凭据时可能增加。
- `skipped`：同角色任务占用或熔断后未执行的入队角色。
- `catalog_status`：`updated`、`unchanged`、`failed` 或 `not_checked`。
- `cache_hits`：直接复用有效社区签名上下文的次数。
- `singleflight_reuses`：并发失效时等待另一协程换新后复用的次数。
- `exchange_attempts/succeeded/failed`：实际换取社区签名上下文的批次计数。
- `circuit_rejections`：共享冷却已打开，因此没有再次请求换凭据接口的次数。
- `issues`：匿名分类及数量，例如 `api:获取社区凭据:405:3`、`role-busy:2`。

## 目录和数据库日志

```text
[endfield-ownership] snapshot schema migrated added_columns=failure_count,next_attempt_at,roster_fingerprint
[endfield-ownership] catalog check complete status=unchanged version=... source=manifest elapsed_seconds=...
[endfield-ownership] catalog check complete status=updated version=... operators=... elapsed_seconds=...
[endfield-ownership] catalog check failed batch_id=... trigger=... error_type=...
```

`source=manifest` 表示只读取了轻量版本清单，没有下载三张完整 AKEData 表。生产库首次升级出现一次 `snapshot schema migrated` 属于正常现象。

## 风控和恢复日志

```text
[endfield-community] exchange circuit opened operation=获取社区凭据 code=405 cooldown_seconds=900
[endfield-ownership] refresh circuit opened batch_id=... code=405 matches=3 window=3 cooldown_seconds=900 queue_remaining=...
[endfield-community] exchange circuit recovered previous_operation=获取社区凭据 previous_code=405
```

- `exchange circuit opened`：换凭据公共入口已冷却，缓存仍可继续使用。
- `refresh circuit opened`：当前持有率批次停止消费剩余队列，旧快照不会被删除。
- `exchange circuit recovered`：冷却结束后首次成功换取社区上下文。

## 常见判断

- `cache_hits` 高且 `exchange_attempts` 低：缓存工作正常。
- `exchange_failed > 0` 且出现 `405/429/5xx`：官方接口或风控问题，先看冷却秒数，不要立即反复强刷。
- `circuit_rejections` 高：请求被本地保护挡住，并非每次都实际打到官方。
- `retry_deferred` 高：大量角色处于失败退避；结合 `issues` 判断凭据过期还是官方异常。
- `limit_deferred` 逐轮下降：积压正在按每批 20 个正常消化。
- `role-busy` 偶发：签到、账号查询或其他角色任务撞车，下个 10 分钟轮次会补刷。
- `catalog_status=failed`：目录检查失败；已有目录和 48 小时内旧快照仍保留，但应检查 AKEData 网络。
- `lock_wait_seconds` 长：手动、绑定或定时批次发生重叠，后到批次正在等待并会重新筛选。

排查时可从日志文件中过滤：

```powershell
rg "\[endfield-(ownership|community)\]" <日志文件>
```
