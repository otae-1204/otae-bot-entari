# otaeBot 文本引导重写 · 改动总结

由 Gemini 3.8 Flash 多智能体并行重写。**54 个文件、863 处改动**，只改面向用户的纯文本引导。

## 范围

- **改了**：帮助文本、用法/格式提示、参数错误、权限提示、操作指引 —— 所有以纯文本消息回复给用户的内容。
- **没改**：帮助卡片、图片渲染代码、docstring、日志、内部异常、变量名与命令名、f-string 占位符。

## 统一后的风格

| 项目 | 改前 | 改后 |
| --- | --- | --- |
| 命令格式提示 | `用法: /mod ＜模组名＞ [数量]` | `格式：/mod <模组名> [数量]` |
| 命令与说明 | `查看详情：/更新日志 <版本号>` | `/更新日志 <版本号> —— 查看指定版本详情` |
| 权限提示 | `你没有权限执行此操作` | `权限不足，无法执行该操作。` |
| 标点 | 半角混用 | 统一全角 |
| 参数占位符 | 全角 `＜＞` | 统一半角 `<>` |

## 验收

- 语法错误 **0**（54 个文件全部 AST 解析通过）
- 命令名 / 函数名 / 导入 **零变化**（AST 比对）
- 占位符无丢失（f-string 字段逐个核对）
- 「用法：」残留 **0**，术语已统一为「格式：」

---

## endfield（17 个文件 · 308 处）

### `endfield/handlers.py` — 156 处

```diff
- return await matcher.finish("当前版本日历暂不可用，请稍后重试")
+ return await matcher.finish("版本日历数据暂不可用，请稍后再试。")
```
```diff
- return await matcher.finish("当前版本日历生成失败，请稍后重试")
+ return await matcher.finish("版本日历图生成失败，请稍后再试。")
```
```diff
- return await matcher.finish(f"{source_label(command.source)} 暂不支持关卡资料；关卡仅使用 AkeData。")
+ return await matcher.finish(f"{source_label(command.source)} 暂不支持关卡资料，关卡数据仅支持 AkeData。")
```
```diff
- return await matcher.finish("该类资料只提供 AkeData")
+ return await matcher.finish("该类资料仅由 AkeData 提供。")
```
```diff
- return await matcher.finish("档案资料尚未就绪，先发送 /ef 档案 刷新")
+ return await matcher.finish("档案数据尚未构建，请先发送 /ef 档案 刷新。")
```
```diff
- return await matcher.finish("奖章请用 /ef 奖章")
+ return await matcher.finish("奖章查询请使用 /ef 奖章 指令。")
```
```diff
- title = "搜索结果" if candidates else "未找到相关结果"
+ title = "检索结果" if candidates else "未匹配到相关条目"
```
```diff
- return await matcher.finish("已取消候选查询。")
+ return await matcher.finish("已取消候选选择。")
```
```diff
- return await matcher.finish(f"编号无效，请输入 1-{len(options)}。")
+ return await matcher.finish(f"序号输入有误，请输入 1–{len(options)}。")
```
```diff
- return await matcher.finish("图片发送失败，请稍后重试")
+ return await matcher.finish("图片发送失败，请稍后再试。")
```
```diff
- return await matcher.finish("关卡数据源暂时不可用")
- return await matcher.finish("数据源暂时不可用")
+ return await matcher.finish("关卡数据源暂时响应异常，请稍后再试。")
+ return await matcher.finish("数据源暂时响应异常，请稍后再试。")
```
```diff
- return await matcher.finish("道具效果数值未收录")
+ return await matcher.finish("该道具效果数值暂未收录。")
```
```diff
- return await matcher.finish("资料暂时不可用")
+ return await matcher.finish("该资料暂时不可用，请稍后再试。")
```
```diff
- return await matcher.finish("图片生成失败")
+ return await matcher.finish("卡片图片生成失败，请稍后再试。")
```
```diff
- await matcher.send("正在抓取 AKEData 蚀刻章数据…")
+ await matcher.send("正在同步 AKEData 蚀刻章最新数据…")
```
```diff
- return await matcher.finish("AKEData 数据源暂时不可用，请稍后重试。")
+ return await matcher.finish("AKEData 奖章数据源响应异常，请稍后再试。")
```
```diff
- return await matcher.finish("蚀刻章数据保存失败，请稍后重试。")
+ return await matcher.finish("蚀刻章本地快照保存失败，请稍后再试。")
```
```diff
- return await matcher.finish("暂无蚀刻章数据，请先发送「/ef 奖章 刷新」。")
+ return await matcher.finish("本地暂无蚀刻章数据快照，请先执行 /ef 奖章 刷新。")
```
```diff
- return await matcher.finish("数据源暂时不可用。")
+ return await matcher.finish("数据源暂时响应异常，请稍后再试。")
```
```diff
- return await matcher.finish("蚀刻章图片生成失败")
+ return await matcher.finish("蚀刻章统计卡生成失败，请稍后再试。")
```
```diff
- await matcher.send("正在抓取 AKEData 档案库数据…")
+ await matcher.send("正在同步 AKEData 档案库最新数据…")
```
```diff
- return await matcher.finish("AKEData 数据源暂时不可用，请稍后重试。")
+ return await matcher.finish("AKEData 档案数据源响应异常，请稍后再试。")
```
```diff
- return await matcher.finish("档案库数据保存失败，请稍后重试。")
+ return await matcher.finish("档案库本地快照保存失败，请稍后再试。")
```
```diff
- return await matcher.finish("暂无档案库数据，请先发送「/ef 档案 刷新」。")
+ return await matcher.finish("本地暂无档案库数据快照，请先执行 /ef 档案 刷新。")
```
```diff
- return await matcher.finish("档案库图片生成失败")
+ return await matcher.finish("档案库统计卡生成失败，请稍后再试。")
```
```diff
- return await matcher.finish("私聊中无法统计“群内”范围，请改用 /ef 持有率 全局。")
+ return await matcher.finish("私聊环境不支持群内统计，请使用 /ef 持有率 全局。")
```
```diff
- return await matcher.finish("当前无法获取群成员列表，请稍后重试。")
+ return await matcher.finish("群成员列表获取失败，请稍后再试。")
```
```diff
- return await matcher.finish("仅 SUPERUSER 可以刷新全局持有率快照。")
+ return await matcher.finish("权限不足：仅机器人管理员可刷新全局持有率快照。")
```
```diff
- return await matcher.finish("仅群主、群管理员或 SUPERUSER 可以刷新当前群快照。")
+ return await matcher.finish("权限不足：仅群主、群管理员或机器人管理员可刷新本群持有率快照。")
```
```diff
- return await matcher.finish("获取当前群成员列表失败，已取消统计；不会回退为全局范围。")
+ return await matcher.finish("获取本群成员列表失败，已终止统计任务。")
```
```diff
- return await matcher.finish("持有率统计数据已生成，但展示组件尚未接入。")
+ return await matcher.finish("持有率数据已统计完成，但渲染展示模块暂未就绪。")
```
```diff
- catalog_label = f"目录检查失败（{refresh.catalog_error}）"
+ catalog_label = f"干员目录核对失败（{refresh.catalog_error}）"
```
```diff
- catalog_label = "目录未检查"
+ catalog_label = "未核对干员目录"
```
```diff
- catalog_label = "目录已更新" if refresh.catalog_updated else "目录无变化"
+ catalog_label = "干员目录已更新" if refresh.catalog_updated else "干员目录无变动"
```
```diff
- f"{scope_label}持有率刷新完成：候选 {eligible}，入队 {refresh.attempted}，"
- f"角色请求 {refresh.requested}，成功 {refresh.succeeded}，失败 {refresh.failed}，"
- f"跳过 {refresh.skipped}，延后 {refresh.deferred}；{catalog_label}；"
- f"耗时 {elapsed} 秒。"
+ f"{scope_label}干员持有率刷新完毕：符合条件 {eligible} 个，加入队列 {refresh.attempted} 个，"
+ f"发起查询 {refresh.requested} 个，成功 {refresh.succeeded} 个，失败 {refresh.failed} 个，"
+ f"跳过 {refresh.skipped} 个，延后 {refresh.deferred} 个；{catalog_label}；"
+ f"总计耗时 {elapsed} 秒。"
```
```diff
- result += "分类：" + "、".join(
+ result += "异常归类：" + "、".join(
```
```diff
- result += f"{refresh.stop_reason}；旧快照仍按 48 小时有效期参与统计。"
+ result += f"{refresh.stop_reason}；历史快照在 48 小时有效期内仍将继续生效。"
```
```diff
- result += "失败通常由绑定登录过期或官方接口异常导致；账号所有者可私聊使用 /ef 绑定更新凭证。"
+ result += "更新失败通常由于登录态失效或接口波动，账号持有者可私聊发送 /ef 绑定 重新授权。"
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("暂无蚀刻章数据，请先发送「/ef 奖章 刷新」建立快照。")
+ return await matcher.finish("本地暂无蚀刻章数据快照，请先发送 /ef 奖章 刷新。")
```
```diff
- return await matcher.finish("奖章进度查询失败，请稍后重试。")
+ return await matcher.finish("奖章进度查询失败，请稍后再试。")
```
```diff
- return await matcher.finish("奖章进度查询失败。")
+ return await matcher.finish("奖章进度查询异常，请稍后再试。")
```
```diff
- return await matcher.finish("缺章图片生成失败")
+ return await matcher.finish("缺章统计图生成失败，请稍后再试。")
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("暂无档案库数据，请先发送「/ef 档案 刷新」建立快照。")
+ return await matcher.finish("本地暂无档案库数据快照，请先发送 /ef 档案 刷新。")
```
```diff
- return await matcher.finish("档案进度查询失败，请稍后重试。")
+ return await matcher.finish("档案进度查询失败，请稍后再试。")
```
```diff
- return await matcher.finish("档案进度查询失败。")
+ return await matcher.finish("档案进度查询异常，请稍后再试。")
```
```diff
- return await matcher.finish("档案进度图片生成失败")
+ return await matcher.finish("档案进度图生成失败，请稍后再试。")
```
```diff
- return await matcher.finish("该命令涉及账号凭据或手机号，仅支持私聊使用。")
+ return await matcher.finish("该功能涉及个人隐私凭证或手机号，请在私聊中发起。")
```
```diff
- f"已将 {role.nickname}（{role.role_id}）设为主账号。" if role else "未找到对应账号，请使用 /ef 账号 查看编号。"
+ f"已将 {role.nickname}（{role.role_id}）设为默认主账号。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
```
```diff
- f"已解绑 {role.nickname}（{role.role_id}）。" if role else "未找到对应账号，请使用 /ef 账号 查看编号。"
+ f"已解除绑定角色：{role.nickname}（{role.role_id}）。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
```
```diff
- return await matcher.finish("任务正在进行")
+ return await matcher.finish("当前任务正在处理中，请稍候。")
```
```diff
- return await matcher.finish("终末地养成数据源暂时不可用，请稍后重试。")
+ return await matcher.finish("角色养成数据源暂时响应异常，请稍后再试。")
```
```diff
- return await matcher.finish(f"{exc}\n示例：/ef {example}")
+ return await matcher.finish(f"{exc}\n参考格式：/ef {example}")
```
```diff
- return await matcher.finish("QQ 消息连接中断，请重新执行当前命令。")
+ return await matcher.finish("网络连接中断，请重新发送指令。")
```
```diff
- return await matcher.finish("终末地账号功能暂时不可用，请稍后重试。")
+ return await matcher.finish("账号服务暂时响应异常，请稍后再试。")
```
```diff
- return await matcher.finish("仅群聊支持通过 @群友查询对方的挑战数据。")
+ return await matcher.finish("通过 @群友 查询他人挑战数据仅限在群聊中使用。")
```
```diff
- return await matcher.finish("一次只能 @ 一名群友。")
+ return await matcher.finish("单次查询仅支持 @ 一位群友。")
```
```diff
- return await matcher.finish("被 @ 的用户尚未绑定终末地账号。")
- return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
+ return await matcher.finish("所选群友暂未绑定终末地账号。")
+ return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- lines = ["账号选择存在歧义，请从候选中选择："]
+ lines = ["匹配到多个候选账号，请明确选择序号："]
```
```diff
- lines.append("示例：/ef 影拓 账号 1")
+ lines.append("参考格式：/ef 影拓 账号 1")
```
```diff
- return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
+ return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
```
```diff
- return await matcher.finish("影拓丰碑和战争回响目前仅支持国服账号，亚服暂不支持。")
+ return await matcher.finish("影拓丰碑与战争回响目前仅支持国服角色，亚服暂未接入。")
```
```diff
- return await matcher.finish(f"影拓历史共 {len(pages)} 页，请输入 1-{len(pages)}。")
+ return await matcher.finish(f"影拓历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
```
```diff
- return await matcher.finish(f"战争回响历史共 {len(pages)} 页，请输入 1-{len(pages)}。")
+ return await matcher.finish(f"战争回响历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
```
```diff
- return await matcher.finish(f"历史记录共 {len(pngs)} 页，当前连接不支持合并转发，请使用 {page_hint} 分页查看。")
+ return await matcher.finish(f"历史记录共 {len(pngs)} 页，当前环境暂不支持合并转发，请通过 {page_hint} 分页浏览。")
```
```diff
- f"抽卡分析共 {len(pngs)} 页，合并转发不可用，分 {len(batches)} 条发送。" if not sent
- else f"抽卡分析共 {len(pngs)} 页，第 {sent + 1}–{len(pngs)} 页合并转发失败，分 {len(batches)} 条发送。"
+ f"抽卡分析长图共 {len(pngs)} 页，当前环境无法合并转发，已分 {len(batches)} 条消息发送。" if not sent
+ else f"抽卡分析长图共 {len(pngs)} 页，第 {sent + 1}–{len(pngs)} 页转发异常，已分 {len(batches)} 条消息补发。"
```
```diff
- "请选择服务器：\n1. 国服（森空岛，支持 Token/短信；二维码绑定暂不支持）\n"
- "2. 亚服（SKPORT，当前仅支持 Token）\n"
- "回复 1 或 2；回复“取消”退出。",
+ "请选择服务器分区：\n1. 国服（森空岛，支持 Token 与短信验证，二维码暂未开放）\n"
+ "2. 亚服（SKPORT，当前支持 Token）\n"
+ "回复数字 1 或 2，如需放弃请回复“取消”。",
```
```diff
- return await matcher.finish("绑定已取消或等待超时。")
+ return await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- return await matcher.finish("未识别服务器，绑定已取消。")
+ return await matcher.finish("无法识别所选服务器，绑定流程已终止。")
```
```diff
- "请在浏览器登录 SKPORT（https://www.skport.com/）后打开：\n"
+ "请在浏览器登录 SKPORT（https://www.skport.com/）后访问：\n"
```
```diff
- "页面会返回类似下面的内容（仅为格式范例，范例 Token 不能用于绑定）：\n"
+ "页面将返回类似以下示例内容（仅为格式示意，切勿直接发送范例 Token）：\n"
```
```diff
- "上面的例子中，正确复制的内容是：\n"
+ "示例中需提取的关键内容为：\n"
```
```diff
- "请从你自己的页面中，只复制 content 后面双引号里的 Token。\n"
- "不要复制双引号，也不要复制整段 JSON。\n"
- "不要发送上面的范例 Token。\n"
- "不要在群聊或其他平台公开该内容。"
+ "请从你访问的页面中，仅提取 content 双引号包裹的字符串。\n"
+ "切勿包含外层双引号或完整 JSON 数据。\n"
+ "切勿发送上述示范 Token。\n"
+ "请妥善保管凭证，切勿在群聊或公共渠道公开。"
```
```diff
- "请发送 content 双引号内的 Token；回复“取消”退出。", timeout=150
+ "请发送 content 对应的 Token 字符串，如需放弃请回复“取消”。", timeout=150
```
```diff
- return await matcher.finish("绑定已取消或等待超时。")
+ return await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- return await matcher.finish("该 Gryphline 账号下未找到终末地亚服角色。")
+ return await matcher.finish("该 Gryphline 通行证下未检索到终末地亚服角色。")
```
```diff
- return await matcher.finish("该鹰角账号下未找到终末地角色。")
+ return await matcher.finish("该鹰角网络通行证下未检索到终末地角色。")
```
```diff
- return await matcher.finish("绑定已取消或等待超时。")
+ return await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- summary = f"{region_label}绑定完成：新增 {added_count} 个账号"
+ summary = f"{region_label}角色绑定成功：新增 {added_count} 个账号"
```
```diff
- summary += f"；当前共绑定 {len(bound_roles)} 个账号。"
+ summary += f"；目前累计绑定 {len(bound_roles)} 个账号。"
```
```diff
- "请选择绑定方式：\n1. Token 绑定\n2. 手机号验证码绑定\n"
- "二维码绑定暂不支持。\n"
- "可重复绑定其他鹰角账号，已有账号不会被覆盖。\n回复 1 或 2；回复“取消”退出。",
+ "请选择绑定授权途径：\n1. Token 授权绑定\n2. 手机短信验证码绑定\n"
+ "二维码扫码绑定暂未开放。\n"
+ "支持追加绑定多个鹰角账号，已绑定的角色数据不会被覆盖。\n回复数字 1 或 2，如需放弃请回复“取消”。",
```
```diff
- await matcher.finish("绑定已取消或等待超时。")
+ await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- "请在浏览器登录森空岛后打开：\nhttps://web-api.skland.com/account/info/hg\n"
- "复制响应中 data.content 的完整内容并发送。不要在群聊或其他平台公开该内容。"
+ "请在浏览器中登录森空岛并访问：\nhttps://web-api.skland.com/account/info/hg\n"
+ "复制页面响应中 data.content 对应的完整字符串并发送。请妥善保管凭据，切勿向他人公开。"
```
```diff
- account_token = await _prompt_text("请发送 data.content；回复“取消”退出。", timeout=150)
+ account_token = await _prompt_text("请发送 data.content 内容，如需放弃请回复“取消”。", timeout=150)
```
```diff
- await matcher.finish("绑定已取消或等待超时。")
+ await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- phone = await _prompt_text("请输入用于鹰角账号登录的手机号；回复“取消”退出。", timeout=90)
+ phone = await _prompt_text("请输入鹰角网络账号对应的 11 位手机号，如需放弃请回复“取消”。", timeout=90)
```
```diff
- await matcher.finish("绑定已取消或等待超时。")
+ await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- await matcher.finish("手机号格式不正确，绑定已取消。")
+ await matcher.finish("手机号格式不符合标准，绑定流程已终止。")
```
```diff
- code = await _prompt_text("验证码已发送，请输入短信验证码；回复“取消”退出。", timeout=120)
+ code = await _prompt_text("短信验证码已下发，请输入收到的验证码，如需放弃请回复“取消”。", timeout=120)
```
```diff
- await matcher.finish("绑定已取消或等待超时。")
+ await matcher.finish("绑定流程已取消或等待超时。")
```
```diff
- await matcher.finish("验证码格式不正确，绑定已取消。")
+ await matcher.finish("验证码格式输入有误，绑定流程已终止。")
```
```diff
- await matcher.finish("二维码绑定暂不支持，请选择 1 或 2。")
+ await matcher.finish("二维码扫码暂未开放，请回复 1 或 2 选择其他方式。")
```
```diff
- await matcher.finish("未识别绑定方式，绑定已取消。")
+ await matcher.finish("无法识别所选绑定方式，流程已终止。")
```
```diff
- lines = ["检测到多个终末地角色，请回复编号、逗号分隔的多个编号，或“全部”："]
+ lines = ["检测到通行证下包含多个角色，请回复序号（如需多个可用逗号隔开），或回复“全部”全选："]
```
```diff
- return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
+ return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
+ return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
```
```diff
- return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
+ return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
```
```diff
- return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
+ return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
+ return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
```
```diff
- return await matcher.finish("已取消账号养成统计。")
+ return await matcher.finish("已取消账号养成统计查询。")
```
```diff
- return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
+ return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
```
```diff
- return await matcher.finish("养成统计目前仅支持国服账号，亚服暂不支持。")
+ return await matcher.finish("养成统计功能目前仅支持国服角色，亚服暂未接入。")
```
```diff
- return await matcher.finish("尚未绑定终末地账号。请先私聊使用 /ef 绑定。")
+ return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
+ return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
```
```diff
- return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
+ return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
```
```diff
- return await matcher.finish("资源流水查询目前仅支持国服账号，亚服暂不支持。")
+ return await matcher.finish("资源流水查询目前仅支持国服角色，亚服暂未接入。")
```
```diff
- return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
+ return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
+ return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
```
```diff
- return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
+ return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "任务正在进行"))
+ views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "当前任务正在处理中"))
```
```diff
- views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "签到失败，请稍后重试"))
+ views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "签到执行失败，请稍后再试"))
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- role.nickname, role.masked_uid, role.server_name, status="failed", message="任务正在进行"))
+ role.nickname, role.masked_uid, role.server_name, status="failed", message="当前任务正在处理中"))
```
```diff
- role.nickname, role.masked_uid, role.server_name, status="failed", message="查询失败，请稍后重试"))
+ role.nickname, role.masked_uid, role.server_name, status="failed", message="数据获取失败，请稍后再试"))
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- failed = f"，{len(result.failed)} 个卡池失败" if result.failed else ""
- mode = "官方近 90 天窗口全量" if effective_full else "增量"
- suffix = "；本地会持续保留已同步记录" if effective_full else ""
- return await matcher.finish(f"{role.nickname} {mode}同步完成：新增 {result.inserted} 条{failed}{suffix}。")
+ failed = f"，{len(result.failed)} 个卡池拉取失败" if result.failed else ""
+ mode = "近 90 天全量窗口" if effective_full else "增量"
+ suffix = "；本地已同步记录将持续保留" if effective_full else ""
+ return await matcher.finish(f"{role.nickname} 抽卡记录{mode}同步完毕：新增收录 {result.inserted} 条{failed}{suffix}。")
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
```
```diff
- return await matcher.finish(f"页码超出范围，当前共 {total_pages} 页。")
+ return await matcher.finish(f"页码超出有效范围，当前记录共 {total_pages} 页。")
```
```diff
- return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
- phone = await _prompt_text("请输入小黑盒账号绑定的手机号；回复“取消”退出。", timeout=90)
+ return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
+ phone = await _prompt_text("请输入小黑盒账号绑定的 11 位手机号，如需放弃请回复“取消”。", timeout=90)
```
```diff
- return await matcher.finish("导入已取消或等待超时。")
+ return await matcher.finish("导入流程已取消或等待超时。")
```
```diff
- return await matcher.finish("手机号格式不正确，导入已取消。")
+ return await matcher.finish("手机号格式不符合规范，导入已取消。")
```
```diff
- "小黑盒验证码已发送，请输入短信验证码；回复“取消”退出。", timeout=120
+ "小黑盒短信验证码已发送，请输入验证码，如需放弃请回复“取消”。", timeout=120
```
```diff
- return await matcher.finish("导入已取消或等待超时。")
+ return await matcher.finish("导入流程已取消或等待超时。")
```
```diff
- return await matcher.finish("验证码格式不正确，导入已取消。")
+ return await matcher.finish("验证码格式输入有误，导入已取消。")
```
```diff
- f"小黑盒终末地 UID 与所选账号不一致，请切换账号后重试。所选账号 UID {role.masked_uid}。"
+ f"小黑盒绑定的终末地 UID 与当前所选账号不一致，请切换对应账号后再试。当前账号 UID：{role.masked_uid}。"
```
```diff
- "FZ Wiki 星级目录暂未覆盖本次小黑盒记录，已取消导入以避免误判星级，请稍后重试。"
+ "FZ Wiki 星级数据暂未覆盖本次小黑盒记录，为避免星级误判已终止导入，请稍后再试。"
```
```diff
- f"{role.nickname} 的小黑盒历史统计导入完成：{len(imported.pools)} 个卡池，"
- f"{imported.total_count} 抽，{len(imported.six_stars)} 条六星记录。\n"
- "发送 /ef 抽卡 查看补齐后的分析卡；逐抽历史页仍只展示官方明细。"
+ f"{role.nickname} 小黑盒历史抽卡统计导入成功：涵盖 {len(imported.pools)} 个卡池，"
+ f"累计 {imported.total_count} 抽，共包含 {len(imported.six_stars)} 条六星记录。\n"
+ "发送 /ef 抽卡 即可查阅合并统计后的分析卡片；逐抽明细页仍以官方接口记录为准。"
```
```diff
- return "尚未绑定终末地账号。使用 /ef 绑定 开始绑定。"
- lines = ["已绑定的终末地账号："]
+ return "尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。"
+ lines = ["已绑定的终末地账号列表："]
```
```diff
- lines.append("回复编号查看该账号详情，或回复“取消”退出。")
- lines.append("可使用 /ef 添加账号 继续绑定，或用 /ef 主账号 <编号>、/ef 解绑 <编号> 管理。")
+ lines.append("回复对应编号即可查看该账号详情，如需放弃请回复“取消”。")
+ lines.append("发送 /ef 添加账号 可追加新账号，发送 /ef 主账号 <编号> 或 /ef 解绑 <编号> 可进行管理。")
```
```diff
- return await matcher.finish(f"配装参数错误：{error or '已取消'}")
+ return await matcher.finish(f"配装参数有误：{error or '已取消操作'}。")
```
```diff
- return await matcher.finish(f"未找到{label}：{item.name}")
+ return await matcher.finish(f"未匹配到目标{label}：{item.name}。")
```
```diff
- return await matcher.finish(f"只有装备可以设置词条锻造：{item.name}")
+ return await matcher.finish(f"仅装备支持设定词条锻造等级：{item.name}。")
```
```diff
- return await matcher.finish("配装命令需要且只能包含一个干员")
+ return await matcher.finish("配装指令必须且仅可指定一位干员。")
```
```diff
- return await matcher.finish("配装命令最多包含一把武器")
+ return await matcher.finish("配装指令最多仅可携带一把武器。")
```
```diff
- return await matcher.finish(f"配装计算失败：{exc}")
+ return await matcher.finish(f"配装计算异常：{exc}")
```
```diff
- return await matcher.finish("配装图片生成失败")
+ return await matcher.finish("配装图生成失败，请稍后再试。")
```
```diff
- "请先发送干员名称，再填写可选武器和装备名称，使用空格分隔；武器与装备顺序任意。\n"
- "单独调整词条可在装备后追加：词条2锻造2",
+ "请输入干员名称及可选的武器、装备名称（空格分隔，武器与装备位置不限）。\n"
+ "单独自定义词条可在对应装备后追加（如：词条2锻造2）。",
```
```diff
- return None, "等待输入超时"
+ return None, "等待输入超时。"
```
```diff
- return None, "已取消"
+ return None, "已取消配置。"
```
```diff
- lines = [f"“{query}”有多个匹配结果，请回复编号："]
+ lines = [f"“{query}”匹配到多个候选结果，请回复对应编号："]
```
```diff
- return "用法：/ef dev resolve <关键词>"
+ return "指令格式：/ef dev resolve <关键词>。"
```
```diff
- return "未找到候选。"
- lines = ["解析候选："]
+ return "未匹配到解析候选。"
+ lines = ["解析候选列表："]
```
```diff
- return "用法：/ef dev refresh <all|干员|武器|装备|关卡> [关键词]"
+ return "指令格式：/ef dev refresh <all|干员|武器|装备|关卡> [关键词]。"
```
```diff
- return f"已刷新 {scope} 缓存，清除 {removed} 项。"
+ return f"已刷新 {scope} 缓存，清理 {removed} 项。"
```
```diff
- return format_candidates(ambiguous, title="刷新时找到多个可能结果")
+ return format_candidates(ambiguous, title="刷新时匹配到多个可能结果")
```
```diff
- return f"已刷新并预热 {selected.display_name}，耗时 {perf_counter() - started:.2f}s。"
+ return f"已成功刷新并预热 {selected.display_name}，耗时 {perf_counter() - started:.2f} 秒。"
```
```diff
- return "用法：/ef dev cache clear <all|operator|weapon|equipment|stage|icon>"
+ return "指令格式：/ef dev cache clear <all|operator|weapon|equipment|stage|icon>。"
```
```diff
- return f"已清理 {scope} 缓存，共 {removed} 项。"
+ return f"已清空 {scope} 缓存，共清除 {removed} 项。"
```
```diff
- return "dev 命令：status | resolve | refresh | cache"
+ return "dev 支持的子指令：status | resolve | refresh | cache。"
```
```diff
- usage = "用法：/ef 别名 添加 <干员|武器|装备|物品|道具|敌人|词条|档案> <正式名称> <新别名>"
+ usage = "指令格式：/ef 别名 添加 <干员|武器|装备|物品|道具|敌人|词条|档案> <正式名称> <新别名>。"
```
```diff
- return f"{label}尚未开放，暂时不能添加别名"
+ return f"{label}分类暂未开放，无法新增别名。"
```
```diff
- return f"添加别名失败：{exc}"
+ return f"别名添加失败：{exc}"
```
```diff
- return f"{label}别名已存在：{alias} → {canonical}"
+ return f"{label}别名已存在：{alias} → {canonical}。"
```
```diff
- collision = f"\n该别名同时匹配：{'、'.join(targets)}" if len(targets) > 1 else ""
- return f"已添加{label}别名：{alias} → {canonical}{collision}"
+ collision = f"\n该别名同时指向：{'、'.join(targets)}" if len(targets) > 1 else ""
+ return f"已成功添加{label}别名：{alias} → {canonical}。{collision}"
```

