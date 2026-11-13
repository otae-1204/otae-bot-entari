# Minecraft Wiki 查询（plugins/McWikiQuery）

在 zh.minecraft.wiki 全文搜索，取第一条结果，用共享浏览器截取正文区域，作为图片回复（超过 7000 像素高按段切开）。用户说明见 `/help mc`（见 [帮助](help_plugin.md)）。

## 命令

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/wiki <内容>` | `/查wiki` | 搜索并回复截图，回复会引用原消息 |

群聊、私聊均可，无权限要求。参数注册为 `Args["content", str]`，只接受一个不含空格的词。

## 配置

无插件配置。搜索请求走进程的 `HTTP(S)_PROXY`（`system_proxy()`）。

## 数据

无。

## 已知问题

- 带空格的关键词（如 `/wiki 红石 中继器`）不触发：参数只收一个词，Alconna 解析失败（`handlers.py:20`）。
