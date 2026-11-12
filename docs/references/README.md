# 框架参考资料

[`entari-plugin-writing.md`](entari-plugin-writing.md) 是用户于 2026-10-07 提供的
Entari 插件编写参考，原文件名为 `SKILL (2).md`。此处保留完整原文，未改写示例。
复制时已核对原件与副本一致，原件的 SHA-256 为
`0ed20547065b9bc9d25b4f6e22815b01770e9a9fc1543a5aaa70023f45fa2312`。
Git 检出时可能转换换行符，跨平台校验时需留意这一差异。

原文是框架参考资料，不是 otae-bot 的新增开发规范，也未安装为全局 Codex skill。
其中独立发行插件的依赖示例为 `arclet-entari>=0.18.0`；本项目的 `pyproject.toml`
固定为 **0.19.4**。涉及 API 或插件组织方式时，需核对当前安装版本，
不能直接把示例当成升级要求。

本项目的开发入口见 [代码结构](../code_structure.md)：命令、监听与会话直接用 Entari
公开接口，多账号、对话与定时任务复用 `otae_bot/accounts/`、`otae_bot/messaging/`、
`otae_bot/scheduling/`，HTTP、存储与渲染复用 `otae_bot/infrastructure/`。
项目内功能仍放在 `plugins/<功能>/`，不因参考文档的发行插件示例而搬迁目录、
更换数据库或修改依赖。`bot.py` 的网络连接配置来自 `.env` 中的 `SATORI_CLIENTS`。