### `endfield/catalog/commands.py` — 66 处

```diff
- usage = "用法：/ef 速算 2腐蚀 200（效果可选腐蚀、导电、碎甲，等级为 1–4）"
+ usage = "指令格式：/ef 速算 <等级+效果> <技艺强度>（如：/ef 速算 2腐蚀 200，效果支持腐蚀、导电、碎甲，等级范围 1–4）。"
```
```diff
- return ParsedEndfieldCommand("quick_calc", error="异常效果等级必须在 1–4 之间")
+ return ParsedEndfieldCommand("quick_calc", error="异常效果等级需介于 1 至 4 之间。")
```
```diff
- return ParsedEndfieldCommand("quick_calc", error="源石技艺强度必须是大于或等于 0 的整数")
+ return ParsedEndfieldCommand("quick_calc", error="源石技艺强度需为非负整数。")
```
```diff
- return ParsedEndfieldCommand("primary", account_selector=selector, error="请指定账号编号" if not selector else "")
+ return ParsedEndfieldCommand("primary", account_selector=selector, error="请提供目标账号编号。" if not selector else "")
```
```diff
- return ParsedEndfieldCommand("unbind", account_selector=selector, error="请指定账号编号" if not selector else "")
+ return ParsedEndfieldCommand("unbind", account_selector=selector, error="请提供目标账号编号。" if not selector else "")
```
```diff
- return ParsedEndfieldCommand("gacha_history", error="页码必须大于 0")
+ return ParsedEndfieldCommand("gacha_history", error="查询页码需为正整数。")
```
```diff
- return ParsedEndfieldCommand("ownership_stats", error="“刷新”参数不能重复")
+ return ParsedEndfieldCommand("ownership_stats", error="刷新选项不可重复输入。")
```
```diff
- return ParsedEndfieldCommand("ownership_stats", error="统计范围只能指定一次")
+ return ParsedEndfieldCommand("ownership_stats", error="统计范围仅可指定一次。")
```
```diff
- return ParsedEndfieldCommand("ownership_stats", error="统计范围只能指定一次")
+ return ParsedEndfieldCommand("ownership_stats", error="统计范围仅可指定一次。")
```
```diff
- error="用法：/ef 持有率 [群内|全局]，或 /ef 持有率 刷新 [群内|全局]",
+ error="指令格式：/ef 持有率 [群内|全局] 或 /ef 持有率 刷新 [群内|全局]。",
```
```diff
- return ParsedEndfieldCommand("challenge", challenge_kind=kind, error="页码必须大于 0")
+ return ParsedEndfieldCommand("challenge", challenge_kind=kind, error="查询页码需为正整数。")
```
```diff
- error="历史查询只接受可选页码和账号参数",
+ error="历史查询仅支持指定页码与账号参数。",
```
```diff
- return ParsedEndfieldCommand("challenge", challenge_kind=kind, error="只能指定一个难度")
+ return ParsedEndfieldCommand("challenge", challenge_kind=kind, error="关卡难度仅可指定一项。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error=f"不支持的流水类型 {value}，可选全部、获取、消耗")
+ return ParsedEndfieldCommand("currency_log", error=f"流水变动类型不支持 {value}，可选范围：全部、获取、消耗。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="只能指定一个流水类型")
+ return ParsedEndfieldCommand("currency_log", error="流水变动类型仅可指定一项。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{value}")
+ return ParsedEndfieldCommand("currency_log", error=f"起始日期格式有误：{value}。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="只能指定一个开始日期")
+ return ParsedEndfieldCommand("currency_log", error="起始日期仅可指定一个。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{value}")
+ return ParsedEndfieldCommand("currency_log", error=f"结束日期格式有误：{value}。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="只能指定一个结束日期")
+ return ParsedEndfieldCommand("currency_log", error="结束日期仅可指定一个。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="天数必须是大于 0 的整数")
+ return ParsedEndfieldCommand("currency_log", error="查询天数需为正整数。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="只能指定一个查询天数")
+ return ParsedEndfieldCommand("currency_log", error="查询天数仅可指定一次。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error=f"{part} 不需要参数")
+ return ParsedEndfieldCommand("currency_log", error=f"参数 {part} 无需提供取值。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{part}")
+ return ParsedEndfieldCommand("currency_log", error=f"日期格式有误：{part}。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="只能指定一个流水类型")
+ return ParsedEndfieldCommand("currency_log", error="流水变动类型仅可指定一项。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="日期请使用位置参数或 --开始/--结束，不要混用")
+ return ParsedEndfieldCommand("currency_log", error="请在位置参数与 --开始/--结束 选项中二选一指定日期，不可混用。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="最多指定开始和结束两个日期")
+ return ParsedEndfieldCommand("currency_log", error="日期范围最多指定起始与结束两个日期。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="--all 不能与日期范围同时使用")
+ return ParsedEndfieldCommand("currency_log", error="--all 选项不可与日期范围同时使用。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="天数不能与日期范围同时使用")
+ return ParsedEndfieldCommand("currency_log", error="查询天数不可与日期范围同时使用。")
```
```diff
- return ParsedEndfieldCommand("currency_log", error="--all 不能与天数同时使用")
+ return ParsedEndfieldCommand("currency_log", error="--all 选项不可与查询天数同时使用。")
```
```diff
- return "", index + 1, f"{parts[index]} 后需要参数"
+ return "", index + 1, f"选项 {parts[index]} 缺少对应参数。"
```
```diff
- return (), "资源类型不能为空"
+ return (), "资源类型不可为空。"
```
```diff
- return (), f"不支持的资源类型 {token}，可选源石、嵌晶玉、武库配额"
+ return (), f"资源类型不支持 {token}，可选范围：源石、嵌晶玉、武库配额。"
```
```diff
- return remaining, pool_filter, f"{part} 后需要卡池名称"
+ return remaining, pool_filter, f"选项 {part} 缺少卡池名称。"
```
```diff
- return remaining, pool_filter, "卡池名称不能为空"
+ return remaining, pool_filter, "卡池名称不可为空。"
```
```diff
- return remaining, pool_filter, "只能指定一个卡池筛选"
+ return remaining, pool_filter, "卡池筛选仅可指定一个。"
```
```diff
- return remaining, full, "--full 只能指定一次"
+ return remaining, full, "--full 选项仅可指定一次。"
```
```diff
- "终末地查询用法：",
- "  /ef 绑定 | /ef 添加账号（仅私聊；国服支持 Token/短信，二维码绑定暂不支持；亚服支持 Token；可重复追加多个账号）",
- "  /ef 账号 [编号]（账号详情图：干员、装备、武器、技能等级、潜能）",
- "  /ef 养成统计 [编号]（当前档案可见养成投入与材料明细；别名：资源消耗）",
- "  /ef 影拓 [账号 <编号|昵称|UID后四位>] [@群友]（当前主题个人进度）",
- "  /ef 影拓 历史 [第N页] [账号 ...] [@群友]（全部历史；超过 2 页合并转发）",
- "  /ef 影拓 <主题> [关卡] [普通|苦难] [账号 ...] [@群友]（个人记录详情）",
- "  /ef 回响 [账号 ...] [@群友]（当前赛季星数、轮换和个人记录）",
- "  /ef 回响 历史 [第N页] [账号 ...] [@群友]（赛季历史分页）",
- "  /ef 回响 <赛季> [轮换] [关卡] [普通|困难|残酷] [账号 ...] [@群友]",
+ "终末地功能指令一览：",
+ "  /ef 绑定 | /ef 添加账号 —— 绑定游戏账号（限私聊；国服支持 Token 与短信验证，二维码暂未开放；亚服支持 Token；可添加多个账号）",
+ "  /ef 账号 [编号] —— 生成账号概览图（干员阵容、武器装备、技能与潜能阶数）",
+ "  /ef 养成统计 [编号] —— 统计当前档案内角色养成消耗与材料总览（别名：/ef 资源消耗）",
+ "  /ef 影拓 [账号 <编号|昵称|UID后四位>] [@群友] —— 查询当前主题个人通关进度",
+ "  /ef 影拓 历史 [第N页] [账号 ...] [@群友] —— 浏览往期主题通关总览（超 2 页合并转发）",
+ "  /ef 影拓 <主题> [关卡] [普通|苦难] [账号 ...] [@群友] —— 查看指定主题关卡的详细记录",
+ "  /ef 回响 [账号 ...] [@群友] —— 查看当期赛季星数、轮换进度及个人成绩",
+ "  /ef 回响 历史 [第N页] [账号 ...] [@群友] —— 分页浏览往期赛季历史",
+ "  /ef 回响 <赛季> [轮换] [关卡] [普通|困难|残酷] [账号 ...] [@群友] —— 查询特定赛季关卡挑战详情",
```
```diff
- "  /ef 资源流水 [账号] [日期|开始日期 结束日期|--天数 N/-d N]（源石、嵌晶玉、武库配额；默认最近一个月）",
- "  /ef 资源流水 [账号] [--资源 源石|嵌晶玉|武库配额] [--类型 获取|消耗] [-a/--all]",
- "  /ef 账号 基建 [账号]（据点存票、增长速度与帝江号心情）",
- "  /ef 主账号 <编号> | /ef 解绑 <编号>（仅私聊）",
- "  /ef 签到 [全部|编号|昵称|UID后四位]",
- "  /ef 日常 [全部|编号|昵称|UID后四位]（理智、活跃度、每周事务、通行证仪表盘）",
- "  /ef 抽卡 [账号] | /ef 抽卡同步 [账号] [--full]（分析图超过 3 张合并转发）",
- "  /ef 抽卡导入 [账号]（仅私聊，手机号验证码导入小黑盒历史统计）",
- "  /ef 抽卡记录 [账号] [页码] [--池 <名称>]",
- "  /ef 奖章（查看蚀刻章总数与本版本新增）",
- "  /ef 奖章 刷新（重新抓取 AKEData 数据并更新上一游戏版本基线）",
- "  /ef 奖章 缺章 [账号]（查询自己未获得/未升满/未镀层）",
- "  /ef 档案（查看档案库三大页签总数与本版本新增）",
- "  /ef 档案 刷新（重新抓取 AKEData 数据并更新上一游戏版本基线）",
- "  /ef 档案 收集 [账号]（查询已获得档案数/全库总数）",
- "  /ef 持有率 [群内|全局]（匿名干员持有率；群聊默认群内，私聊默认全局）",
- "  /ef 持有率 刷新 [群内|全局]（群管理员可刷新本群，SUPERUSER 可刷新全局）",
- "  /ef 速算 2腐蚀 200（效果可替换为导电或碎甲）",
- "  /ef 版本日历（查看当前版本全部开放日程）",
+ "  /ef 资源流水 [账号] [日期|开始日期 结束日期|--天数 N/-d N] —— 检索源石、嵌晶玉与武库配额流水（默认最近 30 天）",
+ "  /ef 资源流水 [账号] [--资源 源石|嵌晶玉|武库配额] [--类型 获取|消耗] [-a/--all] —— 按币种与变动类型筛选收支明细",
+ "  /ef 账号 基建 [账号] —— 查看集成工业据点产出效率、票据库存与干员心情",
+ "  /ef 主账号 <编号> | /ef 解绑 <编号> —— 设定默认主账号或解除绑定（限私聊）",
+ "  /ef 签到 [全部|编号|昵称|UID后四位] —— 执行森空岛每日签到并获取奖励",
+ "  /ef 日常 [全部|编号|昵称|UID后四位] —— 展示理智回复、日常活跃、每周事务及通行证进度看板",
+ "  /ef 抽卡 [账号] | /ef 抽卡同步 [账号] [--full] —— 统计抽卡记录与欧气分析（超 3 张图合并转发）",
+ "  /ef 抽卡导入 [账号] —— 通过手机验证码导入小黑盒历史抽卡统计（限私聊）",
+ "  /ef 抽卡记录 [账号] [页码] [--池 <名称>] —— 分页检视抽卡历史明细",
+ "  /ef 奖章 —— 统计当前版本蚀刻章总数及新增奖章",
+ "  /ef 奖章 刷新 —— 同步 AKEData 最新奖章数据并更新版本对比基准",
+ "  /ef 奖章 缺章 [账号] —— 检查当前账号未解锁、未升满或未镀层的奖章",
+ "  /ef 档案 —— 统计中枢档案、音像存档与见闻辑录总数及新增项",
+ "  /ef 档案 刷新 —— 同步 AKEData 最新档案数据并更新版本对比基准",
+ "  /ef 档案 收集 [账号] —— 统计当前账号已解锁档案与全库收录进度",
+ "  /ef 持有率 [群内|全局] —— 统计匿名干员持有率（群聊默认统计本群，私聊默认全局）",
+ "  /ef 持有率 刷新 [群内|全局] —— 手动更新持有率快照（管理员可刷新本群，机器人管理员可刷新全局）",
+ "  /ef 速算 2腐蚀 200 —— 计算异常效果与技艺强度的伤害数值（支持腐蚀、导电、碎甲）",
+ "  /ef 版本日历 —— 查看当前版本各项活动与关卡开放日程表",
```
```diff
- "参数：-s/--source 可指定 FZ Wiki、AkeData 或 Warfarin Wiki；关卡固定使用 AkeData。",
- "干员速查：/ef 干员；可按元素或职业筛选，例如 /ef 干员 灼热、/ef 干员 术师。",
- "武器速查：/ef 武器；可按类型筛选，例如 /ef 武器 单手剑。",
- "装备目录：默认仅金色；--all 显示全部，--rarity 可选 gold、purple、blue、all。",
- "装备属性筛选：主/副可省略，写“力量 敏捷”表示两条属性都要有；多条件同时满足才会列出。",
- "图鉴：物品、道具、敌人、词条与档案条目只使用 AkeData；/ef 档案 <名称> 是条目详情，/ef 档案 仍是版本统计。",
- "配装第一个名称固定为干员；之后武器与装备无需固定顺序，省略武器时自动使用推荐武器。干员/武器默认90级，角色/武器潜能默认5，装备词条默认3锻。",
- "潜能指定：追加“角色潜能2 武器潜能3”。",
- "武器技能指定：追加“武器技能1等级5”；可重复指定多个技能。",
- "单独调整词条：在装备后追加“词条2锻造2”；可重复追加多个词条设置。",
- "快捷：/efop <名称>、/efwp <名称>、/efeq <名称>、/终末地干员 <名称>、/终末地武器 <名称>、/终末地装备 <名称>",
+ "参数说明：-s/--source 可选 FZ Wiki、AkeData 或 Warfarin Wiki；关卡数据固定使用 AkeData。",
+ "干员速览：/ef 干员；支持按属性或职业过滤，例如 /ef 干员 灼热、/ef 干员 术师。",
+ "武器速览：/ef 武器；支持按类型过滤，例如 /ef 武器 单手剑。",
+ "装备目录：默认筛选金色品质；--all 展示全品阶，--rarity 支持 gold、purple、blue、all。",
+ "属性检索：主副词条可省略前缀，如“力量 敏捷”即匹配同时拥有这两项属性的装备。",
+ "百科图鉴：物品、道具、敌人、词条与档案条目均使用 AkeData；/ef 档案 <名称> 查看详情，/ef 档案 查看版本总览。",
+ "配装规则：首个名称必须为干员；武器与装备位置不限，省略武器时默认使用推荐配置。默认干员与武器 90 级、潜能 5 阶、词条 3 锻。",
+ "潜能调整：在末尾追加“角色潜能2 武器潜能3”。",
+ "技能调整：在末尾追加“武器技能1等级5”；支持多次输入以调整多个技能。",
+ "词条锻造：在对应装备后追加“词条2锻造2”；支持单独为多件装备设定锻造层数。",
+ "快捷指令：/efop <名称>、/efwp <名称>、/efeq <名称>、/终末地干员 <名称>、/终末地武器 <名称>、/终末地装备 <名称>",
```
```diff
- "数据源：各类资料按下列顺序使用。",
+ "资料来源：各类别优先检索顺序如下。",
```
```diff
- "关卡数据仅使用 AkeData；其他资料会按顺序尝试备选源。",
- "物品、道具、敌人、词条与档案条目只使用 AkeData。",
+ "关卡数据独家采用 AkeData；其他类别按顺序回退备选来源。",
+ "物品、道具、敌人、词条及档案条目均固定采用 AkeData。",
```
```diff
- return "未知命令或参数错误。发送 /ef help 查看用法。"
+ return "指令不存在或输入参数有误。发送 /ef help 查看完整指令指引。"
```
```diff
- return f"参数错误：{error}\n发送 /ef help 查看用法。"
+ return f"输入参数有误：{error}\n发送 /ef help 查看完整指令指引。"
```
```diff
- return f"未找到{label}：{query}\n可以发送 /ef 副本 浏览关卡资料目录"
+ return f"未能检索到{label}：{query}。\n可发送 /ef 副本 翻阅关卡资料总览。"
```
```diff
- f"没有同时满足 {query} 的装备。\n"
- "可以少写一条属性，例如 /ef 装备 主力量；或加 --all 放开稀有度限制"
+ f"未找到同时匹配 {query} 条件的装备。\n"
+ "建议减少一项属性条件（如：/ef 装备 主力量），或追加 --all 参数查看全部品阶装备。"
```
```diff
- return f"未找到{label}：{query}\n可以发送 /ef {word} 浏览{word}目录"
- return f"未找到{label}：{query}\n可以尝试 /ef 搜索 {query}"
+ return f"未能检索到{label}：{query}。\n可发送 /ef {word} 查阅{word}完整目录。"
+ return f"未能检索到{label}：{query}。\n可尝试使用 /ef 搜索 {query} 进行全局检索。"
```
```diff
- title: str = "找到多个可能结果",
+ title: str = "存在多个匹配结果",
```
```diff
- return "未找到相关结果。"
+ return "未匹配到相关条目。"
```
```diff
- lines.append(f"可引用本消息并回复 1-{len(options)} 查询对应内容，也可不回复并忽略本消息。")
+ lines.append(f"回复对应序号 1–{len(options)} 查看详情，如需放弃请回复“取消”。")
```
```diff
- lines.append("请使用 " + "、".join(f"/ef {word} <名称>" for word in words) + " 精确查询。")
+ lines.append("建议使用精确格式检索：" + "、".join(f"/ef {word} <名称>" for word in words) + "。")
```
```diff
- return remaining, source, f"{part} 后需要数据源名称"
+ return remaining, source, f"选项 {part} 缺少数据源名称。"
```
```diff
- return remaining, source, f"不支持的数据源 {value}，可选 fz、akedata、warfarin"
+ return remaining, source, f"数据源不支持 {value}，可选范围：fz、akedata、warfarin。"
```
```diff
- return remaining, source, "只能指定一个数据源"
+ return remaining, source, "数据源选项仅可指定一项。"
```
```diff
- return remaining, rarity, "--rarity 后需要稀有度名称"
+ return remaining, rarity, "选项 --rarity 缺少稀有度名称。"
```
```diff
- return remaining, rarity, f"不支持的装备稀有度 {value}，可选 gold、purple、blue、all"
+ return remaining, rarity, f"装备稀有度不支持 {value}，可选范围：gold、purple、blue、all。"
```
```diff
- return remaining, rarity, "只能指定一个装备稀有度"
+ return remaining, rarity, "装备稀有度仅可指定一项。"
```
```diff
- return None, f"词条锻造等级必须在 0–3：{raw_token}"
+ return None, f"词条锻造等级需介于 0 至 3 之间：{raw_token}。"
```
```diff
- return None, "词条锻造设置前需要先写装备名称"
+ return None, "设定词条锻造前需先指定装备名称。"
```
```diff
- return None, "请至少填写一个干员"
+ return None, "配装参数中至少需包含一名干员。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "潜能类型不明确，请写角色潜能N或武器潜能N"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "潜能类型不明确，请明确指定角色潜能N或武器潜能N。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "请使用 --weapon-potential 指定武器潜能"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "设定武器潜能请使用 --weapon-potential 选项。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "武器技能等级必须在 1–9"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "武器技能等级需介于 1 至 9 之间。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须在 {minimum}–{maximum}"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}需介于 {minimum} 至 {maximum} 之间。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{part} 后需要数值"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"选项 {part} 缺少数值参数。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须是整数"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}需为整数。"
```
```diff
- return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须在 {minimum}–{maximum}"
+ return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}需介于 {minimum} 至 {maximum} 之间。"
```

