# 实测（双向限制的测试 bot）

改动收发消息路径必须实测（[架构规范](../development/architecture_conventions.md) 第 9 节）：只用测试账号、只在指定的测试群里，并开着双向限制。

仓库里没有这类开关，限制靠一个放在仓库之外的启动器实现。

## 做法

1. **独立工作区**：用 `git worktree add <目录> <分支>` 建一个单独的工作区，有自己的 `.venv`、`entari.yml`、`.env`、`data/`，不与线上目录共用任何文件。`data/` 从空开始，或用测试数据，不复制线上凭据库。
2. **测试账号**：一个专用的测试 QQ 号，单独的 LLBot 实例与端口，令牌写在这个工作区的 `.env`。`basic.superusers` 只填测试者。
3. **守卫启动器**：在仓库外写一个替代 `bot.py` 的入口，先 `load_config()`，再装两道限制，然后照常构建并运行应用（`otae_bot.application.create_app().run()`，或照 `main()` 先取运行锁）：
   - 入站：不是来自测试群的事件一律丢弃（按事件的 guild / channel ID 判断，私聊也丢，除非专门测私聊）。可以像 `otae_bot/accounts/event_login.py` 那样继承 `Entari` 覆盖公开的 `post`，在交给父类前过滤。
   - 出站：拒绝发往测试群以外的发送。监听 Entari 的公开事件 `APIRequest`（`arclet.entari.event.api`，`otae_bot/pipeline/quote_fallback.py` 有用法），对发消息类接口检查目标频道，不是测试群就阻止并记日志。
   - 只用公开接口，不 monkeypatch。
4. **启动后先确认限制生效**：在别的群发指令应无反应、日志里能看到被拦下的记录，再开始测。

## 记录

测了什么、在哪个提交、结果如何，写进提交说明；没测到的路径写「未实测」。实测发现的问题先补单元测试再修。