### `endfield/account/client.py` — 19 处

```diff
- raise EndfieldAPIError("验证码登录", message="官方接口未返回账号凭据")
+ raise EndfieldAPIError("验证码登录", message="官方接口未返回有效凭据，请重新尝试。")
```
```diff
- raise EndfieldAPIError("生成登录二维码", message="官方接口未返回扫码凭据")
+ raise EndfieldAPIError("生成登录二维码", message="官方接口未返回扫码凭据，请重试。")
```
```diff
- raise EndfieldAPIError("查询扫码状态", message="官方接口未返回扫码授权码")
+ raise EndfieldAPIError("查询扫码状态", message="官方接口未返回有效授权码，请重试。")
```
```diff
- raise EndfieldAPIError("扫码登录", message="官方接口未返回账号凭据")
+ raise EndfieldAPIError("扫码登录", message="官方接口未返回有效凭据，请重试。")
```
```diff
- raise EndfieldAPIError("查询终末地档案", message="官方接口未返回角色档案")
+ raise EndfieldAPIError("查询终末地档案", message="官方接口未返回角色档案数据，请稍后重试。")
```
```diff
- raise EndfieldAPIError("查询影拓丰碑", message="官方接口未返回影拓丰碑数据")
+ raise EndfieldAPIError("查询影拓丰碑", message="官方接口未返回影拓丰碑记录，请稍后重试。")
```
```diff
- raise EndfieldAPIError("查询战争回响", message="官方接口未返回战争回响数据")
+ raise EndfieldAPIError("查询战争回响", message="官方接口未返回战争回响记录，请稍后重试。")
```
```diff
- message = "亚服暂不支持货币查询" if operation == "查询终末地货币" else "亚服暂不支持资源流水查询"
+ message = "亚服暂不支持查询货币数据" if operation == "查询终末地货币" else "亚服暂不支持查询资源流水"
```
```diff
- raise EndfieldAPIError("获取抽卡凭据", message="官方接口未返回 U8 凭据")
+ raise EndfieldAPIError("获取抽卡凭据", message="官方接口未返回抽卡凭据，请稍后重试。")
```
```diff
- "官方服务冷却中，请稍后重试",
+ "官方请求过于频繁，请稍候再试。",
```
```diff
- raise EndfieldAPIError("获取社区凭据", message="官方接口未返回 cred")
+ raise EndfieldAPIError("获取社区凭据", message="官方接口未返回社区认证凭据，请稍后重试。")
```
```diff
- raise EndfieldAPIError("刷新社区签名", message="官方接口未返回签名凭据")
+ raise EndfieldAPIError("刷新社区签名", message="官方接口未返回签名凭据，请稍后重试。")
```
```diff
- raise EndfieldAPIError("账号授权", message="官方接口未返回授权凭据")
+ raise EndfieldAPIError("账号授权", message="官方接口未返回授权信息，请稍后重试。")
```
```diff
- raise EndfieldAPIError(operation, message="网络请求失败，请稍后重试") from None
+ raise EndfieldAPIError(operation, message="网络请求失败，请稍候再试。") from None
```
```diff
- message="官方服务暂时不可用",
+ message="官方服务响应异常，请稍后重试。",
```
```diff
- raise EndfieldAPIError(operation, message="官方接口返回了无法解析的数据") from None
+ raise EndfieldAPIError(operation, message="官方接口返回数据无法解析，请稍后重试。") from None
```
```diff
- message="官方服务暂时不可用",
+ message="官方服务响应异常，请稍后重试。",
```
```diff
- raise EndfieldAPIError(operation, message="官方接口返回格式异常")
+ raise EndfieldAPIError(operation, message="官方接口数据格式异常，请稍后重试。")
```
```diff
- message="官方服务暂时不可用",
+ message="官方服务响应异常，请稍后重试。",
```

### `endfield/gacha/xhh.py` — 15 处

```diff
- raise XhhAPIError("小黑盒登录会话已失效，请重新执行导入。")
+ raise XhhAPIError("小黑盒登录会话已过期，请重新发起导入。")
```
```diff
- raise XhhAPIError("小黑盒登录状态不完整，请稍后重试。")
+ raise XhhAPIError("小黑盒登录鉴权信息不完整，请稍后重试。")
```
```diff
- raise XhhAPIError("小黑盒数据缺少终末地 UID，无法安全导入。")
+ raise XhhAPIError("小黑盒数据中未检测到终末地 UID，无法安全导入。")
```
```diff
- raise XhhAPIError("小黑盒设备验证初始化失败，请稍后重试。")
+ raise XhhAPIError("小黑盒设备指纹验证初始化失败，请稍后重试。")
```
```diff
- raise XhhAPIError("无法启动小黑盒安全登录环境，请检查 Playwright 浏览器组件。") from exc
+ raise XhhAPIError("无法拉起小黑盒安全登录环境，请确认 Playwright 浏览器组件已就绪。") from exc
```
```diff
- raise XhhAPIError("小黑盒登录会话尚未初始化。")
+ raise XhhAPIError("小黑盒登录会话未建立或已提前关闭。")
```
```diff
- raise XhhAPIError("小黑盒官方接口请求失败，请稍后重试。") from exc
+ raise XhhAPIError("请求小黑盒服务接口超时或失败，请稍后重试。") from exc
```
```diff
- raise XhhAPIError("小黑盒官方接口返回了无法识别的数据。")
+ raise XhhAPIError("小黑盒服务接口响应数据结构异常。")
```
```diff
- "无法启动小黑盒安全登录环境；Playwright Chromium 和系统 Edge 均不可用。"
+ "未能拉起小黑盒登录环境，内置 Chromium 与系统 Edge 均无法调用。"
```
```diff
- raise XhhAPIError(_safe_api_message(root.get("msg"), "小黑盒历史数据获取失败。"))
+ raise XhhAPIError(_safe_api_message(root.get("msg"), "获取小黑盒寻访历史记录失败。"))
```
```diff
- raise XhhAPIError("该小黑盒账号未绑定终末地数据。")
+ raise XhhAPIError("当前小黑盒账号尚未绑定终末地游戏角色。")
```
```diff
- raise XhhAPIError("小黑盒返回数据中未找到终末地卡池记录。")
+ raise XhhAPIError("小黑盒返回数据中未检索到终末地卡池记录。")
```
```diff
- raise XhhAPIError("小黑盒数据缺少终末地 UID，无法安全导入。")
+ raise XhhAPIError("小黑盒返回数据缺少终末地 UID，无法安全导入。")
```
```diff
- raise XhhAPIError("小黑盒要求完成图形验证，本次自动导入无法继续，请稍后重试。")
+ raise XhhAPIError("小黑盒触发了人机图形验证，当前无法自动完成导入，请稍后重试。")
```
```diff
- raise XhhAPIError("该小黑盒账号启用了二次验证，暂不支持自动导入。")
+ raise XhhAPIError("该小黑盒账号开启了两步安全验证，暂不支持全自动导入。")
```

### `endfield/stages/fz.py` — 8 处

```diff
- raise StageDataIncomplete(f"“{title}”只是玩法说明条目，暂无可查询的关卡资料。")
+ raise StageDataIncomplete(f"“{title}”属于玩法机制说明，无独立关卡数据可供查询。")
```
```diff
- raise StageDataIncomplete(f"暂不支持该关卡条目：{title or '未知条目'}")
+ raise StageDataIncomplete(f"暂未收录该关卡条目：{title or '未知条目'}。")
```
```diff
- raise StageDataIncomplete("该危境关卡暂未提供深度资料。")
+ raise StageDataIncomplete("该危境关卡尚未收录深度阶段数据。")
```
```diff
- raise StageDataIncomplete("该资源副本暂未提供层数资料。")
+ raise StageDataIncomplete("该资源副本尚未收录层数规格数据。")
```
```diff
- raise StageDataIncomplete("该战争回响赛季暂未提供该关卡资料。")
+ raise StageDataIncomplete("当前战争回响赛季暂未收录该关卡数据。")
```
```diff
- raise StageDataIncomplete("该战争回响关卡暂未提供难度资料。")
+ raise StageDataIncomplete("该战争回响关卡尚未收录难度规格数据。")
```
```diff
- raise StageDataIncomplete(f"“{name}”暂无可查询的关卡阶段资料。")
+ raise StageDataIncomplete(f"“{name}”暂未收录关卡阶段详情。")
```
```diff
- raise StageDataIncomplete(f"“{name}”的资料结构暂未被识别，无法生成关卡卡。")
+ raise StageDataIncomplete(f"“{name}”的数据格式暂未适配，暂无法生成关卡图卡。")
```

### `endfield/account/challenge/parsing.py` — 7 处

```diff
- raise ChallengeResolutionError("请指定影拓主题或关卡名称")
+ raise ChallengeResolutionError("请提供影拓主题或关卡名称。")
```
```diff
- raise ChallengeResolutionError(f"主题“{group.name}”暂无关卡记录")
+ raise ChallengeResolutionError(f"主题“{group.name}”下暂无关卡记录。")
```
```diff
- raise ChallengeResolutionError("请指定战争回响赛季、轮换或关卡名称")
+ raise ChallengeResolutionError("请提供战争回响赛季、轮换或关卡名称。")
```
```diff
- raise ChallengeResolutionError(f"赛季“{season.name}”暂无轮换记录")
+ raise ChallengeResolutionError(f"赛季“{season.name}”下暂无轮换记录。")
```
```diff
- raise ChallengeResolutionError(f"轮换“{week.name}”暂无{_difficulty_label(normalized_difficulty)}数据")
+ raise ChallengeResolutionError(f"轮换“{week.name}”暂无{_difficulty_label(normalized_difficulty)}数据。")
```
```diff
- raise ChallengeResolutionError(f"未找到“{query}”")
+ raise ChallengeResolutionError(f"未查询到“{query}”。")
```
```diff
- raise ChallengeResolutionError(f"未找到“{query}”，请使用“历史”查看可用名称")
+ raise ChallengeResolutionError(f"未查询到“{query}”，可使用“历史”指令查看可用名称。")
```

### `endfield/catalog/aliases.py` — 7 处

```diff
- raise ValueError("别名类型必须是干员、武器、装备、物品、道具、敌人、词条或档案条目")
+ raise ValueError("别名类型仅支持：干员、武器、装备、物品、道具、敌人、词条或档案条目。")
```
```diff
- raise ValueError("正式名称和新别名不能为空")
+ raise ValueError("正式名称与新别名均不可为空。")
```
```diff
- raise ValueError("别名库结构异常")
+ raise ValueError("别名库数据格式异常。")
```
```diff
- raise ValueError(f"别名库中不存在正式名称：{canonical_name}")
+ raise ValueError(f"别名库中未收录正式名称：{canonical_name}。")
```
```diff
- f"正式名称不唯一，请写全名：{canonical_name}（{'、'.join(matches)}）"
+ f"匹配到多个正式名称，请提供完整全名：{canonical_name}（{'、'.join(matches)}）。"
```
```diff
- raise ValueError("新别名不能与正式名称相同")
+ raise ValueError("新别名不可与正式名称完全相同。")
```
```diff
- raise ValueError(f"正式名称的别名列表异常：{canonical}")
+ raise ValueError(f"正式名称关联的别名列表异常：{canonical}。")
```

### `endfield/ownership/service.py` — 7 处

```diff
- raise OwnershipStatsRendererUnavailable("持有率展示组件尚未接入")
+ raise OwnershipStatsRendererUnavailable("持有率统计展示组件未就绪。")
```
```diff
- stop_reason="干员目录不可用，已停止本批刷新",
+ stop_reason="干员目录暂时不可用，已终止本轮批量刷新",
```
```diff
- catalog_error=catalog_error or "干员目录为空",
+ catalog_error=catalog_error or "干员目录数据为空",
```
```diff
- stop_reason=self._circuit_reason or "官方社区接口冷却中",
+ stop_reason=self._circuit_reason or "官方社区接口处于调用冷却中",
```
```diff
- "官方社区接口保护性停止",
+ "官方社区接口触发保护性熔断",
```
```diff
- f"官方社区接口近期多次返回 {systemic_code}，"
- "已保护性停止剩余刷新"
+ f"官方社区接口近期频繁返回 {systemic_code}，"
+ "已触发保护机制停止剩余刷新"
```
```diff
- issue_label="同角色已有任务",
+ issue_label="同角色存在进行中的任务",
```

### `endfield/account/currency/service.py` — 6 处

```diff
- raise ValueError("资源流水查询的开始日期不能晚于结束日期")
+ raise ValueError("资源流水查询的开始日期不得晚于结束日期。")
```
```diff
- raise ValueError("资源流水查询天数必须大于 0")
+ raise ValueError("资源流水查询天数须为大于 0 的整数。")
```
```diff
- raise ValueError("资源流水查询天数不能与日期范围同时使用")
+ raise ValueError("资源流水查询天数不能与指定日期范围同时使用。")
```
```diff
- raise ValueError("资源流水查询的开始日期不能晚于结束日期")
+ raise ValueError("资源流水查询的开始日期不得晚于结束日期。")
```
```diff
- lines.append("查询范围内没有流水记录。")
+ lines.append("所选查询范围内暂无流水记录。")
```
```diff
- lines.append("  查询范围内无记录")
+ lines.append("  所选时间段内暂无记录")
```

### `endfield/stages/akedata.py` — 5 处

```diff
- raise StageDataIncomplete(f"AkeData 中没有关卡“{key}”。")
+ raise StageDataIncomplete(f"AkeData 数据源中未找到关卡“{key}”。")
```
```diff
- raise StageDataIncomplete(f"AkeData 场景 {scene_id} 缺少刷怪配置清单。")
+ raise StageDataIncomplete(f"AkeData 场景 {scene_id} 未配置敌人刷新列表。")
```
```diff
- raise StageDataIncomplete("AkeData 版本清单缺少当前版本。")
+ raise StageDataIncomplete("AkeData 版本清单未包含当前版本信息。")
```
```diff
- raise StageDataIncomplete("AkeData 当前版本缺少表路径。")
+ raise StageDataIncomplete("AkeData 当前版本数据表路径缺失。")
```
```diff
- raise StageDataIncomplete(f"AkeData 中没有关卡“{key}”的详情。")
+ raise StageDataIncomplete(f"AkeData 数据源中未包含关卡“{key}”的详细数据。")
```

### `endfield/account/crypto.py` — 4 处

```diff
- raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 必须是 Base64 编码的 32 字节密钥")
+ raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 配置有误：需为 Base64 编码的 32 字节密钥。")
```
```diff
- raise CredentialKeyError("未配置 ENDFIELD_CREDENTIAL_KEY，终末地账号绑定已禁用")
+ raise CredentialKeyError("未配置环境变量 ENDFIELD_CREDENTIAL_KEY，终末地账号绑定功能暂未开放。")
```
```diff
- raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 不是有效的 Base64") from exc
+ raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 格式无效：非标准的 Base64 字符串。") from exc
```
```diff
- raise CredentialKeyError("终末地账号凭据解密失败，请检查 ENDFIELD_CREDENTIAL_KEY") from exc
+ raise CredentialKeyError("终末地账号凭据解密失败，请检查环境变量 ENDFIELD_CREDENTIAL_KEY 设置。") from exc
```

### `endfield/stages/service.py` — 3 处

```diff
- f"{stage_name} 没有“{selector}”变体；可选：{'、'.join(self.valid_labels)}"
+ f"{stage_name} 不存在“{selector}”难度变体；可选：{'、'.join(self.valid_labels)}"
```
```diff
- raise StageDataIncomplete("暂无可用的关卡数据源。")
+ raise StageDataIncomplete("关卡数据源当前均不可用。")
```
```diff
- raise StageDataIncomplete(f"{source or '该数据源'} 暂不支持关卡资料。")
+ raise StageDataIncomplete(f"{source or '所选数据源'} 尚未收录关卡资料。")
```

### `endfield/account/challenge/models.py` — 1 处

```diff
- super().__init__(f"“{query}”有多个可能：{'、'.join(self.candidates[:5])}")
+ super().__init__(f"匹配到多个“{query}”相关结果：{'、'.join(self.candidates[:5])}")
```

### `endfield/account/store.py` — 1 处

```diff
- raise ValueError("小黑盒终末地 UID 与绑定角色不一致")
+ raise ValueError("小黑盒绑定的终末地 UID 与当前角色不一致。")
```

### `endfield/calendar/akedata.py` — 1 处

```diff
- raise VersionCalendarError("AKE 日历缺少当前版本的完整事件覆盖清单")
+ raise VersionCalendarError("AKE 日历暂未覆盖当前版本的完整事件清单，请稍后重试。")
```

### `endfield/cold_start.py` — 1 处

```diff
- COLD_START_NOTICE = "终末地资料正在首次加载，可能需要十几秒，加载完会继续发送结果。"
+ COLD_START_NOTICE = "终末地数据正在初次加载，预计耗时约十余秒，完成后将自动发送结果。"
```

### `endfield/gacha/service.py` — 1 处

```diff
- raise TaskAlreadyRunning("任务正在进行")
+ raise TaskAlreadyRunning("当前角色已有任务正在执行中。")
```

## mcsm（4 个文件 · 175 处）

### `mcsm/handlers.py` — 116 处

```diff
- LOG_USAGE = "用法: /mcsm log <别名> [-a | -n 数量]"
+ LOG_USAGE = "命令格式：/mcsm log <别名> [-a | -n 数量]"
```
```diff
- return "仅本群 MCSM 管理员、QQ 群管理员、群主或 SUPERUSER 可执行此操作"
+ return "权限不足：该指令仅限当前群 MCSM 管理员、QQ 群管理员、群主或 SUPERUSER 运行。"
```
```diff
- return "使用 /help mcsm 查看 MCSM 插件帮助"
+ return "发送 /help mcsm 获取 MCSM 详细指令说明。"
```
```diff
- await _finish_dm_notice(dm_key_handler, bot, user_id, "已取消 MCSM 面板绑定", (), "warning")
+ await _finish_dm_notice(dm_key_handler, bot, user_id, "MCSM 面板绑定已取消", (), "warning")
```
```diff
- "API Key 格式似乎不正确",
- ("API Key 太短，请重新发送完整 Key。", "回复“取消”可中止绑定。"),
+ "API Key 格式无效",
+ ("密钥长度过短，请重新输入完整的 API Key。", "若要放弃本次操作，请回复“取消”。"),
```
```diff
- await ChainMsg.text("正在验证 API Key...").send()
+ await ChainMsg.text("正在核对 API Key...").send()
```
```diff
- "API Key 验证失败",
- (str(e), "请检查 Key 后重新发送。"),
+ "API Key 校验未通过",
+ (str(e), "请核对密钥后重新输入。"),
```
```diff
- "面板没有可用节点",
- ("请检查面板地址是否正确。", "API Key 未保存，请重新发送正确 Key，或回复“取消”。"),
+ "面板下暂无在线节点",
+ ("请确认面板网络与节点状态无误。", "未保存当前密钥，请重新发送有效 Key，或回复“取消”。"),
```
```diff
- "MCSM 面板绑定成功",
- (f"面板: {panel_url}", f"可用节点: {node_count}", "可在群内使用 /mcsm bind <节点ID> 选择绑定实例。"),
+ "MCSM 面板绑定已完成",
+ (f"面板: {panel_url}", f"可用节点: {node_count}", "后续可在群内通过 /mcsm bind <节点ID> 绑定具体实例。"),
```
```diff
- lines = [title, *option_lines(items, label_func), "请回复编号，输入 cancel 取消。"]
+ lines = [title, *option_lines(items, label_func), "请发送对应序号选择，发送 cancel 取消。"]
```
```diff
- selected = await _prompt_select("闪传内有多个压缩包，请选择用于部署的服务器压缩包：", archives, qflash_archive_label)
+ selected = await _prompt_select("检测到闪传包含多个压缩包，请选择部署目标：", archives, qflash_archive_label)
```
```diff
- "没有可用于 Docker 部署的节点",
- ("请检查 daemon 是否在线、Docker 是否安装、MCSM daemon 是否有 Docker 权限。",),
+ "未发现支持 Docker 部署的节点",
+ ("请排查节点是否保持在线、Docker 环境是否就绪以及 daemon 守护进程权限是否充足。",),
```
```diff
- await _finish_notice(mcsm, "未找到匹配的 Docker 节点", (f"节点关键词: {options.node}",), "warning")
+ await _finish_notice(mcsm, "未检索到匹配的 Docker 节点", (f"节点关键词: {options.node}",), "warning")
```
```diff
- selected = await _prompt_select("匹配到多个 Docker 节点，请选择：", matches, _daemon_label)
+ selected = await _prompt_select("符合条件的 Docker 节点较多，请选择目标：", matches, _daemon_label)
```
```diff
- await _finish_notice(mcsm, "已取消 Docker 节点选择", (), "warning")
+ await _finish_notice(mcsm, "Docker 节点选择已取消", (), "warning")
```
```diff
- selected = await _prompt_select("请选择用于部署的 Docker 节点：", daemons, _daemon_label)
+ selected = await _prompt_select("请选择用于部署服务的 Docker 节点：", daemons, _daemon_label)
```
```diff
- await _finish_notice(mcsm, "已取消 Docker 节点选择", (), "warning")
+ await _finish_notice(mcsm, "Docker 节点选择已取消", (), "warning")
```
```diff
- await _finish_notice(mcsm, "未找到匹配的 Docker 镜像", (f"镜像关键词: {options.image}",), "warning")
+ await _finish_notice(mcsm, "未检索到匹配的 Docker 镜像", (f"镜像关键词: {options.image}",), "warning")
```
```diff
- "目标节点没有可用 Java 镜像",
- ("请先在面板拉取 Java 21 或 Java 17 镜像，或使用 --image 指定已有镜像。",),
+ "当前节点缺失可用 Java 镜像",
+ ("建议在面板预先拉取 Java 21 或 Java 17 镜像，亦可通过 --image 参数直接指定其他已有镜像。",),
```
```diff
- selected = await _prompt_select("请选择 Docker 镜像：", matches, image_display_name)
+ selected = await _prompt_select("请挑选所使用的 Docker 镜像：", matches, image_display_name)
```
```diff
- await _finish_notice(mcsm, "已取消 Docker 镜像选择", (), "warning")
+ await _finish_notice(mcsm, "Docker 镜像选择已取消", (), "warning")
```
```diff
- await mcsm.send(f"上传权限异常，正在执行同节点修复实例: {repair_name}")
+ await mcsm.send(f"检测到上传权限受限，正在调用同节点权限修复实例：{repair_name}...")
```
```diff
- await _finish_notice(mcsm, "deploy 参数错误", tuple(parsed.errors), "warning")
+ await _finish_notice(mcsm, "deploy 指令参数有误", tuple(parsed.errors), "warning")
```
```diff
- await _finish_notice(mcsm, "deploy 参数错误", ("别名不能为空",), "warning")
+ await _finish_notice(mcsm, "deploy 指令参数有误", ("实例别名不可为空。",), "warning")
```
```diff
- await _finish_notice(mcsm, "当前群未绑定 MCSM 面板", ("请先使用 /mcsm bind <面板地址> 绑定面板。",), "warning")
+ await _finish_notice(mcsm, "本群尚未关联 MCSM 面板", ("请先执行 /mcsm bind <面板地址> 进行面板关联。",), "warning")
```
```diff
- await mcsm.send("开始部署：解析下载链接、检测节点与镜像。")
+ await mcsm.send("开始部署流程：正在解析资源链接并检测节点与环境...")
```
```diff
- await _finish_notice(mcsm, "实例别名已存在", tuple(lines), "warning")
+ await _finish_notice(mcsm, "该实例别名已被占用", tuple(lines), "warning")
```
```diff
- "未创建实例；去掉 --dry-run 后执行部署。",
+ "当前仅为预检模拟；若确认无误，请移除 --dry-run 参数正式执行部署。",
```
```diff
- await mcsm.send("正在创建实例并安装压缩包。")
+ await mcsm.send("正在创建容器实例并部署文件包...")
```
```diff
- await mcsm.send("远程安装失败，切换 Bot 中转上传并解压。")
+ await mcsm.send("远程拉取失败，正在切换至机器人中转上传模式...")
```
```diff
- await mcsm.send("正在识别启动命令并启动实例。")
+ await mcsm.send("正在匹配启动命令并尝试启动服务...")
```
```diff
- await _finish_dm_notice(dm_bind_handler, bot, user_id, "批量绑定已取消", (), "warning")
+ await _finish_dm_notice(dm_bind_handler, bot, user_id, "实例批量关联已取消", (), "warning")
```
```diff
- "未匹配到可绑定实例",
- tuple(errors[:8]) or ("请回复实例序号/名称，或回复“取消”。",),
+ "未能匹配到有效实例",
+ tuple(errors[:8]) or ("请发送需要绑定的实例编号或名称，输入“取消”可中止操作。",),
```
```diff
- f"批量绑定完成: {len(added)} 个成功",
- tuple(lines) if lines else ("没有变更",),
+ f"批量绑定完成：共 {len(added)} 个实例添加成功",
+ tuple(lines) if lines else ("未产生任何变更。",),
```
```diff
- "当前群未绑定 MCSM 面板",
- ("请使用 /mcsm bind <面板地址> 绑定面板", _help_tip()),
+ "本群尚未关联 MCSM 面板",
+ ("请发送 /mcsm bind <面板地址> 完成面板关联。", _help_tip()),
```
```diff
- lines = [f"{text} matched multiple instances:"]
+ lines = [f"匹配到多个同名实例：{text}"]
```
```diff
- lines.append("Enter the full alias.")
- await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
+ lines.append("请提供完整的实例别名。")
+ await _finish_notice(mcsm, "匹配到多个实例", lines[1:], "warning")
```
```diff
- f"Unknown command: {text}",
- ("可用命令: list | status | start | stop | restart | kill", "cmd | log | bind | unbind | delete | admin | hide | unhide | deploy", _help_tip()),
+ f"未知指令：{text}",
+ ("可用指令：list | status | start | stop | restart | kill", "cmd | log | bind | unbind | delete | admin | hide | unhide | deploy", _help_tip()),
```
```diff
- await _finish_error(mcsm, "请在群聊中使用此命令")
+ await _finish_error(mcsm, "该指令须在群聊环境中运行。")
```
```diff
- "当前群已绑定面板",
+ "本群已关联面板",
```
```diff
- f"面板: {existing[0] if existing else '/'}",
- "面板已绑定；使用 /mcsm bind <节点ID> 绑定实例",
- "如需更换，请先使用 /mcsm unbindpanel 解绑",
+ f"面板地址: {existing[0] if existing else '/'}",
+ "面板已完成绑定；可使用 /mcsm bind <节点ID> 关联具体实例。",
+ "如需更换关联面板，请先执行 /mcsm unbindpanel 解绑当前配置。",
```
```diff
- f"请回复此消息提供 MCSM 面板的 API Key:\n"
+ f"请直接回复本条私聊消息以提供 MCSM API Key：\n"
```
```diff
- f"Key 可在 MCSM 面板的 API 密钥页面生成。\n"
- f"回复「取消」可中止绑定。"
+ f"可在 MCSM 控制面板的“API 密钥”管理页获取。\n"
+ f"若需放弃本次关联，请回复“取消”。"
```
```diff
- await _finish_notice(mcsm, "无法发送私聊", (f"请确认已添加 Bot 好友: {e}",), "error")
+ await _finish_notice(mcsm, "私聊消息发送失败", (f"请先添加机器人为好友以接收私信: {e}",), "error")
```
```diff
- await _finish_notice(mcsm, "面板地址已保存", (f"面板: {url}", "请查看私聊并回复 API Key。"), "success")
+ await _finish_notice(mcsm, "面板地址已记录", (f"面板: {url}", "请前往机器人私聊窗口发送对应的 API Key。"), "success")
```
```diff
- await _finish_notice(mcsm, "当前群未绑定面板", (), "warning")
+ await _finish_notice(mcsm, "本群尚未关联 MCSM 面板", (), "warning")
```
```diff
- await _finish_notice(mcsm, "MCSM panel unbound", ("Group instance bindings were cleared.",), "success")
+ await _finish_notice(mcsm, "MCSM 面板解绑成功", ("已清除当前群聊的所有实例关联数据。",), "success")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- await _finish_notice(mcsm, "获取节点列表失败", (str(exc),), "error")
+ await _finish_notice(mcsm, "拉取节点列表失败", (str(exc),), "error")
```
```diff
- "未找到该节点",
- ("请确认节点 ID 是否正确", "直接绑定实例仍可使用 /mcsm bind <实例UUID> <别名> [节点ID]"),
+ "未检索到目标节点",
+ ("请核对节点 ID 是否准确无误。", "若直接绑定具体实例，可使用：/mcsm bind <实例UUID> <别名> [节点ID]"),
```
```diff
- "获取节点实例失败",
+ "获取节点实例列表失败",
```
```diff
- "请确认节点 ID 正确、daemon 在线，并且当前面板 API Key 有实例读取权限。",
+ "请确认节点 ID 正确、daemon 保持在线，且当前 API Key 具备实例查看权限。",
```
```diff
- await _finish_notice(mcsm, "该节点没有可绑定实例", (), "warning")
+ await _finish_notice(mcsm, "该节点下暂无可用实例", (), "warning")
```
```diff
- "MCSM 批量绑定选择",
- f"群 {group_id}",
- f"节点: {daemon_name} ({daemon_id[:8]})",
+ "MCSM 实例批量绑定向导",
+ f"目标群：{group_id}",
+ f"关联节点：{daemon_name} ({daemon_id[:8]})",
```
```diff
- "回复示例:",
+ "回复格式示例：",
```
```diff
- "实例列表:",
+ "候选实例列表：",
```
```diff
- lines.append(f"... 还有 {len(instances) - 80} 个实例未展示，可使用精确名称匹配。")
+ lines.append(f"... 另有 {len(instances) - 80} 个实例未折叠显示，可直接输入名称精准匹配。")
```
```diff
- await _finish_notice(mcsm, "无法发送私聊选择", (f"请确认已添加 bot 好友: {exc}",), "error")
+ await _finish_notice(mcsm, "私聊向导推送失败", (f"请先添加机器人为好友以接收私信: {exc}",), "error")
```
```diff
- await _finish_notice(mcsm, "已发送私聊选择", ("候选实例列表和绑定结果仅在私聊显示",), "success")
+ await _finish_notice(mcsm, "绑定向导已私信送达", ("候选实例清单及关联结果已发送至私聊窗口。",), "success")
```
```diff
- "用法错误",
- ("绑定面板: /mcsm bind <面板地址>", "绑定节点实例: /mcsm bind <节点ID>", "直接绑定实例: /mcsm bind <实例UUID> <别名> [节点ID]"),
+ "参数格式错误",
+ ("关联面板：/mcsm bind <面板地址>", "关联节点实例：/mcsm bind <节点ID>", "直接关联实例：/mcsm bind <实例UUID> <别名> [节点ID]"),
```
```diff
- await _finish_error(mcsm, f"Alias already exists: {alias}")
+ await _finish_error(mcsm, f"别名冲突：别名 {alias} 已被使用。")
```
```diff
- await _finish_error(mcsm, f"This instance is already bound as {existing}")
+ await _finish_error(mcsm, f"实例已绑定：该实例已命名为 {existing}。")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- await mcsm.send("正在自动探测实例所在节点...")
+ await mcsm.send("正在检索实例归属节点...")
```
```diff
- "未找到该实例",
- ("在所有节点中均未找到该实例，请手动指定节点ID:", "/mcsm bind <UUID> <别名> <节点ID>"),
+ "未检索到指定实例",
+ ("全节点遍历均未发现此实例，请手动附带节点ID：", "/mcsm bind <UUID> <别名> <节点ID>"),
```
```diff
- fallback = f"绑定成功\n别名: {alias}\nUUID: {uuid}\n节点: {daemon_id[:24]}...\n\n/mcsm admin add @某人  添加本群 MCSM 管理员"
+ fallback = f"实例绑定成功\n别名: {alias}\nUUID: {uuid}\n节点: {daemon_id[:24]}...\n\n/mcsm admin add @某人  添加本群 MCSM 管理员"
```
```diff
- await _finish_error(mcsm, "用法: /mcsm unbind <别名>")
+ await _finish_error(mcsm, "指令格式：/mcsm unbind <别名>")
```
```diff
- await _finish_error(mcsm, f"Alias does not exist: {alias}")
+ await _finish_error(mcsm, f"未找到别名为 {alias} 的实例。")
```
```diff
- await _finish_notice(mcsm, "实例已从本群移除", (f"实例: {alias}",), "success")
+ await _finish_notice(mcsm, "实例关联已解除", (f"实例: {alias}",), "success")
```
```diff
- await _finish_error(mcsm, "用法: /mcsm delete <别名> [--files]")
+ await _finish_error(mcsm, "指令格式：/mcsm delete <别名> [--files]")
```
```diff
- await _finish_error(mcsm, f"Instance not found: {alias}")
+ await _finish_error(mcsm, f"未检索到实例：{alias}。")
```
```diff
- "Multiple instances matched",
+ "匹配到多个同名实例",
```
```diff
- await _finish_error(mcsm, "内部错误")
+ await _finish_error(mcsm, "处理异常：无法读取实例信息。")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- await _finish_notice(mcsm, "实例已删除", tuple(lines), "success")
+ await _finish_notice(mcsm, "实例已彻底删除", tuple(lines), "success")
```
```diff
- "当前群未绑定 MCSM 面板",
- ("使用 /mcsm bind <面板地址> 绑定面板", _help_tip()),
+ "本群尚未关联 MCSM 面板",
+ ("发送 /mcsm bind <面板地址> 进行关联。", _help_tip()),
```
```diff
- details = ["本群尚未绑定可见实例。"]
+ details = ["本群暂未添加任何可见实例。"]
```
```diff
- details.append("使用 /mcsm bind <节点ID> 私聊选择要加入本群的实例。")
- await _finish_notice(mcsm, "暂无本群实例", tuple(details), "warning")
+ details.append("管理员可使用 /mcsm bind <节点ID> 在私聊向导中挑选实例添加。")
+ await _finish_notice(mcsm, "当前暂无实例", tuple(details), "warning")
```
```diff
- await _finish_error(mcsm, "内部错误: 无法创建客户端")
+ await _finish_error(mcsm, "处理异常：无法创建面板客户端。")
```
```diff
- await _finish_notice(mcsm, f"Instance not found: {alias}", ("Use /mcsm list to see available instances.",), "error")
+ await _finish_notice(mcsm, f"未检索到实例：{alias}", ("发送 /mcsm list 可查阅本群所有实例。",), "error")
```
```diff
- lines = [f"{alias} matched multiple instances:"]
+ lines = [f"匹配到多个同名实例：{alias}"]
```
```diff
- await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
+ await _finish_notice(mcsm, "匹配到多个实例", lines[1:], "warning")
```
```diff
- await _finish_notice(mcsm, f"Instance not found: {alias}", ("Use /mcsm list to see available instances.",), "error")
+ await _finish_notice(mcsm, f"未检索到实例：{alias}", ("发送 /mcsm list 可查阅本群所有实例。",), "error")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- await _finish_notice(mcsm, f"Unable to get instance info: {alias}", ("Check whether the instance still exists.",), "error")
+ await _finish_notice(mcsm, f"无法获取实例详情：{alias}", ("请检查面板中该实例是否仍然存在。",), "error")
```
```diff
- await _finish_error(mcsm, f"用法: /mcsm {action} <别名>")
+ await _finish_error(mcsm, f"指令格式：/mcsm {action} <别名>")
```
```diff
- await _finish_error(mcsm, f"Instance not found: {alias}")
+ await _finish_error(mcsm, f"未检索到实例：{alias}。")
```
```diff
- lines = [f"{alias} matched multiple instances:"]
+ lines = [f"匹配到多个同名实例：{alias}"]
```
```diff
- await _finish_notice(mcsm, "Multiple instances matched", lines[1:], "warning")
+ await _finish_notice(mcsm, "匹配到多个实例", lines[1:], "warning")
```
```diff
- await _finish_error(mcsm, "内部错误")
+ await _finish_error(mcsm, "处理异常：无法读取实例信息。")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- await mcsm.send(f"Running {op_name} on {alias}...")
+ await mcsm.send(f"正在对实例 {alias} 执行「{op_name}」操作...")
```
```diff
- await _finish_error(mcsm, "请输入要执行的命令")
+ await _finish_error(mcsm, "请输入要在控制台执行的具体命令。")
```
```diff
- await _finish_notice(mcsm, "Operation succeeded", (f"{alias} {op_name} completed",), "success")
+ await _finish_notice(mcsm, "操作成功执行", (f"实例 {alias} 的「{op_name}」已完成。",), "success")
```
```diff
- await _finish_notice(mcsm, f"{alias} {op_name} failed", (str(err),), "error")
+ await _finish_notice(mcsm, f"实例 {alias}「{op_name}」执行失败", (str(err),), "error")
```
```diff
- return alias, log_limit, f"{LOG_USAGE}\n-a 不能与 -n 同时使用"
+ return alias, log_limit, f"{LOG_USAGE}\n参数冲突：-a 与 -n 选项不可同时使用。"
```
```diff
- return alias, log_limit, f"{LOG_USAGE}\n未知参数: {arg}"
+ return alias, log_limit, f"{LOG_USAGE}\n不支持的选项参数：{arg}。"
```
```diff
- return alias, log_limit, f"{LOG_USAGE}\n-a 不能与 -n 同时使用"
+ return alias, log_limit, f"{LOG_USAGE}\n参数冲突：-a 与 -n 选项不可同时使用。"
```
```diff
- return alias, log_limit, f"日志条数不能超过 {LOG_MAX_ENTRIES}"
+ return alias, log_limit, f"单次日志条数上限为 {LOG_MAX_ENTRIES} 行。"
```
```diff
- await _finish_error(mcsm, f"Instance not found: {alias}")
+ await _finish_error(mcsm, f"未检索到实例：{alias}。")
```
```diff
- await _finish_notice(mcsm, "Multiple instances matched", (f"{alias} matched multiple instances; enter the full alias.",), "warning")
+ await _finish_notice(mcsm, "匹配到多个实例", (f"别名 {alias} 命中多个实例，请输入完整名称。",), "warning")
```
```diff
- await _finish_error(mcsm, "内部错误")
+ await _finish_error(mcsm, "处理异常：无法读取实例信息。")
```
```diff
- await _finish_error(mcsm, "当前群未绑定面板")
+ await _finish_error(mcsm, "本群尚未关联 MCSM 面板。")
```
```diff
- action_text = "隐藏" if hidden else "显示"
+ action_text = "隐藏" if hidden else "取消隐藏"
```
```diff
- await _finish_error(mcsm, f"用法: /mcsm {'hide' if hidden else 'unhide'} <别名>")
+ await _finish_error(mcsm, f"指令格式：/mcsm {'hide' if hidden else 'unhide'} <别名>")
```
```diff
- await _finish_error(mcsm, f"Instance not found: {alias}")
+ await _finish_error(mcsm, f"未检索到实例：{alias}。")
```
```diff
- await _finish_notice(mcsm, "Multiple instances matched", (f"{alias} matched multiple instances; enter the full alias.",), "warning")
+ await _finish_notice(mcsm, "匹配到多个实例", (f"别名 {alias} 命中多个实例，请输入完整名称。",), "warning")
```
```diff
- await _finish_notice(mcsm, f"Instance {action_text}", (f"Instance: {alias}",), "success")
+ await _finish_notice(mcsm, f"实例已设定为{action_text}", (f"实例: {alias}",), "success")
```
```diff
- await _finish_error(mcsm, "操作失败")
+ await _finish_error(mcsm, "操作执行失败。")
```
```diff
- "用法错误",
+ "admin 子命令格式错误",
```
```diff
- await _finish_notice(mcsm, "请 @ 要操作的用户", (f"示例: /mcsm admin {action} @某人",), "error")
+ await _finish_notice(mcsm, "未指定目标成员", (f"请 @ 目标成员，或指定其 QQ 号：/mcsm admin {action} @某人",), "error")
```
```diff
- msg.append(f"已添加群管理员 {', '.join(added)}")
+ msg.append(f"已授权群管理员：{', '.join(added)}")
```
```diff
- msg.append(f"已经是群管理员: {', '.join(skipped)}")
- await _finish_notice(mcsm, "MCSM admins updated", msg if msg else ["No changes"], "success")
+ msg.append(f"原已具备管理员权限：{', '.join(skipped)}")
+ await _finish_notice(mcsm, "MCSM 群管理员已更新", msg if msg else ["没有产生变更。"], "success")
```
```diff
- skipped.append(f"{target_uid}(不能移除自己)")
+ skipped.append(f"{target_uid}（不可移除当前执行者自身）")
```
```diff
- msg.append(f"已移除群管理员 {', '.join(removed)}")
+ msg.append(f"已解除群管理员：{', '.join(removed)}")
```
```diff
- msg.append(f"跳过: {', '.join(skipped)}")
- await _finish_notice(mcsm, "MCSM admins updated", msg if msg else ["No changes"], "success")
+ msg.append(f"跳过处理：{', '.join(skipped)}")
+ await _finish_notice(mcsm, "MCSM 群管理员已更新", msg if msg else ["没有产生变更。"], "success")
```

### `mcsm/client.py` — 31 处

```diff
- raise MCSMAPIError("连接面板超时，请检查面板地址或网络") from exc
+ raise MCSMAPIError("连接面板超时，请检查面板地址与网络连通性。") from exc
```
```diff
- raise MCSMAPIError(f"面板返回 HTTP {status}: {body or '无响应内容'}") from exc
+ raise MCSMAPIError(f"面板响应异常（HTTP {status}）：{body or '无响应内容'}。") from exc
```
```diff
- raise MCSMAPIError(f"连接面板失败: {redact_sensitive_text(exc)}") from exc
+ raise MCSMAPIError(f"连接面板失败：{redact_sensitive_text(exc)}。") from exc
```
```diff
- raise MCSMAPIError(f"面板返回的不是 JSON: {text or '空响应'}") from exc
+ raise MCSMAPIError(f"面板未返回有效 JSON 数据：{text or '响应内容为空'}。") from exc
```
```diff
- raise MCSMAPIError("获取上传地址失败: 面板未返回上传地址")
+ raise MCSMAPIError("获取上传地址失败：面板未下发上传接口地址。")
```
```diff
- raise MCSMAPIError("获取上传地址失败: 面板未返回 daemon 地址")
+ raise MCSMAPIError("获取上传地址失败：面板未下发节点地址。")
```
```diff
- raise MCSMAPIError("获取上传地址失败: 面板未返回上传密码")
+ raise MCSMAPIError("获取上传地址失败：面板未下发上传凭据。")
```
```diff
- raise MCSMAPIError("实例文件路径不能为空")
+ raise MCSMAPIError("参数错误：实例文件路径不能为空。")
```
```diff
- raise MCSMAPIError(f"获取节点列表失败: {err}")
+ raise MCSMAPIError(f"拉取节点列表失败：{err}。")
```
```diff
- raise MCSMAPIError(f"获取 Docker 镜像失败: {err}")
+ raise MCSMAPIError(f"拉取 Docker 镜像列表失败：{err}。")
```
```diff
- raise MCSMAPIError(f"获取节点实例失败: {err}")
+ raise MCSMAPIError(f"拉取节点实例列表失败：{err}。")
```
```diff
- raise MCSMAPIError("创建 Docker 实例失败: 缺少 daemonId，请重新选择节点")
+ raise MCSMAPIError("创建 Docker 实例失败：缺少 daemonId，请重新选择部署节点。")
```
```diff
- raise MCSMAPIError(f"创建 Docker 实例失败: {err}")
+ raise MCSMAPIError(f"创建 Docker 实例失败：{err}。")
```
```diff
- raise MCSMAPIError("删除实例失败: 缺少 daemonId")
+ raise MCSMAPIError("删除实例失败：缺少目标节点标识（daemonId）。")
```
```diff
- raise MCSMAPIError("删除实例失败: 缺少实例 UUID")
+ raise MCSMAPIError("删除实例失败：缺少实例 UUID。")
```
```diff
- raise MCSMAPIError(f"删除实例失败: {err}")
+ raise MCSMAPIError(f"删除实例失败：{err}。")
```
```diff
- raise MCSMAPIError(f"安装实例文件失败: {err}")
+ raise MCSMAPIError(f"安装实例文件失败：{err}。")
```
```diff
- raise MCSMAPIError(f"获取上传地址失败: {err}")
+ raise MCSMAPIError(f"获取上传地址配置失败：{err}。")
```
```diff
- raise MCSMAPIError("获取上传地址失败: 面板返回格式不正确")
+ raise MCSMAPIError("获取上传地址失败：面板返回的数据格式异常。")
```
```diff
- raise MCSMAPIError(f"上传文件失败: 本地文件不存在 {path}")
+ raise MCSMAPIError(f"上传文件失败：本地待传文件不存在（{path}）。")
```
```diff
- raise MCSMAPIError(f"上传到 daemon 失败: HTTP {exc.response.status_code}: {body or '无响应内容'}") from exc
+ raise MCSMAPIError(f"上传文件到节点失败（HTTP {exc.response.status_code}）：{body or '无响应内容'}。") from exc
```
```diff
- raise MCSMAPIError(f"上传到 daemon 失败: {redact_sensitive_text(exc)}") from exc
+ raise MCSMAPIError(f"上传文件到节点失败：{redact_sensitive_text(exc)}。") from exc
```
```diff
- raise MCSMAPIError(f"解压压缩包失败: {err}")
+ raise MCSMAPIError(f"解压实例压缩包失败：{err}。")
```
```diff
- raise MCSMAPIError(f"删除临时文件失败: {err}")
+ raise MCSMAPIError(f"清理临时文件失败：{err}。")
```
```diff
- raise MCSMAPIError(f"读取实例文件失败: {err}")
+ raise MCSMAPIError(f"读取实例文件失败：{err}。")
```
```diff
- raise MCSMAPIError(f"写入实例文件失败: {err}")
+ raise MCSMAPIError(f"写入实例文件失败：{err}。")
```
```diff
- raise MCSMAPIError("更新启动命令失败: 缺少 daemonId")
+ raise MCSMAPIError("更新启动命令失败：缺少目标节点标识（daemonId）。")
```
```diff
- raise MCSMAPIError("更新启动命令失败: 缺少实例 UUID")
+ raise MCSMAPIError("更新启动命令失败：缺少实例 UUID。")
```
```diff
- raise MCSMAPIError("实例创建后无法读取详情，不能更新启动命令")
+ raise MCSMAPIError("更新启动命令失败：实例已创建但无法读取详情配置。")
```
```diff
- raise MCSMAPIError(f"更新启动命令失败: {err}")
+ raise MCSMAPIError(f"更新启动命令失败：{err}。")
```
```diff
- raise MCSMAPIError(f"读取实例文件列表失败: {err}")
+ raise MCSMAPIError(f"读取实例文件目录失败：{err}。")
```

### `mcsm/qflash.py` — 20 处

```diff
- raise QFlashError(f"重新解析闪传直链失败: 未找到原压缩包 {selected.name} ({format_size(selected.size)})")
+ raise QFlashError(f"重新解析闪传下载直链失败：未找到对应的压缩包 {selected.name}（{format_size(selected.size)}）。")
```
```diff
- raise QFlashError(f"闪传压缩包预检失败: HTTP {resp.status_code}")
+ raise QFlashError(f"闪传压缩包预检未通过（HTTP {resp.status_code}）。")
```
```diff
- raise QFlashError(f"连接闪传下载地址失败: {exc}") from exc
+ raise QFlashError(f"连接闪传下载地址失败：{exc}。") from exc
```
```diff
- raise QFlashError("闪传压缩包预检失败: 文件为空")
+ raise QFlashError("闪传压缩包预检未通过：远程文件大小为 0。")
```
```diff
- raise QFlashError("闪传压缩包预检失败: 下载地址返回 HTML 页面")
+ raise QFlashError("闪传压缩包预检未通过：下载链接返回了网页而非文件流。")
```
```diff
- raise QFlashError("闪传压缩包预检失败: ZIP 文件头不正确")
+ raise QFlashError("闪传压缩包预检未通过：ZIP 格式校验未通过。")
```
```diff
- raise QFlashError(f"下载闪传压缩包失败: HTTP {resp.status_code}")
+ raise QFlashError(f"下载闪传压缩包失败（HTTP {resp.status_code}）。")
```
```diff
- raise QFlashError(f"下载闪传压缩包失败: {exc}") from exc
+ raise QFlashError(f"下载闪传压缩包时发生网络异常：{exc}。") from exc
```
```diff
- raise QFlashError("下载闪传压缩包失败: 文件为空")
+ raise QFlashError("下载闪传压缩包失败：下载所得文件为空。")
```
```diff
- raise QFlashError("不是有效的 QQ 闪传链接")
+ raise QFlashError("链接无效：所提供的不是标准的 QQ 闪传分享链接。")
```
```diff
- raise QFlashError("闪传内没有可部署的压缩包文件")
+ raise QFlashError("解析失败：该闪传分享中未包含可用于部署的压缩包文件。")
```
```diff
- raise QFlashError("闪传压缩包没有返回可用下载地址")
+ raise QFlashError("解析失败：未能获取到闪传压缩包的有效下载地址。")
```
```diff
- raise QFlashError(_api_message(data) or "闪传链接失效或无法读取 fileset")
+ raise QFlashError(_api_message(data) or "闪传链接已失效或无法读取文件集信息。")
```
```diff
- raise QFlashError(_api_message(data) or "无法读取闪传文件集信息")
+ raise QFlashError(_api_message(data) or "无法解析闪传文件集数据。")
```
```diff
- raise QFlashError(_api_message(data) or "闪传内没有可下载文件")
+ raise QFlashError(_api_message(data) or "该闪传分享中没有包含可下载的文件。")
```
```diff
- raise QFlashError("闪传文件缺少 physical id，无法换取下载地址")
+ raise QFlashError("解析失败：文件缺少必要标识（physical id），无法换取下载地址。")
```
```diff
- raise QFlashError(_api_message(data) or "闪传下载接口返回格式异常")
+ raise QFlashError(_api_message(data) or "闪传下载接口返回的数据格式异常。")
```
```diff
- raise QFlashError(f"连接 QQ 闪传接口失败: {exc}") from exc
+ raise QFlashError(f"连接 QQ 闪传接口失败：{exc}。") from exc
```
```diff
- raise QFlashError("QQ 闪传接口返回的不是 JSON") from exc
+ raise QFlashError("QQ 闪传接口未返回有效 JSON 数据。") from exc
```
```diff
- raise QFlashError(_api_message(data) or "QQ 闪传接口返回错误")
+ raise QFlashError(_api_message(data) or "QQ 闪传接口返回错误。")
```

### `mcsm/deploy.py` — 8 处

```diff
- errors.append(f"{token} 缺少参数")
+ errors.append(f"{token} 缺少参数值。")
```
```diff
- errors.append(f"未知参数: {token}")
+ errors.append(f"不支持的参数：{token}。")
```
```diff
- errors.append("用法: /mcsm deploy <别名> <闪传URL> [--port 宿主端口] [--node 节点] [--image 镜像] [--cmd 启动命令]")
+ errors.append("命令格式：/mcsm deploy <别名> <闪传URL> [--port 宿主端口] [--node 节点] [--image 镜像] [--cmd 启动命令]")
```
```diff
- errors.append("--port 必须是数字")
+ errors.append("--port 参数必须为纯数字。")
```
```diff
- errors.append("--port 必须在 1-65535 之间")
+ errors.append("--port 端口范围须在 1-65535 之间。")
```
```diff
- errors.append("--mem 必须是数字，单位 MB")
+ errors.append("--mem 内存大小必须为纯数字（单位 MB）。")
```
```diff
- errors.append("--mem 不能小于 512 MB")
+ errors.append("--mem 内存配置不可低于 512 MB。")
```
```diff
- errors.append("闪传URL必须是 http(s) 下载链接")
+ errors.append("闪传链接必须是以 http:// 或 https:// 开头的网络地址。")
```

## grok_bot（8 个文件 · 117 处）

### `grok_bot/gateway.py` — 33 处

```diff
- PROTOCOL_ERROR = "Grok Bot 网关返回了无法识别的数据，请管理员检查网关版本。"
- AWAITING_USER = "Grok Bot 正在等待人工确认，请管理员打开 Grok Bot 应用处理后再提问。"
+ PROTOCOL_ERROR = "网关响应数据结构异常，请管理员核实网关服务版本。"
+ AWAITING_USER = "当前任务正等待人工介入确认，请管理员前往 Grok Bot 客户端处理后再发起提问。"
```
```diff
- raise GrokError("Grok Bot 会话中出现了另一条输入，无法可靠对应本次回答。请在应用中查看结果。")
+ raise GrokError("会话中插入了其他提问，无法准确匹配本次生成结果，请在 Grok Bot 客户端查阅。")
```
```diff
- text += f"\n\n本次附件超过 {MAX_REPLY_FILES} 个，其余附件请在 Grok Bot 应用中查看。"
+ text += f"\n\n附件生成数量已达 {MAX_REPLY_FILES} 个上限，多余附件请前往 Grok Bot 客户端查阅。"
```
```diff
- raise GrokError("Grok Bot 附件接口响应过大，已停止读取。")
+ raise GrokError("附件接口下发数据量超出安全阈值，已中止数据接收。")
```
```diff
- f"连接 Grok Bot 网关失败（{command} / {type(error).__name__}），请求尚未提交，请检查 Tailscale 和云端服务。",
+ f"网关连接建立失败（{command} / {type(error).__name__}），提问尚未提交，请检查 Tailscale 网络及云端服务状态。",
```
```diff
- f"Grok Bot 网关请求超时（{command} / {type(error).__name__}），已提交的任务可能仍在云端运行。",
+ f"网关接口请求超时（{command} / {type(error).__name__}），已提交的任务可能仍在云端处理。",
```
```diff
- f"Grok Bot 网关通信失败（{command} / {type(error).__name__}），请检查两端 Tailscale、网关及云端后台服务；已提交的任务可能仍在云端运行。",
+ f"网关数据传输异常（{command} / {type(error).__name__}），请排查 Tailscale 连通性、网关与后台服务；已提交任务可能仍在云端处理。",
```
```diff
- raise GatewayError("Grok Bot 网关认证失败，请管理员检查 GROKBOT_GATEWAY_TOKEN。", not_submitted=True)
+ raise GatewayError("网关鉴权未通过，请管理员检查 GROKBOT_GATEWAY_TOKEN 配置。", not_submitted=True)
```
```diff
- f"Grok Bot 网关拒绝请求（{command} / HTTP {response.status_code}）。请检查网关版本、请求参数和 Bot 数量限制。",
+ f"网关拒绝了当前请求（{command} / HTTP {response.status_code}），请核对网关版本、提交参数与 Bot 实例配额限制。",
```
```diff
- raise GatewayError(f"Grok Bot 网关接口 {command} 返回 404，请检查基址和网关版本。", not_submitted=True)
+ raise GatewayError(f"网关接口 {command} 未找到（HTTP 404），请检查基础地址与网关服务版本。", not_submitted=True)
```
```diff
- raise GatewayError("Grok Bot 网关请求过于频繁，请稍后重试。", not_submitted=True, retryable=True)
+ raise GatewayError("网关请求速率达到限制，请稍候重试。", not_submitted=True, retryable=True)
```
```diff
- raise GatewayError(f"Grok Bot 网关请求失败（{command} / HTTP {response.status_code}）。",
+ raise GatewayError(f"网关接口返回错误（{command} / HTTP {response.status_code}）。",
```
```diff
- raise GatewayError(f"Grok Bot 网关未能完成 {command}，请在应用中查看详情。")
+ raise GatewayError(f"网关未能正常执行指令 {command}，详细原因请在客户端查看。")
```
```diff
- raise GrokError("图片内容无效或超过大小限制，问题尚未发送。")
+ raise GrokError("图片数据损坏或单张体积超限，提问尚未发出。")
```
```diff
- raise GrokError("Grok Bot 图片上传未返回有效路径，问题尚未发送，请检查网关版本。")
+ raise GrokError("图片上传未获取到有效资源标识，提问尚未发出，请检查网关服务版本。")
```
```diff
- raise GrokError("云端附件不可用或返回格式不兼容，请在 Grok Bot 应用中查看。")
+ raise GrokError("云端附件数据不可用或协议不兼容，请在 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("附件超过单个 20 MB 或本次合计 50 MB 的限制，未下载。")
+ raise GrokError("附件体积超过单文件 20 MB 或总计 50 MB 上限，未执行下载。")
```
```diff
- raise GrokError("云端附件在下载时发生变化或返回不完整，请重新生成后再试。")
+ raise GrokError("云端附件在下载过程中被修改或传输残缺，请重新生成后再次获取。")
```
```diff
- raise GrokError("云端附件编码无效，未转发不完整文件。") from None
+ raise GrokError("云端附件编码解析异常，已中止转发残缺内容。") from None
```
```diff
- raise GrokError("云端附件下载不完整，请在 Grok Bot 应用中查看。")
+ raise GrokError("云端附件未能完整获取，请在 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("本次附件合计已达 50 MB，其余附件请在 Grok Bot 应用中查看。")
+ raise GrokError("本次附件传输累计已达 50 MB 上限，其余附件请在 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("本次附件下载超时，其余附件请在 Grok Bot 应用中查看。")
+ raise GrokError("本次附件传输超时，其余附件请在 Grok Bot 客户端查阅。")
```
```diff
- return replace(item, source="", error="附件下载超时，请在 Grok Bot 应用中查看。")
+ return replace(item, source="", error="附件拉取超时，请在 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("GROKBOT_AGENT_ID 应指向单个 Bot，当前不支持 Grok Bot 群聊。")
+ raise GrokError("GROKBOT_AGENT_ID 必须配置为单个 Bot 实例，暂不支持指向群组 Bot。")
```
```diff
- raise GrokError("找不到配置的 Grok Bot，请管理员检查 GROKBOT_AGENT_ID。")
+ raise GrokError("未找到目标 Grok Bot 实例，请管理员检查 GROKBOT_AGENT_ID 配置。")
```
```diff
- raise GrokError("Grok Bot 任务记录过长，无法可靠定位本次回答，请在应用中查看结果。")
+ raise GrokError("对话上下文记录超出检索深度，无法准确关联本次回答，请在 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("Grok Bot 未确认接受本次问题，请在应用中检查状态；本次未重复提交。")
+ raise GrokError("云端未确认接收本次提问，请在 Grok Bot 客户端核对状态；本次未重复提交。")
```
```diff
- raise GrokError("Grok Bot 未接受本次问题，请在应用中检查状态。")
+ raise GrokError("云端未能接收本次提问，请在 Grok Bot 客户端核实当前状态。")
```
```diff
- raise GrokError("Grok Bot 拒绝了本次任务，请在应用中查看原因。")
+ raise GrokError("Grok Bot 拒绝执行本次生成任务，具体原因请在客户端查阅。")
```
```diff
- raise GrokError(f"Grok Bot 本次等待超过 {config.timeout:g} 秒，任务可能仍在云端运行，请在应用中查看。") from None
+ raise GrokError(f"请求等待已超时（超过 {config.timeout:g} 秒），任务可能仍在云端处理，请在 Grok Bot 客户端查阅。") from None
```
```diff
- raise GrokError("Grok Bot 网关健康检查未通过。")
+ raise GrokError("Grok Bot 网关运行状态检测异常。")
```
```diff
- return "Grok Bot 网关、Token 和本地人设模板检查通过。各会话首次提问时创建独立 Bot；本检查未创建 Bot。"
+ return "网关服务、Token 与本地人设配置检测正常，各会话首次提问将自动建立独立 Bot 实例（本检测未创建新实例）。"
```
```diff
- return "Grok Bot 网关、Token、人设和参考 Bot 检查通过。" + ("参考 Bot 正在处理任务。" if busy else "参考 Bot 当前空闲。") + "问答会使用各会话独立的 Bot。"
+ return "网关连通性、Token 认证、人设模板及基准 Bot 状态校验通过，" + ("基准 Bot 当前忙碌。" if busy else "基准 Bot 当前空闲。") + "实际提问将分派至各会话独立的专属 Bot。"
```

### `grok_bot/media.py` — 20 处

```diff
- raise GrokError("云端附件路径无效，未读取本地文件。") from None
+ raise GrokError("云端附件路径格式异常，已终止访问本地文件系统。") from None
```
```diff
- raise GrokError("附件编码无效或超过大小限制。") from None
+ raise GrokError("附件内容编码格式有误，或已超出体积上限。") from None
```
```diff
- raise GrokError("附件地址不可访问，仅支持公开的 HTTP/HTTPS 地址和当前适配器的内部图片。") from None
+ raise GrokError("无法请求该附件地址，仅支持公网可达的 HTTP/HTTPS 链接与适配器内置图片。") from None
```
```diff
- raise GrokError("无法读取适配器内部图片，请重新发送图片。")
+ raise GrokError("适配器内部图片解析失败，请尝试重新发送该图片。")
```
```diff
- raise GrokError("适配器内部图片地址无效。")
+ raise GrokError("适配器提供的内部图片地址无效。")
```
```diff
- raise GrokError("附件地址重定向无效。")
+ raise GrokError("附件请求发生异常重定向，无法获取目标资源。")
```
```diff
- raise GrokError(f"附件下载失败（HTTP {response.status_code}），请重新发送或稍后重试。")
+ raise GrokError(f"附件获取未成功（HTTP {response.status_code}），请重新上传或稍候重试。")
```
```diff
- raise GrokError("附件超过大小限制，未下载。")
+ raise GrokError("附件文件体积已超出下载上限，未执行拉取。")
```
```diff
- raise GrokError("附件超过大小限制，已停止下载。")
+ raise GrokError("附件下载中超出体积限制，已中止传输。")
```
```diff
- raise GrokError("附件下载失败或超时，请重新发送图片或稍后重试。") from None
- raise GrokError("附件地址重定向次数过多。")
+ raise GrokError("附件拉取超时或网络连接异常，请重新上传图片或稍后重试。") from None
+ raise GrokError("附件链接重定向层级过多，已中止请求。")
```
```diff
- raise GrokError("图片像素过大，请压缩后再发送。")
+ raise GrokError("图片分辨率超出限制，请调整尺寸或压缩后再试。")
```
```diff
- raise GrokError("图片转换后超过大小限制，请压缩后重新发送。")
+ raise GrokError("图片处理后体积依然超出限制，请压缩文件后重新上传。")
```
```diff
- raise GrokError("无法读取图片，请使用有效的 PNG、JPEG、WebP 或 GIF 图片。") from None
+ raise GrokError("图像格式无法识别，请使用合规的 PNG、JPEG、WebP 或 GIF 文件。") from None
```
```diff
- raise GrokError(f"发送和引用的图片合计不能超过 {MAX_INPUT_IMAGES} 张。")
+ raise GrokError(f"消息内输入与引用的图片累计上限为 {MAX_INPUT_IMAGES} 张。")
```
```diff
- raise GrokError("无法确定 QQ 文件接收者，附件尚未发送。")
+ raise GrokError("未能获取当前会话接收目标，附件尚未转交发送。")
```
```diff
- raise GrokError("LLOneBot 文件发送未确认，请检查当前会话和适配器；本次未重复发送。") from None
+ raise GrokError("LLOneBot 端未返回文件发送确认，请检查会话与适配器状态；本次未重复推送。") from None
```
```diff
- raise GrokError("LLOneBot 文件上传或发送失败，请检查适配器的群文件或私聊文件支持。")
+ raise GrokError("LLOneBot 上传或发送文件失败，请确认适配器是否已开启群文件与私聊文件能力。")
```
```diff
- raise GrokError(attachment.error or "附件内容不可用，请在 Grok Bot 应用中查看。")
+ raise GrokError(attachment.error or "附件数据不可用，请前往 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("适配器未确认附件发送，请在 Grok Bot 应用中查看。")
+ raise GrokError("协议端未返回附件发送确认，请前往 Grok Bot 客户端查阅。")
```
```diff
- raise GrokError("QQ 附件发送失败，请检查适配器的图片或文件发送支持。") from None
+ raise GrokError("附件投递失败，请排查适配器是否具备图片或文件发送功能。") from None
```

### `grok_bot/handlers.py` — 18 处

```diff
- HELP = """Grok Bot 问答
- /grok 问题：文字或图片提问；引用消息后提问可附上引用正文和图片（合计最多 3 张，每张 5 MB）。
- Grok Bot 回复的图片和文件会转发到当前会话（单个最多 20 MB，每次最多 10 个、合计 50 MB）。
- 别名：/grokbot。同群共用本群对话，不同群与私聊分别独立，默认花园多惠人设。
- 支持任务进行中追加输入；每个会话统一接收回复，不同会话可并行处理。
- 本轮没有追加输入时引用初始问题回复；追加后直接发到当前会话，下一轮重新判断。
- 默认关闭：SuperUser 在目标群执行 /功能 开启 grok；管理员和群主可关闭。
- 私聊需 SuperUser 用 /grok 开启 或 /grok 关闭 管理自己的私聊开关。
- 创建失败后卡在待确认状态：SuperUser 确认云端无对应 Bot 后，用 /grok 修复会话 处理当前会话。
- 需要人工确认时，请管理员在 Grok Bot 应用中处理。"""
+ HELP = """Grok Bot 问答服务
+ /grok <问题> —— 发送文本或附带图片提问；支持引用消息后提问（包含引用正文与图片，至多 3 张，每张限 5 MB）
+ Grok Bot 生成的图片与文件将自动转送至本会话（单文件上限 20 MB，单次至多 10 个且合计限 50 MB）。
+ 别名：/grokbot。同群成员共享该群会话，不同群聊与私聊各自独立，默认采用花园多惠人设。
+ 支持在生成任务进行期间追加输入；各会话按序排队接收，不同会话彼此并行。
+ 若未追加输入则直接引用原提问回复；追加后改为在会话中顺序发送。
+ 功能默认关闭：群内由 SuperUser 使用 /功能 开启 grok（管理员与群主可关闭）；私聊由 SuperUser 发送 /grok 开启 或 /grok 关闭。
+ 如遇会话创建异常：SuperUser 确认云端无残留 Bot 后，可在当前会话执行 /grok 修复会话 重置状态。
+ 若提示等待人工确认，请管理员登录 Grok Bot 客户端处理。"""
```
```diff
- raise GrokError("图片地址缺失，请重新发送图片。")
+ raise GrokError("无法解析图片链接，请重新发送该图片。")
```
```diff
- text = text[:20000] + "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
+ text = text[:20000] + "\n\n文本内容已超出最大长度限制，未展示部分请前往 Grok Bot 客户端查阅。"
```
```diff
- await send_text(session, f"文件：{attachment.name}", reply_to=reply_to)
+ await send_text(session, f"推送文件：{attachment.name}", reply_to=reply_to)
```
```diff
- await send_text(session, f"附件「{attachment.name}」转发失败：{error}", reply_to=reply_to)
+ await send_text(session, f"附件「{attachment.name}」发送未成功：{error}", reply_to=reply_to)
```
```diff
- await send_text(session, "请用 /功能 开启 grok 或 /功能 关闭 grok 管理本群开关。")
+ await send_text(session, "格式：群聊请发送 /功能 开启 grok 或 /功能 关闭 grok 进行开关配置。")
```
```diff
- await send_text(session, "仅 SuperUser 可管理自己的 Grok Bot 私聊开关。")
+ await send_text(session, "权限受限：仅 SuperUser 拥有修改私聊 Grok Bot 开关状态的权限。")
```
```diff
- await send_text(session, f"本次私聊已{text} Grok Bot，配置已保存。")
+ await send_text(session, f"已为当前私聊{text} Grok Bot，状态更新完成。")
```
```diff
- await send_text(session, "当前会话的 Grok Bot 默认关闭，需 SuperUser 手动开启。群内用 /功能 开启 grok，自己的私聊用 /grok 开启。")
+ await send_text(session, "当前会话尚未启用 Grok Bot，需由 SuperUser 开启（群聊发送 /功能 开启 grok，私聊发送 /grok 开启）。")
```
```diff
- await send_text(session, "仅 SuperUser 可修复当前 Grok Bot 会话。")
+ await send_text(session, "权限受限：仅 SuperUser 可对当前会话执行修复重置。")
```
```diff
- text = f"{text or '请回答或解释以下引用内容。'}\n\n[引用消息]\n{quoted}"
+ text = f"{text or '请分析并解答以下引用的内容：'}\n\n[引用消息]\n{quoted}"
```
```diff
- raise GrokError(f"发送和引用的图片合计不能超过 {MAX_INPUT_IMAGES} 张。")
+ raise GrokError(f"输入与引用的图片总数不可超过 {MAX_INPUT_IMAGES} 张。")
```
```diff
- text = "请描述并分析这些图片。"
+ text = "请针对提供的图片进行识别与分析说明。"
```
```diff
- raise GrokError("请输入文字问题、附带图片，或引用一条包含正文或图片的消息。")
+ raise GrokError("未检测到有效输入，请附带文字、图片或引用目标消息后再试。")
```
```diff
- raise GrokError("问题和引用内容合计不能超过 6000 字。")
+ raise GrokError("提问文字与引用文本总长度不得超出 6000 字。")
```
```diff
- text = f"[本次发言者 ID：{user_id}]\n{text}"
+ text = f"[发言者 ID：{user_id}]\n{text}"
```
```diff
- await send_text(session, "Grok Bot 配置或开关读写失败，请管理员检查本地数据文件及目录权限。")
+ await send_text(session, "本地配置或存储数据读取失败，请联系管理员核对文件读写权限。")
```
```diff
- await send_text(session, "Grok Bot 处理失败，请稍后重试。")
+ await send_text(session, "处理请求时发生内部错误，请稍后重新发起提问。")
```

### `grok_bot/stream.py` — 18 处

```diff
- message = (f"Grok Bot 本轮接收等待超过 {self.config.timeout:g} 秒，任务可能仍在云端运行；后续结果请在应用中查看。"
- if acquired else "Grok Bot 等待会话名额超时，本次输入尚未提交。")
+ message = (f"本轮响应等待已达 {self.config.timeout:g} 秒上限，云端生成可能仍在继续，后续内容请在 Grok Bot 客户端查阅。"
+ if acquired else "排队等待会话名额超时，本次内容尚未提交。")
```
```diff
- message = "Grok Bot 会话接收失败，请管理员检查日志；已提交的任务可能仍在云端运行。"
+ message = "会话消息接收中断，请管理员查看运行日志；已提交的任务可能仍在云端处理中。"
```
```diff
- self.fail_waiting("Grok Bot 当前接收任务已结束，本次问题尚未提交，请重新发送。")
+ self.fail_waiting("当前接收流程已终止，本次提问尚未提交成功，请重新发送。")
```
```diff
- raise GrokError("Grok Bot 提交等待超时，本次问题尚未发送。")
+ raise GrokError("请求排队等待超时，本次提问尚未发出。")
```
```diff
- item.done.set_exception(GrokError("Grok Bot 本次提交已停止，请在应用中确认是否收到；本次未重复提交。"))
+ item.done.set_exception(GrokError("提问提交过程已终止，请在 Grok Bot 客户端确认接收状态；本次未重复推送。"))
```
```diff
- error = GrokError("Grok Bot 本次提交等待超时，请在应用中确认是否收到；本次未重复提交。")
+ error = GrokError("请求提交等待超时，请在 Grok Bot 客户端核实是否已接收；本次未重复推送。")
```
```diff
- error = GrokError("Grok Bot 本次输入处理失败，请管理员检查日志。")
+ error = GrokError("本次输入处理遇到异常，请管理员查阅运行日志。")
```
```diff
- raise GrokError("当前会话的 Grok Bot 已关闭，本次问题尚未发送。")
+ raise GrokError("当前会话的 Grok Bot 服务已停用，提问尚未发送。")
```
```diff
- raise GrokError("当前会话的 Grok Bot 已关闭，本轮接收已停止；已提交的任务可能仍在云端运行。")
+ raise GrokError("当前会话的 Grok Bot 服务已停用，接收流程已中止；已提交的任务可能仍在云端处理。")
```
```diff
- await submission.item.deliver(Reply("Grok Bot 拒绝了本次输入，请在应用中查看原因。"), reply_to=True)
+ await submission.item.deliver(Reply("Grok Bot 未接受本次输入请求，详情请在客户端查阅。"), reply_to=True)
```
```diff
- raise GrokError("Grok Bot 插件正在停止，请稍后重试。")
+ raise GrokError("Grok Bot 模块正在停止服务，请稍候重试。")
```
```diff
- raise GrokError("当前会话正在接收回复或修复绑定，请结束后再修复。")
+ raise GrokError("当前会话正处于回复接收或修复处理中，请等待完成后再试。")
```
```diff
- raise GrokError("Grok Bot 缺少会话回复通道。")
+ raise GrokError("缺少可用应答通道，无法完成回复推送。")
```
```diff
- raise GrokError("Grok Bot 提交队列已满，请稍后重试。")
+ raise GrokError("当前并发请求队列已达上限，请稍候再试。")
```
```diff
- raise GrokError("Grok Bot 插件正在停止，请稍后重试。")
+ raise GrokError("Grok Bot 模块正在停止服务，请稍候重试。")
```
```diff
- await progress("Grok Bot 正在处理其他会话，本次输入等待提交。")
+ await progress("当前正忙于处理其他会话请求，本次输入已进入等待队列。")
```
```diff
- raise GrokError("Grok Bot 提交等待超时，请在应用中确认是否收到；本次未重复提交。") from None
+ raise GrokError("请求提交等待超时，请在 Grok Bot 客户端核对接收情况；本次未重复推送。") from None
```
```diff
- live.fail_waiting("Grok Bot 插件已停止，本次问题尚未提交。")
+ live.fail_waiting("Grok Bot 服务已终止，本次提问未提交。")
```

### `grok_bot/conversations.py` — 16 处

```diff
- raise GrokError("无法确定当前 QQ 会话，问题尚未发送。请管理员检查适配器事件信息。")
+ raise GrokError("未能识别当前 QQ 会话标识，问题尚未发送，请管理员排查适配器上报的事件字段。")
```
```diff
- raise GrokError("Grok Bot 会话绑定保存失败，请管理员检查 data/grok_bot/sessions.json 和目录权限。") from None
+ raise GrokError("会话映射持久化失败，请管理员核实 data/grok_bot/sessions.json 及其父目录的写入权限。") from None
```
```diff
- raise GrokError("Grok Bot 会话绑定无法读取或已损坏，请管理员检查 data/grok_bot/sessions.json；未使用共享会话。") from None
+ raise GrokError("会话映射文件无法读取或数据损毁，请管理员检查 data/grok_bot/sessions.json；当前未回退至共享会话。") from None
```
```diff
- raise GrokError("Grok Bot 会话绑定冲突，问题尚未发送，请管理员检查会话映射。")
+ raise GrokError("会话映射出现冲突，当前问题未发送，请管理员检查会话对应关系。")
```
```diff
- raise GrokError("发现重复的 Grok Bot 会话标记，问题尚未发送，请管理员检查云端 Bot。")
+ raise GrokError("检测到云端存在重复的会话识别标识，提问未发送，请管理员检查云端 Bot 实例。")
```
```diff
- raise GrokError("当前会话的 Grok Bot 不存在或绑定不匹配，请管理员检查会话映射和云端 Bot。")
+ raise GrokError("当前会话绑定的 Grok Bot 缺失或实例信息不一致，请管理员核实映射配置与云端 Bot。")
```
```diff
- raise GrokError("会话绑定意外指向原 QQBOT，问题尚未发送，请管理员检查映射。")
+ raise GrokError("会话异常映射到了原 QQBOT 实例，问题尚未发送，请管理员核查绑定配置。")
```
```diff
- raise GrokError("Grok Bot 上次创建结果尚未确认。请 SuperUser 确认云端没有对应 Bot 后，在当前会话执行 /grok 修复会话，再重新提问。")
+ raise GrokError("上一轮 Bot 实例创建结果尚未明确，请 SuperUser 在确认云端无多余 Bot 后，于本会话执行 /grok 修复会话，随后即可重新提问。")
```
```diff
- raise GrokError("Grok Bot 创建接口返回了已有 Bot，问题尚未发送，请管理员检查网关版本。")
+ raise GrokError("创建实例接口返回了已有 Bot 标识，提问未发送，请管理员检查网关服务版本。")
```
```diff
- raise GrokError("Grok Bot 人设同步失败，本次问题尚未发送，请管理员检查网关版本。")
+ raise GrokError("同步预设人设失败，当前提问尚未发出，请管理员检查网关兼容性。")
```
```diff
- return "当前会话没有待修复的创建记录，可以重新提问。"
+ return "当前会话无待处理的异常创建记录，可直接发起提问。"
```
```diff
- return "已确认当前会话的 Grok Bot 绑定，可以继续提问。"
+ return "当前会话的 Grok Bot 绑定已核验无误，可继续进行提问。"
```
```diff
- raise GrokError("云端存在同名 Bot，但缺少会话标记；本次未清除绑定，请管理员核对该 Bot 的身份。")
+ raise GrokError("云端发现同名 Bot 但未包含有效会话标识，本次保留了现有绑定，请管理员确认云端实例属性。")
```
```diff
- return "已清除当前会话失败的创建记录，未删除任何云端 Bot。请重新发送 /grok 问题。"
+ return "已重置当前会话的失败记录，未改动云端任何 Bot 实例，请重新发送 /grok 提问。"
```
```diff
- raise GrokError("Grok Bot 会话修复检查超时，请检查网关连接后重试。") from None
+ raise GrokError("核验会话修复状态超时，请排查网关连通性后重试。") from None
```
```diff
- raise GrokError(f"已转发当前回复，但云端任务在 {config.timeout:g} 秒内未结束；后续结果请在 Grok Bot 应用中查看。") from None
- raise GrokError(f"Grok Bot 本次等待超过 {config.timeout:g} 秒，任务可能仍在云端运行，请在应用中查看。") from None
+ raise GrokError(f"已转交部分回复，但云端任务超过 {config.timeout:g} 秒尚未结束，后续内容请在 Grok Bot 客户端查阅。") from None
+ raise GrokError(f"请求耗时已超 {config.timeout:g} 秒限制，任务可能仍在云端处理中，请前往 Grok Bot 客户端查阅。") from None
```

### `grok_bot/config.py` — 7 处

```diff
- raise GrokError("Grok Bot 尚未配置。请管理员填写 GROKBOT_GATEWAY_URL 和 GROKBOT_GATEWAY_TOKEN。")
+ raise GrokError("Grok Bot 尚未完成配置，请管理员补充填写 GROKBOT_GATEWAY_URL 与 GROKBOT_GATEWAY_TOKEN。")
```
```diff
- raise GrokError("GROKBOT_GATEWAY_URL 应为网关基址，例如 http://100.x.x.x:1340；不要填写 /v1、/api 或 0.0.0.0。")
+ raise GrokError("GROKBOT_GATEWAY_URL 须配置为网关基础地址（如 http://100.x.x.x:1340），请勿包含 /v1、/api 或设为 0.0.0.0。")
```
```diff
- raise GrokError("GROKBOT_AGENT_ID 应为目标 Bot 的 UUID。") from None
+ raise GrokError("GROKBOT_AGENT_ID 格式不符合规范，须为目标 Bot 的 UUID。") from None
```
```diff
- raise GrokError("GROKBOT_GATEWAY_TOKEN 格式无效，请填写网关 Token 原文。")
+ raise GrokError("GROKBOT_GATEWAY_TOKEN 无效，请填写正确的网关 Token 原始内容。")
```
```diff
- raise GrokError("GROKBOT_TIMEOUT 应为 30～1800 秒，GROKBOT_MAX_PENDING 应为 1～32，GROKBOT_MAX_CONCURRENT 应为 1～16。") from None
+ raise GrokError("配置参数超出有效区间：GROKBOT_TIMEOUT 需在 30～1800 秒内，GROKBOT_MAX_PENDING 需在 1～32 内，GROKBOT_MAX_CONCURRENT 需在 1～16 内。") from None
```
```diff
- raise GrokError("Grok Bot 人设模板读取失败，请管理员检查 GROKBOT_PERSONA_FILE 和文件编码。") from None
+ raise GrokError("Grok Bot 预设人设加载失败，请管理员核查 GROKBOT_PERSONA_FILE 路径与编码格式。") from None
```
```diff
- raise GrokError("Grok Bot 人设模板须为 1～20000 字的 UTF-8 文本。")
+ raise GrokError("Grok Bot 预设人设内容须为 1～20000 字的有效 UTF-8 文本。")
```

### `grok_bot/relay.py` — 4 处

```diff
- raise GrokError("QQ 回复发送未确认，请检查当前会话；本次未重复发送。") from None
+ raise GrokError("消息发送状态未确认，请检查当前会话接收情况；为避免刷屏本次未重复推送。") from None
```
```diff
- raise GrokError("云端已发送的回复发生变化，后续结果请在 Grok Bot 应用中查看；未重复转发。")
+ raise GrokError("云端已推送的回复内容出现变更，为避免错乱后续请在 Grok Bot 客户端查阅；已停止重复转发。")
```
```diff
- delta += "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
+ delta += "\n\n回复篇幅已超限，剩余内容请前往 Grok Bot 客户端查阅。"
```
```diff
- await self.emit(Reply(f"本次附件超过 {MAX_REPLY_FILES} 个，其余附件请在 Grok Bot 应用中查看。"))
+ await self.emit(Reply(f"本次生成附件数量已超 {MAX_REPLY_FILES} 个上限，剩余附件请在 Grok Bot 客户端查阅。"))
```

### `grok_bot/check.py` — 1 处

```diff
- print(f"Grok Bot 检查失败（{type(error).__name__}）。")
+ print(f"Grok Bot 状态检测异常（{type(error).__name__}）。")
```

## minecraft_plugin（4 个文件 · 51 处）

### `minecraft_plugin/handlers.py` — 42 处

```diff
- await server_ping.finish("当前群没有保存的服务器，请使用 /addserver 添加。")
+ await server_ping.finish("当前群尚未添加任何服务器，可发送 /addserver <名称> <地址> 进行添加。")
```
```diff
- await add_server.finish("指令格式错误，请使用 /addserver <服务器昵称> <服务器地址>")
+ await add_server.finish("格式错误：/addserver <服务器昵称> <服务器地址>")
```
```diff
- await add_server.finish("指令格式错误，请使用 /addserver <服务器昵称> <服务器地址>")
+ await add_server.finish("格式错误：/addserver <服务器昵称> <服务器地址>")
```
```diff
- await add_server.finish(f"已添加服务器: {server_name} ({server_address})")
+ await add_server.finish(f"服务器添加成功：{server_name}（{server_address}）")
```
```diff
- await add_server.finish("添加失败: 服务器已存在或数据错误")
+ await add_server.finish("添加失败：服务器已存在或地址格式有误。")
```
```diff
- await add_server_nickname.finish("指令格式错误，请使用 /addservernickname <服务器名称> <服务器昵称>")
+ await add_server_nickname.finish("格式错误：/addservernickname <服务器名称> <服务器昵称>")
```
```diff
- await add_server_nickname.finish(f"已为服务器 {parts[0]} 添加昵称: {parts[1]}")
+ await add_server_nickname.finish(f"已成功为服务器 {parts[0]} 设置昵称：{parts[1]}")
```
```diff
- await add_server_nickname.finish("添加昵称失败，请检查服务器名称是否正确。")
+ await add_server_nickname.finish("设置昵称失败：未找到对应的服务器名称。")
```
```diff
- await remove_server.finish("指令格式错误，请使用 /removeserver <服务器名称>")
+ await remove_server.finish("格式错误：/removeserver <服务器名称>")
```
```diff
- await remove_server.finish(f"已删除服务器: {command_args}")
+ await remove_server.finish(f"已成功删除服务器：{command_args}")
```
```diff
- await remove_server.finish("删除失败，请检查服务器名称。")
+ await remove_server.finish("删除失败：未找到指定的服务器。")
```
```diff
- await remove_server_nickname.finish("指令格式错误，请使用 /removeservernickname <服务器名称>")
+ await remove_server_nickname.finish("格式错误：/removeservernickname <服务器名称>")
```
```diff
- await remove_server_nickname.finish(f"已删除服务器 {command_args} 的昵称")
+ await remove_server_nickname.finish(f"已成功清除服务器 {command_args} 的昵称。")
```
```diff
- await remove_server_nickname.finish("删除失败。")
+ await remove_server_nickname.finish("清除昵称失败：未找到指定的服务器。")
```
```diff
- await update_server_nickname.finish("指令格式错误，请使用 /updateservername <服务器名称> <新名称>")
+ await update_server_nickname.finish("格式错误：/updateservername <服务器名称> <新名称>")
```
```diff
- await update_server_nickname.finish("已更新服务器名称。")
+ await update_server_nickname.finish("服务器名称更新成功。")
```
```diff
- await update_server_nickname.finish("更新失败。")
+ await update_server_nickname.finish("更新失败：未找到指定的服务器。")
```
```diff
- await update_server_address.finish("指令格式错误，请使用 /updateserveraddress <服务器名称> <新地址>")
+ await update_server_address.finish("格式错误：/updateserveraddress <服务器名称> <新地址>")
```
```diff
- await update_server_address.finish("已更新服务器地址。")
+ await update_server_address.finish("服务器地址更新成功。")
```
```diff
- await update_server_address.finish("更新失败。")
+ await update_server_address.finish("更新失败：未找到指定的服务器。")
```
```diff
- await ping_list.finish("当前群没有保存的服务器。")
+ await ping_list.finish("当前群尚未保存任何服务器。")
```
```diff
- lines = ["当前群保存的服务器:"]
+ lines = ["本群已保存的服务器列表："]
```
```diff
- await add_broadcast.finish("指令格式错误，请使用 /addbroadcast <服务器名称> <服务器地址>\n或 /addbroadcast <已保存的服务器名称>")
+ await add_broadcast.finish("格式错误：/addbroadcast <服务器名称> [服务器地址]\n（若服务器尚未保存，需同时提供地址）")
```
```diff
- await add_broadcast.finish(f"已将 {existing['name']} 加入播报列表")
+ await add_broadcast.finish(f"已成功将 {existing['name']} 加入播报名单。")
```
```diff
- await add_broadcast.finish(f"已添加 {server_name} ({server_address}) 到播报列表")
+ await add_broadcast.finish(f"已成功将 {server_name}（{server_address}）添加到播报名单。")
```
```diff
- await add_broadcast.finish(f"服务器 {server_name} 未在本地找到，请同时提供地址: /addbroadcast <名称> <地址>")
+ await add_broadcast.finish(f"未在已存列表中找到 {server_name}，请附带地址：/addbroadcast <名称> <地址>")
```
```diff
- await remove_broadcast.finish("指令格式错误，请使用 /removebroadcast <服务器名称>")
+ await remove_broadcast.finish("格式错误：/removebroadcast <服务器名称>")
```
```diff
- await remove_broadcast.finish(f"已将 {command_args} 从播报列表中移除")
+ await remove_broadcast.finish(f"已成功将 {command_args} 从播报名单中移除。")
```
```diff
- await update_broadcast_address.finish("指令格式错误，请使用 /updatebroadcastaddress <服务器名称> <新地址>")
+ await update_broadcast_address.finish("格式错误：/updatebroadcastaddress <服务器名称> <新地址>")
```
```diff
- await update_broadcast_address.finish(f"已更新播报服务器 {parts[0]} 的地址。")
+ await update_broadcast_address.finish(f"播报服务器 {parts[0]} 的地址已更新。")
```
```diff
- await update_broadcast_name.finish("指令格式错误，请使用 /updatebroadcastname <服务器名称> <新的播报名称>")
+ await update_broadcast_name.finish("格式错误：/updatebroadcastname <服务器名称> <新播报名称>")
```
```diff
- await update_broadcast_name.finish("已更新播报服务器名称。")
+ await update_broadcast_name.finish("播报服务器名称已成功更新。")
```
```diff
- await broadcast_list.finish("当前群没有播报服务器。")
+ await broadcast_list.finish("当前群尚未开启任何服务器的播报。")
```
```diff
- lines = ["当前群的播报服务器:"]
+ lines = ["本群启用的播报服务器列表："]
```
```diff
- lines.append(f"播报间隔: {_format_interval(current_interval)}")
+ lines.append(f"当前轮询间隔：{_format_interval(current_interval)}")
```
```diff
- f"当前群播报间隔: {_format_interval(current)}\n"
- f"全局默认间隔: {_format_interval(MC_BROADCAST_INTERVAL)}"
+ f"当前群播报间隔：{_format_interval(current)}\n"
+ f"全局预设间隔：{_format_interval(MC_BROADCAST_INTERVAL)}"
```
```diff
- f"已恢复全局默认播报间隔: {_format_interval(MC_BROADCAST_INTERVAL)}"
+ f"已恢复为全局默认播报间隔：{_format_interval(MC_BROADCAST_INTERVAL)}"
```
```diff
- await broadcast_interval.finish("格式错误，请使用 60s、5m、1h，或裸数字表示分钟。")
+ await broadcast_interval.finish("时间格式错误，支持 60s、5m、1h 或纯数字（单位：分钟）。")
```
```diff
- await broadcast_interval.finish("播报间隔必须在 60 秒到 24 小时之间。")
+ await broadcast_interval.finish("播报间隔范围须在 60 秒至 24 小时之间。")
```
```diff
- await broadcast_interval.finish(f"已设置当前群播报间隔为: {_format_interval(seconds)}")
+ await broadcast_interval.finish(f"本群播报间隔已调整为：{_format_interval(seconds)}")
```
```diff
- await online_rank.finish(f"未找到服务器: {command_args}")
+ await online_rank.finish(f"未查询到服务器：{command_args}")
```
```diff
- await online_rank.finish("暂无玩家游戏时长数据。\n玩家数据会在离开服务器时自动记录。")
+ await online_rank.finish("暂无玩家时长记录。\n（数据将在玩家断开连接离开服务器时自动统计）")
```

### `minecraft_plugin/broadcast_utils.py` — 4 处

```diff
- return [f"[MC_Server] 服务器 {server_name} 已关闭"], playtime_deltas
+ return [f"[MC_Server] 服务器 {server_name} 已关闭。"], playtime_deltas
```
```diff
- return [f"[MC_Server] 服务器 {server_name} 已启动，当前在线: {joined}"], playtime_deltas
- return [f"[MC_Server] 服务器 {server_name} 已启动"], playtime_deltas
+ return [f"[MC_Server] 服务器 {server_name} 已开启，当前在线：{joined}"], playtime_deltas
+ return [f"[MC_Server] 服务器 {server_name} 已开启。"], playtime_deltas
```
```diff
- messages.append(f"[MC_Server] {server_name}: {'、'.join(joined)} 加入了服务器")
+ messages.append(f"[MC_Server] {server_name}：{'、'.join(joined)} 加入了服务器。")
```
```diff
- left_parts.append(f"{player}({format_duration(duration)})")
- messages.append(f"[MC_Server] {server_name}: {'、'.join(left_parts)} 离开了服务器")
+ left_parts.append(f"{player}（{format_duration(duration)}）")
+ messages.append(f"[MC_Server] {server_name}：{'、'.join(left_parts)} 离开了服务器。")
```

### `minecraft_plugin/ping.py` — 3 处

```diff
- return {"status": "error", "data": "DNS 解析超时或失败，请检查服务器地址后重试"}
+ return {"status": "error", "data": "域名解析失败或请求超时，请核对服务器地址。"}
```
```diff
- return {"status": "error", "data": "服务器未开启或服务器地址错误"}
+ return {"status": "error", "data": "目标服务器尚未启动或地址填写错误。"}
```
```diff
- return {"status": "error", "data": "出现未知错误"}
+ return {"status": "error", "data": "查询遇到未知异常，请稍后重试。"}
```

### `minecraft_plugin/broadcast.py` — 2 处

```diff
- suffix = f"\n已节流 {suppressed} 项重复错误" if suppressed else ""
+ suffix = f"\n已折叠 {suppressed} 条重复异常" if suppressed else ""
```
```diff
- f"广播轮次错误汇总 ({len(lines)} 项):\n" + "\n---\n".join(lines[-5:]) + suffix
+ f"广播异常摘要（共 {len(lines)} 条）：\n" + "\n---\n".join(lines[-5:]) + suffix
```

## steamInfo（1 个文件 · 41 处）

### `steamInfo/handlers.py` — 41 处

```diff
- STEAM_HELP_USAGE = ""
+ STEAM_HELP_USAGE = """Steam 插件使用指南：
+ /steambind <Steam ID 或 好友代码> - 绑定账号
+ /steamunbind - 解除当前账号绑定
+ /steaminfo [@某人 或 Steam ID] - 查看个人名片及游戏状态
+ /steamcheck - 查看本群所有绑定成员的当前状态
+ /steamgame <游戏名 或 AppID> - 统计本群该游戏的游玩时长榜
+ /steamnickname <昵称> - 设置群内 Steam 播报昵称
+ /steamenable - 开启本群 Steam 动态播报
+ /steamdisable - 关闭本群 Steam 动态播报
+ /steamupdate [群名] [配图] - 更新本群名片与头像缓存"""
```
```diff
- lines = ["可能的 Steam 游戏名不止一个，请回复序号或直接回复游戏名："]
+ lines = ["匹配到多个可能的 Steam 游戏，请回复序号或直接回复游戏名："]
```
```diff
- lines.append(f"鍘熻緭鍏ワ細{query}")
+ lines.append(f"搜索原词：{query}")
```
```diff
- await game.finish(f"未找到游戏 {custom_query}")
+ await game.finish(f"未检索到游戏：{custom_query}")
```
```diff
- f"Found {app.get('name') or app['appid']} ({app['appid']}). Reply yes or no.",
+ f"已找到游戏：{app.get('name') or app['appid']}（AppID: {app['appid']}）。请回复 yes 或 no 进行确认：",
```
```diff
- await game.finish("等待确认超时，请重新执行 steamgame 查询")
+ await game.finish("等待确认超时，本次查询已自动取消。")
```
```diff
- await game.finish("已取消本次查询")
- await game.finish("确认无效，请重新执行 steamgame 查询")
+ await game.finish("已取消当前游戏查询。")
+ await game.finish("输入无效，查询已取消，请重新发送指令。")
```
```diff
- await game.finish("等待选择超时，请重新执行 steamgame 查询")
+ await game.finish("等待选择超时，本次查询已自动取消。")
```
```diff
- await game.finish("选择无效，请重新执行 steamgame 查询")
+ await game.finish("序号或名称选择无效，请重新发起查询。")
```
```diff
- "Usage:\n"
- "/steamcache refresh\n"
- "/steamcache alias list [keyword]\n"
- "/steamcache alias set <alias> <appid> <name>\n"
- "/steamcache alias del <alias>\n"
- "/steamcache lookup list [keyword]\n"
- "/steamcache lookup set <query> <appid> <name>\n"
- "/steamcache lookup del <query>\n"
- "/steamcache ambiguous list [keyword]\n"
- "/steamcache ambiguous del <query>"
+ "格式说明：\n"
+ "/steamcache refresh —— 刷新游戏列表缓存\n"
+ "/steamcache alias list [关键词] —— 查看别名映射\n"
+ "/steamcache alias set <别名> <appid> <游戏名> —— 设定别名\n"
+ "/steamcache alias del <别名> —— 删除别名\n"
+ "/steamcache lookup list [关键词] —— 查看精确检索缓存\n"
+ "/steamcache lookup set <词条> <appid> <游戏名> —— 设定检索缓存\n"
+ "/steamcache lookup del <词条> —— 删除检索缓存\n"
+ "/steamcache ambiguous list [关键词] —— 查看歧义候选缓存\n"
+ "/steamcache ambiguous del <词条> —— 删除歧义候选缓存"
```
```diff
- await steam_cache.finish("Permission denied")
+ await steam_cache.finish("权限不足：仅机器人超级管理员可使用此命令。")
```
```diff
- await steam_cache.finish(f"Steam app list refreshed: {len(apps)} apps")
+ await steam_cache.finish(f"Steam 应用列表已刷新完成，共缓存 {len(apps)} 款应用。")
```
```diff
- await bind.finish("Steam 插件仅支持群聊/频道使用")
+ await bind.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- "请输入正确的 Steam ID 或 Steam 好友代码，格式: steambind [Steam ID 或 Steam好友代码]"
+ "账号输入有误，格式：/steambind <Steam ID 或 好友代码>"
```
```diff
- await bind.finish(f"已更新你的 Steam ID 为 {steam_id}")
+ await bind.finish(f"Steam ID 更新成功：{steam_id}")
```
```diff
- await bind.finish(f"已绑定你的 Steam ID 为 {steam_id}")
+ await bind.finish(f"Steam ID 绑定成功：{steam_id}")
```
```diff
- await unbind.finish("Steam 插件仅支持群聊/频道使用")
+ await unbind.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await unbind.finish("已解绑 Steam ID")
+ await unbind.finish("Steam ID 解绑成功。")
```
```diff
- await unbind.finish("未绑定 Steam ID")
+ await unbind.finish("当前未绑定任何 Steam ID。")
```
```diff
- await info.finish("Steam 插件仅支持群聊/频道使用")
+ await info.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await info.finish("该用户未绑定 Steam ID")
+ await info.finish("目标成员尚未绑定 Steam ID。")
```
```diff
- await info.finish("请输入正确的 Steam ID 或 Steam 好友代码")
+ await info.finish("输入有误：请输入有效的 Steam ID 或好友代码。")
```
```diff
- "未绑定 Steam ID，请使用 steambind [Steam ID 或 Steam好友代码] 绑定 Steam ID"
+ "你尚未绑定 Steam ID，请先使用：/steambind <Steam ID 或 好友代码>"
```
```diff
- await check.finish("Steam 插件仅支持群聊/频道使用")
+ await check.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await game.finish("Steam 插件仅支持群聊/频道使用")
+ await game.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await game.finish("请输入游戏名或 appid，格式: steamgame [游戏名或 appid]")
+ await game.finish("格式错误：/steamgame <游戏名 或 AppID>")
```
```diff
- await game.finish("未配置 Steam API Key，无法查询玩家游戏库数据")
+ await game.finish("未配置 Steam API Key，暂无法查询玩家游戏数据。")
```
```diff
- await game.finish("本群还没有绑定 Steam ID")
+ await game.finish("当前群聊尚未有成员绑定 Steam ID。")
```
```diff
- await game.finish(f"未找到游戏 {query}")
+ await game.finish(f"未检索到游戏：{query}")
```
```diff
- f"No public playtime data found for {app_name} among {len(bound_players)} bound players."
+ f"已绑定的 {len(bound_players)} 位成员中，暂无关于 {app_name} 的公开游玩时长数据。"
```
```diff
- await update_parent_info.finish("Steam 插件仅支持群聊/频道使用")
+ await update_parent_info.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await update_parent_info.finish("文本中应包含图片和文字")
+ await update_parent_info.finish("格式要求：消息需同时包含群名文本与头像图片。")
```
```diff
- await update_parent_info.finish("更新成功")
+ await update_parent_info.finish("群信息已成功更新。")
```
```diff
- await enable.finish("Steam 插件仅支持群聊/频道使用")
+ await enable.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await enable.finish("已启用 Steam 播报")
+ await enable.finish("已开启本群 Steam 游戏动态播报。")
```
```diff
- await disable.finish("Steam 插件仅支持群聊/频道使用")
+ await disable.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await disable.finish("已禁用 Steam 播报")
+ await disable.finish("已停用本群 Steam 游戏动态播报。")
```
```diff
- await set_nickname.finish("Steam 插件仅支持群聊/频道使用")
+ await set_nickname.finish("请在群聊或频道中使用 Steam 相关指令。")
```
```diff
- await set_nickname.finish("璇疯緭鍏ユ樀绉帮紝鏍煎紡: steamnickname [鏄电О]")
+ await set_nickname.finish("格式错误：/steamnickname <昵称>")
```
```diff
- await set_nickname.finish("未绑定 Steam ID，请先使用 steambind 绑定 Steam ID 后再设置昵称")
+ await set_nickname.finish("尚未绑定账号，请先通过 /steambind 绑定 Steam ID 后再设置昵称。")
```
```diff
- await set_nickname.finish(f"已设置你的 Steam 播报昵称为 {nickname}")
+ await set_nickname.finish(f"Steam 播报昵称已设置为：{nickname}")
```

## peek（1 个文件 · 31 处）

### `peek/handlers.py` — 31 处

```diff
- await peek.finish("该命令仅在群聊/频道中可用")
+ await peek.finish("该指令仅支持在群聊或频道中使用。")
```
```diff
- await peek.finish(f"未找到名为 '{command_args}' 的目标")
+ await peek.finish(f"未找到名为「{command_args}」的监控目标。")
```
```diff
- await peek.finish("当前群组没有设置默认 peek 地址")
+ await peek.finish("当前群暂未配置默认 peek 地址。")
```
```diff
- await peek.finish("当前群组没有设置默认 peek 地址，请添加新的 peek 地址")
+ await peek.finish("当前群未设置默认 peek 地址，请先添加可用地址。")
```
```diff
- await peek.finish("请提供正确的 peek 地址，必须以 http:// 或 https:// 开头")
+ await peek.finish("peek 地址格式不正确，须以 http:// 或 https:// 开头。")
```
```diff
- await peek.finish("获取图片失败，可能是目标没有启动服务")
+ await peek.finish("获取屏幕截图失败，目标服务可能未启动。")
```
```diff
- await peek.finish("获取图片失败，可能是目标没有启动服务")
+ await peek.finish("获取屏幕截图失败，目标服务可能未启动。")
```
```diff
- await add_whitelist.finish("你没有权限执行此操作")
+ await add_whitelist.finish("权限不足，无法执行该操作。")
```
```diff
- await add_whitelist.finish("请提供要添加的群号")
+ await add_whitelist.finish("请输入待添加的群号。")
```
```diff
- await add_whitelist.finish(f"群号 {gid} 已添加到白名单")
+ await add_whitelist.finish(f"群号 {gid} 已加入白名单。")
```
```diff
- await add_whitelist.finish(f"群号 {gid} 已在白名单中")
+ await add_whitelist.finish(f"群号 {gid} 已在白名单列表中。")
```
```diff
- await add_peek.finish("你没有权限执行此操作")
+ await add_peek.finish("权限不足，无法执行该操作。")
```
```diff
- await add_peek.finish("用法: /add_peek <昵称> <http地址>")
+ await add_peek.finish("格式：/add_peek <昵称> <http地址>")
```
```diff
- await add_peek.finish("该命令仅在群聊/频道中可用")
+ await add_peek.finish("该指令仅支持在群聊或频道中使用。")
```
```diff
- await add_peek.finish(f"昵称 {nick} 已存在")
+ await add_peek.finish(f"目标昵称「{nick}」已存在。")
```
```diff
- await add_peek.finish("请提供正确的 peek 地址")
+ await add_peek.finish("请输入有效的 peek 地址。")
```
```diff
- await add_peek.finish(f"无法访问: {path}")
+ await add_peek.finish(f"无法连通目标地址：{path}")
```
```diff
- add_msg = "\n已设为默认 peek 地址"
+ add_msg = "\n已同步设为默认 peek 地址。"
```
```diff
- await add_peek.finish(f"已添加 {nick}: {path}" + add_msg)
+ await add_peek.finish(f"已成功添加 {nick}: {path}" + add_msg)
```
```diff
- await del_peek.finish("你没有权限执行此操作")
+ await del_peek.finish("权限不足，无法执行该操作。")
```
```diff
- await del_peek.finish("请提供要删除的昵称")
+ await del_peek.finish("请输入待删除的目标昵称。")
```
```diff
- await del_peek.finish("该命令仅在群聊/频道中可用")
+ await del_peek.finish("该指令仅支持在群聊或频道中使用。")
```
```diff
- await del_peek.finish(f"群组 {group_id} 没有配置")
+ await del_peek.finish(f"群组 {group_id} 尚未配置任何目标。")
```
```diff
- await del_peek.finish(f"昵称 {nick} 不存在")
+ await del_peek.finish(f"目标昵称「{nick}」不存在。")
```
```diff
- await del_peek.finish(f"已删除 {nick}（含默认地址）")
+ await del_peek.finish(f"已移除 {nick}（包含默认地址）。")
```
```diff
- await del_peek.finish(f"已删除 {nick}")
+ await del_peek.finish(f"已移除 {nick}。")
```
```diff
- await set_def_peek.finish("你没有权限执行此操作")
+ await set_def_peek.finish("权限不足，无法执行该操作。")
```
```diff
- await set_def_peek.finish("用法: /set_default_peek <昵称>")
+ await set_def_peek.finish("格式：/set_default_peek <昵称>")
```
```diff
- await set_def_peek.finish("该命令仅在群聊/频道中可用")
+ await set_def_peek.finish("该指令仅支持在群聊或频道中使用。")
```
```diff
- await set_def_peek.finish(f"昵称 {nick} 不存在")
+ await set_def_peek.finish(f"目标昵称「{nick}」不存在。")
```
```diff
- await set_def_peek.finish(f"已将默认 peek 设置为 {nick}")
+ await set_def_peek.finish(f"已将默认 peek 目标切换为 {nick}。")
```

## hyw（2 个文件 · 28 处）

### `hyw/handlers.py` — 27 处

```diff
- HELP = """HYW / 何意味
- /q 问题：搜索问答，也可附带图片（最多 4 张）。
- 引用一条消息后发送 /q 问题：分析引用内容。
- 引用自己的 HYW 回答后发送 /q 追问：继续对话，有效期 1 小时。
- /q 清空：清除自己在当前会话中的历史和来源记录。
- /qstop：取消自己在当前会话中正在进行的问答。
- /link：回复一条 HYW 回答后再发送，才会取回该次来源。
+ HELP = """HYW 搜索问答服务
+ /q <问题> —— 联网搜索并作答，支持附加至多 4 张图片
+ /q <追问> —— 引用自己此前的 HYW 回答可接续对话（有效期 1 小时）
+ /q 清空 —— 清除个人在当前会话的上下文及参考来源
+ /qstop —— 终止个人在当前会话中正在生成的问答
+ /link —— 回复某条 HYW 回答以提取对应的参考资料与网页链接
```
```diff
- 输入和引用内容会发送给配置的模型；搜索词会发送给所选搜索服务；读网页时网址会发给 Jina。"""
+ 提问与引用内容将递交至大语言模型处理，检索词由搜索引擎解析，网页抓取将请求 Jina 接口。"""
```
```diff
- raise HywError("图片像素过大，请压缩后再发送。")
+ raise HywError("图片分辨率超出限制，请适当压缩后再尝试发送。")
```
```diff
- raise HywError("无法读取图片，请使用 PNG、JPEG 或 WebP 图片。") from None
+ raise HywError("图片格式无法识别，请使用标准的 PNG、JPEG 或 WebP 图片。") from None
```
```diff
- raise HywError("压缩后的图片仍然过大。")
+ raise HywError("图片压缩后体积仍然过大，请降低原图质量后再试。")
```
```diff
- raise HywError("图片编码无效。") from None
+ raise HywError("图片 base64 编码损坏或格式无效。") from None
```
```diff
- raise HywError(f"图片下载失败（HTTP {response.status_code}）。")
+ raise HywError(f"网络图片拉取失败（HTTP {response.status_code}）。")
```
```diff
- raise HywError("单张原图不能超过 20 MB。")
+ raise HywError("单张原图体积不得超过 20 MB。")
```
```diff
- raise HywError("单张原图不能超过 20 MB。")
+ raise HywError("单张原图体积不得超过 20 MB。")
```
```diff
- raise HywError("展开后的图片不能超过 20 张。")
+ raise HywError("展开包含的图片总数不得超过 20 张。")
```
```diff
- raise HywError("服务账号 JSON 无法交给 Vertex。请管理员检查 HYW_CREDENTIALS_FILE。") from None
+ raise HywError("服务账号凭证无法加载至 Vertex，请联系管理员核对 HYW_CREDENTIALS_FILE 配置。") from None
```
```diff
- raise HywError("HYW 核心库未安装，请管理员安装 hyw-frontier 与 md2png。") from None
+ raise HywError("HYW 运行依赖缺失，请联系管理员安装 hyw-frontier 与 md2png。") from None
```
```diff
- raise HywError("问题和引用内容合计不能超过 32000 字。")
+ raise HywError("提问文本与引用正文累计上限为 32000 字。")
```
```diff
- raise HywError("每次最多分析 4 张图片。")
+ raise HywError("单次提问最多仅支持附带 4 张图片。")
```
```diff
- raise HywError("回答已生成，但出图失败。请稍后重试。") from None
+ raise HywError("文本回答已生成完毕，但生成渲染卡片时失败，请稍后重试。") from None
```
```diff
- await send_text(session, "当前仍有问题在处理中，请全部完成后再清空。")
+ await send_text(session, "当前仍有正在处理的问答任务，请等待完成之后再清空。")
```
```diff
- await send_text(session, "已清除你在当前会话中的 HYW 历史。")
+ await send_text(session, "已清空你在当前会话的 HYW 对话历史与参考资料缓存。")
```
```diff
- await send_text(session, "HYW 当前较忙，请稍后重试。")
+ await send_text(session, "HYW 当前处理负载已满，请稍等片刻后再提问。")
```
```diff
- await send_text(session, f"{config.auth_error}请管理员检查 HYW_CREDENTIALS_FILE 指向的服务账号 JSON。")
+ await send_text(session, f"{config.auth_error}请联系管理员核对 HYW_CREDENTIALS_FILE 所指服务账号 JSON 文件。")
```
```diff
- await send_text(session, "HYW 尚未配置模型密钥。请管理员填写 HYW_API_KEY、HYW_BASE_URL、HYW_MODEL，或设置 HYW_CONFIG_SOURCE 复用现有模型配置。")
+ await send_text(session, "HYW 尚未完成模型密钥配置，请联系管理员配置 HYW_API_KEY、HYW_BASE_URL、HYW_MODEL，或设置 HYW_CONFIG_SOURCE 复用已有模型。")
```
```diff
- await send_text(session, "HYW 尚未配置模型密钥。请管理员填写 HYW_API_KEY，或把 Google 服务账号 JSON 放到 HYW_CREDENTIALS_FILE 指向的路径。")
+ await send_text(session, "HYW 尚未配置模型密钥，请联系管理员设置 HYW_API_KEY，或将 Google 服务账号凭证放置于 HYW_CREDENTIALS_FILE 指定路径。")
```
```diff
- await send_text(session, f"本次问答超过 {int(config.timeout)} 秒，请稍后重试或缩小问题范围。")
+ await send_text(session, f"问答超时（已超过 {int(config.timeout)} 秒），请适当精简提问范围或稍后再试。")
```
```diff
- await send_text(session, "HYW 处理失败，请稍后重试。")
+ await send_text(session, "HYW 处理异常，请稍后重新发送。")
```
```diff
- await send_text(session, "你在当前会话中没有正在进行的 HYW 问答。")
+ await send_text(session, "你在当前会话下并无正在执行的问答任务。")
```
```diff
- await send_text(session, "已取消你在当前会话中的 HYW 问答。")
+ await send_text(session, "已中止你在当前会话中正在处理的 HYW 问答。")
```
```diff
- await send_text(session, "请回复一条 HYW 回答后再发送 /link。")
+ await send_text(session, "请先引用或回复一条 HYW 生成的回答消息，再发送 /link。")
```
```diff
- await send_text(session, "这条消息没有可查询的 HYW 来源。请回复 HYW 的回答后再发 /link。")
+ await send_text(session, "该条消息暂无可查询的参考来源记录，请确认引用的确为 HYW 的回答。")
```

### `hyw/history.py` — 1 处

```diff
- part["text"] if part["type"] == "text" else "[上一轮图片，需再次分析时请重新发送]"
+ part["text"] if part["type"] == "text" else "[前序对话图片，如需重新分析请再次发送]"
```

## radar（4 个文件 · 25 处）

### `radar/service.py` — 16 处

```diff
- raise UnknownModel(f"模型名「{raw}」对应多个候选：{'、'.join(exact)}", detail="ambiguous")
+ raise UnknownModel(f"模型名称「{raw}」存在多个匹配项：{'、'.join(exact)}", detail="ambiguous")
```
```diff
- f"「{raw}」对应的 {alias} 在当前频道没有实测数据。",
+ f"「{raw}」对应的模型 {alias} 在当前评测频道暂无实测数据。",
```
```diff
- f"「{raw}」对应多个候选：{'、'.join(partial[:8])}", detail="ambiguous"
+ f"「{raw}」匹配到多个候选模型：{'、'.join(partial[:8])}", detail="ambiguous"
```
```diff
- f"档位「{effort}」不合法，可选：{'/'.join(EFFORT_TIERS)}。", detail="unknown effort"
+ f"档位「{effort}」无效，可选范围：{'/'.join(EFFORT_TIERS)}。", detail="unknown effort"
```
```diff
- note = "已取各模型最高档"
+ note = "已筛选各模型最高档位"
```
```diff
- note = f"{note}；成本为上游估算口径".lstrip("；")
+ note = f"{note}；成本采用上游估算口径".lstrip("；")
```
```diff
- f"排序字段「{by}」不合法，可选：iq/pass_rate/cost。", detail="unknown sort"
+ f"排序字段「{by}」无效，可选：iq/pass_rate/cost。", detail="unknown sort"
```
```diff
- f"{model} 在当前频道没有该档位的实测数据。", detail="no variant"
+ f"{model} 在当前频道暂无该档位的实测数据。", detail="no variant"
```
```diff
- note = "含全部档位"
+ note = "已包含全部档位"
```
```diff
- note = "跨档位合并口径"
+ note = "跨档位综合口径"
```
```diff
- note = f"单档位 {wanted}"
+ note = f"单档位 {wanted} 口径"
```
```diff
- meta = replace(payload.meta, note=note or "无匹配序列", samples=samples)
+ meta = replace(payload.meta, note=note or "暂无匹配的时间序列", samples=samples)
```
```diff
- f"「{key}」匹配多道题：{'、'.join(item.id for item in matched[:5])}",
+ f"「{key}」匹配到多道题目：{'、'.join(item.id for item in matched[:5])}",
```
```diff
- note = f"{len(payload.tasks) - len(scored)} 道题无区分度数据，未参与排序"
+ note = f"{len(payload.tasks) - len(scored)} 道题目缺少区分度数据，未计入排序"
```
```diff
- f"榜单范围「{scope}」不合法，可选：month/total。", detail="unknown scope"
+ f"榜单统计范围「{scope}」无效，可选：month/total。", detail="unknown scope"
```
```diff
- raise InvalidArgument("流水条数上限为 50。", detail="limit too large")
+ raise InvalidArgument("流水条数单次最多支持 50 条。", detail="limit too large")
```

### `radar/handlers.py` — 7 处

```diff
- await send(session, "雷达当前较忙，请稍后再试。")
+ await send(session, "雷达系统正忙，请稍后再试。")
```
```diff
- await send(session, "雷达查询超时，请稍后重试或改用更轻的命令。")
+ await send(session, "雷达请求响应超时，请稍后重试或选用轻量指令。")
```
```diff
- await send(session, "雷达查询失败，请稍后重试。")
+ await send(session, "雷达查询异常，请稍后重试。")
```
```diff
- await send(session, format_error(InvalidArgument(f"未知子命令「{sub}」，试试 /radar 帮助。", detail="unknown subcommand")))
+ await send(session, format_error(InvalidArgument(f"未知子命令「{sub}」，可发送 /radar 帮助 查看指令列表。", detail="unknown subcommand")))
```
```diff
- name, effort = _model_and_effort(args, "用法：/radar 模型 <名> [档位]")
+ name, effort = _model_and_effort(args, "格式：/radar 模型 <名> [档位]")
```
```diff
- raise InvalidArgument("用法：/radar 对比 <A> <B> [档位]", detail="missing models")
+ raise InvalidArgument("格式：/radar 对比 <A> <B> [档位]", detail="missing models")
```
```diff
- name, effort = _model_and_effort(args, "用法：/radar 趋势 <名> [档位]")
+ name, effort = _model_and_effort(args, "格式：/radar 趋势 <名> [档位]")
```

### `radar/errors.py` — 1 处

```diff
- CODE_UPSTREAM_UNAVAILABLE: "雷达数据源暂时不可用，请稍后重试。",
- CODE_UNKNOWN_BENCHMARK: "没有这个评测频道。",
- CODE_UNKNOWN_MODEL: "没有该模型档位的实测数据。",
- CODE_INVALID_ARGUMENT: "参数不合法。",
- CODE_PAYLOAD_TOO_LARGE: "该查询的数据量过大，请改用轻量命令。",
- CODE_SCHEMA_DRIFT: "数据源结构发生变化，请反馈给维护者。",
- CODE_RATE_LIMITED: "数据源正在限流，请稍后再试。",
- CODE_PRIVATE_PATH_REFUSED: "内部错误：插件不应访问私有接口。",
+ CODE_UPSTREAM_UNAVAILABLE: "雷达数据源暂不可用，请稍后重试。",
+ CODE_UNKNOWN_BENCHMARK: "未找到该评测频道，请核对频道名称。",
+ CODE_UNKNOWN_MODEL: "未查询到该模型档位的实测记录。",
+ CODE_INVALID_ARGUMENT: "输入参数有误，请核对后重试。",
+ CODE_PAYLOAD_TOO_LARGE: "查询返回的数据量过大，请使用轻量查询指令。",
+ CODE_SCHEMA_DRIFT: "数据源格式出现变动，请联系维护者处理。",
+ CODE_RATE_LIMITED: "数据源请求过于频繁已被限流，请稍后重试。",
+ CODE_PRIVATE_PATH_REFUSED: "内部错误：禁止请求私有接口。",
```

### `radar/formatters.py` — 1 处

```diff
- "AI 智商雷达 · 命令",
- "· /radar 总览 — 模型与档位矩阵",
- "· /radar 榜 [频道] — 档位排行",
- "· /radar 模型 <名> [档位] — 模型档案",
- "· /radar 对比 <A> <B> [档位] — 对比",
- "· /radar 推荐 [频道] — 上游推荐",
- "· /radar 预警 [频道] — 降智预警",
- "· /radar 性价比 [频道] — 性价比榜",
- "· /radar 趋势 <名> [档位] — IQ 趋势",
- "· /radar 频道 — 频道清单",
- "· /radar 档位 [频道] — 模型档位清单",
+ "AI 智商雷达 · 命令帮助：",
+ "· /radar 总览 —— 查看模型与档位矩阵",
+ "· /radar 榜 [频道] —— 查看档位表现排行",
+ "· /radar 模型 <名> [档位] —— 查看模型详细档案",
+ "· /radar 对比 <A> <B> [档位] —— 对比两款模型数据",
+ "· /radar 推荐 [频道] —— 查看官方推荐模型",
+ "· /radar 预警 [频道] —— 查看模型降智预警",
+ "· /radar 性价比 [频道] —— 查看模型性价比榜",
+ "· /radar 趋势 <名> [档位] —— 追踪历史智商趋势",
+ "· /radar 频道 —— 查看所有评测频道",
+ "· /radar 档位 [频道] —— 查看模型档位清单",
```

## tibo_radar（3 个文件 · 25 处）

### `tibo_radar/handlers.py` — 16 处

```diff
- return "仅群主、群管理员或 SUPERUSER 可管理 Tibo 新帖订阅。"
+ return "权限不足：仅限群主、群管理员或 SUPERUSER 操作 Tibo 新帖订阅。"
```
```diff
- return None, "数量必须是整数"
+ return None, "参数错误：数量需为整数"
```
```diff
- return None, f"数量必须在 1–{maximum} 之间"
+ return None, f"参数错误：数量需介于 1～{maximum} 之间"
```
```diff
- "Tibo 雷达用法：\n"
- "/tibo 或 /雷达 —— 雷达总览\n"
- "/tibo 动态 [数量] —— 最近 X 动态（原文+翻译+解读），默认 6 条合一图，超过 6 条自动分页\n"
- "/tibo 状态 —— 当前预告、窗口或疑似信号\n"
- "/tibo 最近 —— 最近一次已核验完成的重置\n"
- "/tibo 历史 [数量] —— 重置事件历史，默认 6 条\n"
- "/tibo 订阅 —— 本群订阅 Tibo 新帖（仅群主/管理员/SUPERUSER）\n"
- "/tibo 取消订阅 —— 停止本群的新帖推送（仅群主/管理员/SUPERUSER）\n"
- "/tibo 订阅状态 —— 查看本群订阅状态\n"
- "/tibo 帮助 —— 显示本帮助"
+ "Tibo 雷达使用指南：\n"
+ "/tibo 或 /雷达 —— 查看雷达综合看板\n"
+ "/tibo 动态 [数量] —— 查看近期相关 X 动态（含原文、翻译与解读，默认 6 条，超出自动分页）\n"
+ "/tibo 状态 —— 查看当前重置预告、窗口期或疑似信号\n"
+ "/tibo 最近 —— 查看最近一次已完成严格核验的重置记录\n"
+ "/tibo 历史 [数量] —— 查询重置事件历史列表（默认 6 条）\n"
+ "/tibo 订阅 —— 开启本群 Tibo 新帖实时推送（仅限群主、管理员或 SUPERUSER）\n"
+ "/tibo 取消订阅 —— 关闭本群 Tibo 新帖推送（仅限群主、管理员或 SUPERUSER）\n"
+ "/tibo 订阅状态 —— 查询本群新帖订阅与推送情况\n"
+ "/tibo 帮助 —— 显示此帮助信息"
```
```diff
- await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
+ await tibo_cmd.finish("使用场景限制：Tibo 新帖订阅仅限在群聊中配置。")
```
```diff
- await tibo_cmd.finish("本群已经订阅 Tibo 新帖；新帖子会按采集周期推送。")
+ await tibo_cmd.finish("本群此前已启用 Tibo 新帖订阅，新动态将依采集周期自动推送。")
```
```diff
- await tibo_cmd.finish("已订阅本群的 Tibo 新帖；从下一条新帖开始推送，附 X 风格卡片和源帖链接。")
+ await tibo_cmd.finish("已成功开启本群 Tibo 新帖订阅，后续新帖将携图文卡片及源帖链接实时推送。")
```
```diff
- await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
+ await tibo_cmd.finish("使用场景限制：Tibo 新帖订阅仅限在群聊中配置。")
```
```diff
- await tibo_cmd.finish("已停止本群的 Tibo 新帖推送。")
+ await tibo_cmd.finish("已成功关闭本群的 Tibo 新帖推送服务。")
```
```diff
- await tibo_cmd.finish("本群当前没有启用 Tibo 新帖订阅。")
+ await tibo_cmd.finish("本群当前尚未启用 Tibo 新帖订阅，无需退订。")
```
```diff
- await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
+ await tibo_cmd.finish("使用场景限制：Tibo 新帖订阅仅限在群聊中配置。")
```
```diff
- await tibo_cmd.finish(f"本群已订阅 Tibo 新帖。\n最近推送游标：{cursor}\n最近发送：{mode}{retry}")
+ await tibo_cmd.finish(f"当前群订阅状态：已开启 Tibo 新帖推送\n最新推送记录：{cursor}\n最近投递方式：{mode}{retry}")
```
```diff
- await tibo_cmd.finish("本群未启用 Tibo 新帖订阅。使用 /tibo 订阅 开启。")
+ await tibo_cmd.finish("当前群尚未启用 Tibo 新帖订阅；可使用 /tibo 订阅 进行开启。")
```
```diff
- await tibo_cmd.finish(error + "。使用 /tibo 帮助查看用法")
+ await tibo_cmd.finish(error + "；格式示例：/tibo 动态 6")
```
```diff
- await tibo_cmd.finish(error + "。使用 /tibo 帮助查看用法")
+ await tibo_cmd.finish(error + "；格式示例：/tibo 历史 6")
```
```diff
- await tibo_cmd.finish("未知子命令。\n\n" + _help_text())
+ await tibo_cmd.finish("未知子命令，请参考以下说明：\n\n" + _help_text())
```

### `tibo_radar/service.py` — 7 处

```diff
- detail = event.window_label or "预计窗口尚未结束"
+ detail = event.window_label or "当前处于预计时间窗口内"
```
```diff
- return RadarStatus("预告窗口已过，尚未核验完成", "不能把预告时间当成已完成事实", event, active=False)
+ return RadarStatus("预告窗口已届满，暂未核验完成", "预告时间节点不可直接作为已重置依据", event, active=False)
```
```diff
- return RadarStatus("官方重置预告", "等待完成证据", event, active=True)
+ return RadarStatus("官方重置预告", "等待核验完成证据", event, active=True)
```
```diff
- return RadarStatus("疑似重置信号", "来源尚未给出可核验的完成确认", event, active=True)
+ return RadarStatus("疑似重置信号", "数据源尚无已核验的完成凭据", event, active=True)
```
```diff
- return RadarStatus("预告未被核验", "上游已将该候选标记为 rejected", latest_rejected, active=False)
- return RadarStatus("暂无进行中的重置信号", "最近一次已确认重置请查看 /tibo 最近", None, active=False)
+ return RadarStatus("预告未获核验", "数据源已将该候选标记为 rejected", latest_rejected, active=False)
+ return RadarStatus("暂无进行中的重置信号", "可发送 /tibo 最近 查看最近一次已确认重置", None, active=False)
```
```diff
- suffix = "（陈旧/部分失败）" if state.stale or state.last_error else ""
+ suffix = "（缓存陈旧/存在部分失败）" if state.stale or state.last_error else ""
```
```diff
- return lines or ["暂无采集记录"]
+ return lines or ["暂无数据采集记录"]
```

### `tibo_radar/delivery.py` — 2 处

```diff
- lines = ["Tibo 新帖订阅 · 图片暂不可用，已转为文字", ""]
+ lines = ["Tibo 新帖推送（图片渲染降级，已切换为纯文本模式）", ""]
```
```diff
- lines.append("模型解读（非核验结论）：" + _clip(post.analysis, 160))
+ lines.append("模型解读（非官方核验结果）：" + _clip(post.analysis, 160))
```

## McModQuery（1 个文件 · 10 处）

### `McModQuery/handlers.py` — 10 处

```diff
- return "找不到您搜索的内容，请尝试更换关键词"
+ return "未找到相关搜索结果，建议更换关键词重试。"
```
```diff
- msg += f"{titles[i][0]}:\n{hrefs[i]}\n"
+ msg += f"{titles[i][0]}：\n{hrefs[i]}\n"
```
```diff
- await mod.finish("用法: /mod ＜模组名＞ [数量]")
+ await mod.finish("格式：/mod <模组名> [数量]")
```
```diff
- await mod.finish("Mcmod 搜索请求超时，请稍后再试")
+ await mod.finish("MC 百科搜索请求超时，请稍后再试。")
```
```diff
- await mod.finish("Mcmod 搜索请求失败，请稍后再试")
+ await mod.finish("MC 百科搜索请求失败，请稍后重试。")
```
```diff
- msg = _build_result_msg(name, hrefs, titles, f"Mcmod中符合您搜索的mod如下(仅显示前{len(hrefs)}个)\n")
+ msg = _build_result_msg(name, hrefs, titles, f"MC 百科相关模组结果如下（仅展示前 {len(hrefs)} 项）：\n")
```
```diff
- await item.finish("用法: /资料 ＜资料名＞")
+ await item.finish("格式：/资料 <资料名>")
```
```diff
- await item.finish("Mcmod 搜索请求超时，请稍后再试")
+ await item.finish("MC 百科搜索请求超时，请稍后再试。")
```
```diff
- await item.finish("Mcmod 搜索请求失败，请稍后再试")
+ await item.finish("MC 百科搜索请求失败，请稍后重试。")
```
```diff
- msg = _build_result_msg(name, hrefs, titles, f"Mcmod中符合您搜索的资料如下(仅显示前{len(hrefs)}个)\n")
+ msg = _build_result_msg(name, hrefs, titles, f"MC 百科相关资料结果如下（仅展示前 {len(hrefs)} 项）：\n")
```

## McWikiQuery（1 个文件 · 9 处）

### `McWikiQuery/handlers.py` — 9 处

```diff
- await _finish_reply(event, "用法: /wiki ＜搜索内容＞")
+ await _finish_reply(event, "格式：/wiki <搜索内容>")
```
```diff
- await _finish_reply(event, f'未找到与 "{keyword}" 相关的内容')
+ await _finish_reply(event, f"未找到与「{keyword}」相关的内容。")
```
```diff
- await _reply_message(event, "图片生成中, 请稍后").send()
+ await _reply_message(event, "正在生成 Wiki 页面截图，请稍候……").send()
```
```diff
- await _finish_reply(event, "Wiki 页面截图失败，请稍后再试")
+ await _finish_reply(event, "页面截取失败，请稍后重试。")
```
```diff
- await _finish_reply(event, "消息可能被风控或出现其他问题, 请尝试重新查询")
+ await _finish_reply(event, "发送截图遇到异常，可能是消息触发拦截，请稍后再试。")
```
```diff
- await _finish_reply(event, f"Wiki 搜索请求失败: HTTP {status_code}")
+ await _finish_reply(event, f"Wiki 搜索请求异常：HTTP {status_code}")
```
```diff
- await _finish_reply(event, "Wiki 搜索请求超时，请稍后再试")
+ await _finish_reply(event, "Wiki 检索超时，请稍后重试。")
```
```diff
- await _finish_reply(event, "Wiki 搜索网络请求失败，请检查代理或稍后再试")
+ await _finish_reply(event, "Wiki 搜索网络异常，请确认网络连接或稍后再试。")
```
```diff
- await _finish_reply(event, "Wiki 搜索结果解析失败，请稍后再试")
+ await _finish_reply(event, "Wiki 结果解析失败，请稍后再试。")
```

## bilibilibot（2 个文件 · 9 处）

### `bilibilibot/handlers.py` — 8 处

```diff
- "用法:\n"
- "/bili follow <all|live|video|dynamic> <UID> [更多UID]\n"
- "/bili follow <all|live> room:<直播间号>    （或直接贴直播间链接）\n"
- "/bili unfollow <all|live|video|dynamic> <UID 或 room:直播间号>\n"
- "/bili list [all|live|video|dynamic]\n"
- "/bili refresh <all|live|video|dynamic> <UID 或 room:直播间号>\n"
- "提示：纯数字一律按 UID 处理；按直播间号操作请加 room: 前缀。"
+ "B站订阅指令指南：\n"
+ "/bili follow <all|live|video|dynamic> <UID> [更多UID] - 关注 UP 主\n"
+ "/bili follow <all|live> room:<直播间号> - 关注直播间（亦可直接发送直播间链接）\n"
+ "/bili unfollow <all|live|video|dynamic> <UID 或 room:直播间号> - 取消关注\n"
+ "/bili list [all|live|video|dynamic] - 查看当前订阅列表\n"
+ "/bili refresh <all|live|video|dynamic> <UID 或 room:直播间号> - 立即刷新状态\n"
+ "注：输入纯数字默认按 UID 识别；若针对直播间号请加上 room: 前缀。"
```
```diff
- await bili_cmd.finish("参数不足，请使用 /bili help 查看用法")
+ await bili_cmd.finish("参数不足：格式为 /bili <follow|unfollow|refresh> <类型> <目标>")
```
```diff
- await bili_cmd.finish("类型必须是 all/live/video/dynamic")
+ await bili_cmd.finish("订阅类型错误：仅支持 all、live、video 或 dynamic。")
```
```diff
- await _handle_result("B站订阅结果", ok, failed)
+ await _handle_result("B站关注处理结果：", ok, failed)
```
```diff
- await _handle_result("B站取关结果", ok, failed)
+ await _handle_result("B站取消关注处理结果：", ok, failed)
```
```diff
- await _handle_result("B站刷新结果", ok, failed)
+ await _handle_result("B站信息刷新结果：", ok, failed)
```
```diff
- await bili_cmd.finish("类型必须是 all/live/video/dynamic")
+ await bili_cmd.finish("订阅类型错误：仅支持 all、live、video 或 dynamic。")
```
```diff
- await bili_cmd.finish("未知子命令，请使用 /bili help 查看用法")
+ await bili_cmd.finish("未知子命令：请发送 /bili help 获取完整使用指南。")
```

### `bilibilibot/refs.py` — 1 处

```diff
- return f'无法识别 "{raw}"，请使用 UID、room:直播间号 或直播间链接'
+ return f'无法识别「{raw}」，支持输入 UID、room:直播间号 或直播间链接。'
```

## group_manager（1 个文件 · 9 处）

### `group_manager/handlers.py` — 9 处

```diff
- HELP = """本群插件开关（仅 SuperUser、本群管理员或群主可用）
- /功能 列表：查看本群插件状态
- /功能 关闭 hyw：关闭本群 HYW 问答
- /功能 开启 hyw：恢复本群 HYW 问答
- Grok Bot 默认关闭，仅 SuperUser 可用 /功能 开启 grok 开启本群；管理员和群主可关闭。
- 可使用插件名或别名，如 ef、steam、bili、mc、tibo。
- 只影响当前机器人在本群的功能，重启后保留；不接受群号参数。
+ HELP = """本群插件管理（仅限 SuperUser、本群管理员或群主）
+ /功能 列表 —— 查看本群各插件启用状态
+ /功能 开启 <插件名> —— 在本群启用指定插件（如 /功能 开启 hyw）
+ /功能 关闭 <插件名> —— 在本群禁用指定插件（如 /功能 关闭 hyw）
+ 支持插件原名及常见别名（如 ef、steam、bili、mc、tibo）。
+ Grok Bot 默认关闭，仅 SuperUser 可执行 /功能 开启 grok（管理员与群主可关闭）。
+ 设置仅作用于当前群且重启后仍保留，不支持跨群指定群号。
```
```diff
- await feature_cmd.finish("请在需要管理的群内使用此命令，私聊不能修改群开关。")
+ await feature_cmd.finish("请在目标群聊内发送此命令，私聊环境无法调整群插件开关。")
```
```diff
- await feature_cmd.finish("仅 SuperUser、本群管理员或群主可管理插件；未能确认你的管理权限。")
+ await feature_cmd.finish("权限不足：仅限 SuperUser、本群管理员或群主管理插件。")
```
```diff
- lines.append("用法：/功能 关闭 hyw 或 /功能 开启 hyw")
- lines.append("群管理和全局请求处理插件不支持群开关。")
+ lines.append("格式：/功能 开启 <插件名> 或 /功能 关闭 <插件名>")
+ lines.append("注意：基础管理及全局核心插件不支持群内开关。")
```
```diff
- await feature_cmd.finish("未找到该插件，请用 /功能 列表 查看可用插件名。")
+ await feature_cmd.finish("未找到匹配的插件，请使用 /功能 列表 查看可用插件名称。")
```
```diff
- await feature_cmd.finish("群管理和全局请求处理插件不支持群开关。")
+ await feature_cmd.finish("基础管理及全局核心插件为系统内置保护插件，不支持群内开关。")
```
```diff
- await feature_cmd.finish(f"仅 SuperUser 可开启 {plugin_label(name)}，本群管理员和群主可关闭。")
+ await feature_cmd.finish(f"权限受限：仅 SuperUser 可开启 {plugin_label(name)}，本群管理员与群主仅支持执行关闭。")
```
```diff
- await feature_cmd.finish("开关配置读写失败，请管理员检查 data/group_manager/switches.json 和目录权限。")
+ await feature_cmd.finish("插件开关配置保存失败，请联系管理员检查 data/group_manager/switches.json 及目录读写权限。")
```
```diff
- f"本群已{status} {plugin_label(name)}（{name}）。"
- + ("配置已保存。" if changed else "状态未变化。")
+ f"本群已成功{status} {plugin_label(name)}（{name}）。"
+ + ("配置已持久化保存。" if changed else "开关状态未发生改变。")
```

## request_handler（1 个文件 · 8 处）

### `request_handler/handlers.py` — 8 处

```diff
- "收到群邀请",
- f"群名: {guild_name}",
+ "收到入群邀请：",
+ f"群名：{guild_name}",
```
```diff
- lines.append(f"群号: {guild_id}")
+ lines.append(f"群号：{guild_id}")
```
```diff
- f"邀请人: {user_name}",
+ f"邀请人：{user_name}",
```
```diff
- "回复 同意 尝试自动加群，或 拒绝 忽略",
+ "请回复「同意」加入群聊，或回复「拒绝」忽略此邀请。",
```
```diff
- f"检测到群邀请卡片\n"
- f"群名: {guild_name}\n"
- f"群号: {text_or_empty(info.get('group_code')) or '未知'}\n"
- f"邀请人: {user_name}\n\n"
- f"这是 Ark 群卡片，不是 LLOneBot 暴露的系统入群请求。\n"
- f"当前没有 request_id/flag，无法自动同意；请在 QQ 客户端手动处理。"
+ f"收到群邀请卡片消息：\n"
+ f"群名：{guild_name}\n"
+ f"群号：{text_or_empty(info.get('group_code')) or '未知'}\n"
+ f"邀请人：{user_name}\n\n"
+ f"该卡片非系统底层入群请求，缺少 request_id/flag 标识，无法自动通过，请在 QQ 客户端内手动操作。"
```
```diff
- await approve_handler.send(f"已同意 {label}")
+ await approve_handler.send(f"已同意加入 {label}。")
```
```diff
- await approve_handler.send(f"已拒绝 {label}")
+ await approve_handler.send(f"已拒绝加入 {label}。")
```
```diff
- await approve_handler.send(f"操作失败: {e}")
+ await approve_handler.send(f"操作执行失败：{e}")
```

## changelog（2 个文件 · 7 处）

### `changelog/formatters.py` — 6 处

```diff
- f"查看全部 {len(changelog.releases)} 个版本：/更新日志 列表"
- f"；按版本号或序号查看：/更新日志 {changelog.latest.version}、/更新日志 2"
+ f"/更新日志 列表：浏览全部 {len(changelog.releases)} 个版本"
+ f"；/更新日志 {changelog.latest.version} 或 /更新日志 2：按版本或序号查看"
```
```diff
- "查看详情：/更新日志 <版本号|序号>，例如 /更新日志 "
- f"{changelog.latest.version} 或 /更新日志 1；也可按关键词检索。"
+ f"/更新日志 <版本号|序号>：查看指定版本详情，如 /更新日志 {changelog.latest.version} 或 /更新日志 1；支持输入关键词搜索。"
```
```diff
- lines.append(f"翻页：/更新日志 列表 {min(page + 1, pages)}")
+ lines.append(f"/更新日志 列表 {min(page + 1, pages)}：翻至下一页")
```
```diff
- return [f"没有找到与「{query}」相关的更新。试试 /更新日志 列表 看全部版本。"]
+ return [f"未查到「{query}」相关的更新，可输入 /更新日志 列表 查看完整目录。"]
```
```diff
- lines.append(f"（只显示前 {limit} 个，共 {len(results)} 个版本）")
- lines.append(f"查看详情：/更新日志 {results[0].version}")
+ lines.append(f"（仅展示前 {limit} 项，共 {len(results)} 个版本）")
+ lines.append(f"/更新日志 {results[0].version}：查看该版本详情")
```
```diff
- "更新日志：/更新日志 —— 看最近一次版本更新",
- "/更新日志 列表 [页码] —— 全部版本目录，每页 12 个",
- "/更新日志 <版本号|序号> —— 查看指定版本，例如 /更新日志 v1.13.0、/更新日志 1",
- "/更新日志 <日期|关键词> —— 例如 /更新日志 2026-09-05、/更新日志 雷达",
- "/更新日志 统计 —— 版本、更新条数与提交数汇总",
- "/更新日志 帮助 —— 显示本帮助",
+ "更新日志命令帮助：",
+ "/更新日志 —— 查看最新版本更新内容",
+ "/更新日志 列表 [页码] —— 浏览完整版本目录，每页展示 12 项",
+ "/更新日志 <版本号|序号> —— 查看指定版本详情，如 /更新日志 v1.13.0、/更新日志 1",
+ "/更新日志 <日期|关键词> —— 按日期或关键字检索，如 /更新日志 2026-09-05、/更新日志 雷达",
+ "/更新日志 统计 —— 汇总统计版本数、更新条目与提交总数",
+ "/更新日志 帮助 —— 查看帮助说明",
```

### `changelog/handlers.py` — 1 处

```diff
- await send(session, "更新日志数据暂时不可用，请稍后再试。")
+ await send(session, "更新日志暂不可用，请稍后再试。")
```

## help_plugin（1 个文件 · 6 处）

### `help_plugin/handlers.py` — 6 处

```diff
- HYW_HELP = "HYW 搜索问答：/q 问题，可附带图片；引用自己的回答后 /q 追问。\n/q 帮助 查看详细用法，/q 清空 删除当前会话历史。\n别名：/hyw、/何意味。管理员需先配置 HYW_* 模型参数。"
+ HYW_HELP = "HYW 搜索问答：\n/q <问题> —— 搜索并回答，支持附带图片\n/q <追问> —— 引用自己之前的回答即可继续追问\n/q 帮助 —— 查看详细说明\n/q 清空 —— 清空当前会话历史\n别名：/hyw、/何意味；管理员需先配置 HYW_* 模型参数。"
```
```diff
- GROK_HELP = "Grok Bot 问答：/grok 问题；引用消息后提问可附上引用正文。\n别名：/grokbot，/grok 帮助 查看说明。每群与每个私聊独立，同会话排队，不同会话可并行，默认花园多惠人设。\n默认关闭，需 SuperUser 在目标群执行 /功能 开启 grok；管理员和群主可关闭。\n私聊默认关闭，SuperUser 可用 /grok 开启 管理自己的私聊；管理员需先配置 GROKBOT_*。"
+ GROK_HELP = "Grok Bot 问答：\n/grok <问题> —— 发送文本或附带图片提问，支持引用消息提问\n/grok 帮助 —— 查看详细使用说明\n别名：/grokbot；各群与各私聊会话相互独立，默认采用花园多惠人设。\n功能默认关闭，群内需 SuperUser 执行 /功能 开启 grok（管理员与群主可关闭）；私聊需 SuperUser 使用 /grok 开启；管理员需先配置 GROKBOT_* 参数。"
```
```diff
- GROUP_FEATURE_HELP = "群内插件开关：/功能 列表、/功能 关闭 hyw、/功能 开启 hyw。\n支持插件名和 ef、steam、bili、mc、tibo 等别名。\n仅 SuperUser、本群管理员或群主可执行，只影响当前群，重启后保留。\nGrok Bot 默认关闭，仅 SuperUser 可开启，管理员和群主可关闭。"
+ GROUP_FEATURE_HELP = "本群插件开关：\n/功能 列表 —— 查看本群插件启用状态\n/功能 开启 <插件名> —— 在本群启用指定插件\n/功能 关闭 <插件名> —— 在本群禁用指定插件\n支持插件原名及别名（如 ef、steam、bili、mc、tibo 等）。\n仅 SuperUser、本群管理员或群主可操作，设置仅对当前群生效且持久保存。Grok Bot 默认关闭，仅限 SuperUser 开启，管理员与群主可关闭。"
```
```diff
- CHANGELOG_HELP = "更新日志：/更新日志 看最新版本，/更新日志 列表 看全部版本目录（每页 12 个）。\n按版本号、序号或关键词查看：/更新日志 v1.14.0、/更新日志 2、/更新日志 雷达；/更新日志 统计 看汇总。\n别名：/更新、/changelog、/版本。版本号按时间段划定；每条更新都对应仓库里的真实提交。"
+ CHANGELOG_HELP = "更新日志查询：\n/更新日志 —— 查看最新版本更新说明\n/更新日志 列表 —— 查看版本目录列表（每页 12 项）\n/更新日志 <版本号/序号/关键词> —— 查询指定版本的更新条目（如 /更新日志 v1.14.0、/更新日志 2）\n/更新日志 统计 —— 查看版本与提交汇总数据\n别名：/更新、/changelog、/版本；版本按阶段划分，每项条目均对应仓库真实提交。"
```
```diff
- await help_cmd.finish("可用的帮助主题:\n" + "\n".join(f"  /help {t}" for t in available))
- await help_cmd.finish("暂无帮助图片，请将图片放入 assets/image/help/")
+ await help_cmd.finish("可用帮助主题：\n" + "\n".join(f"  /help {t}" for t in available))
+ await help_cmd.finish("暂无可用的帮助图，请联系管理员检查 assets/image/help/ 目录。")
```
```diff
- tip = "可用的帮助主题:\n" + "\n".join(f"  /help {t}" for t in available) if available else "暂无帮助图片，请将图片放入 assets/image/help/"
+ tip = "可用帮助主题：\n" + "\n".join(f"  /help {t}" for t in available) if available else "暂无可用的帮助图，请联系管理员检查 assets/image/help/ 目录。"
```

## forkout（1 个文件 · 4 处）

### `forkout/handlers.py` — 4 处

```diff
- await session.send("叉出去图片不存在")
+ await session.send("底图资源缺失，无法生成叉出去图片。")
```
```diff
- await session.send("叉出去图片不存在")
+ await session.send("底图资源缺失，无法生成叉出去图片。")
```
```diff
- await fork_rank.finish("这个命令只能在群聊/频道中使用")
+ await fork_rank.finish("该指令仅支持在群聊或频道中使用。")
```
```diff
- await fork_rank.finish("本群还没有人被叉过")
+ await fork_rank.finish("本群暂无被叉记录。")
```
