# Grok Bot 附件协议核对记录

核对日期：2026-09-07。仅下载和只读提取公开客户端代码，没有登录账号、
读取部署 Token 或调用生产网关。以下是客户端使用的协议，不等于生产环境联调结果。

## 来源

- 官方 [Grok Bot 0.30.0 Windows 安装包](https://downloads.cursor.com/grokbot/stable/win32-x64/0.30.0/Grok_Bot_0.30.0_Setup.exe)。
  安装包 SHA-256：`cb1a5ef75b4d3ebeee7e7833ecdd66440e2639864f255e18ec6d5029058e814d`。
- 从 `resources/app.asar` 中读取 `dist/electron-main/main.cjs` 的参数声明和附件上传、
  下载调用；该文件 SHA-256：`c5189454e56820ef5108a37d9d0a8b27b906243d0f22bbe0cb202bc8f9bff708`。
- 从 `dist/renderer/assets/index-B3DQqRUH.js` 核对发出消息的图片和文件结构。
- QQ 文件接口核对了
  [LLOneBot b18cbb3](https://github.com/LLOneBot/LuckyLilliaBot/tree/b18cbb38f6b7914db77e1ea11833e5f96fc8a1f6)
  的 `src/satori/server.ts`、`src/satori/message.ts`、
  `src/onebot11/action/go-cqhttp/UploadGroupFile.ts` 和 `UploadPrivateFile.ts`。

未把安装包或客户端实现复制进本仓库。测试使用本项目构造的图片、文本和模拟响应。

## 网关调用

以下 RPC 均向 `/api/<命令名>` POST JSON，使用网关 Token 认证。
`agentId` 在附件 RPC 中可选，插件始终使用当前会话已绑定的 Bot ID。

| 命令 | 请求字段 | 返回字段 |
| --- | --- | --- |
| `uploadAttachment` | `agentId?`, `filename`, `bytesBase64` | `path` |
| `sendPrompt` | 原有参数，加 `attachmentPaths`, `attachmentNames` | 原有接收结果 |
| `readAttachmentChunk` | `agentId?`, `path`, `offset`, `length`, `videoPlayback?` | `bytesBase64`, `totalSize`, `mime` |
| `readAttachmentImage` | `path` | `dataUrl`, `width`, `height`；插件不使用预览接口 |

图片上传的 `bytesBase64` 是纯 Base64，不带 `data:` 前缀。
`sendPrompt.attachmentPaths` 使用上传结果的云端路径；显示名与路径一一对应。
读取附件先请求 `offset=0,length=0` 获取总字节数，再分块读取，
拒绝总大小变化、编码无效、空块或超出请求范围的响应，不发送半截文件。

回复结构样例（字段值均为示例）：

```json
[
  {
    "kind": "send-message",
    "message": {
      "type": "text",
      "content": "这是生成的图片。",
      "images": [{"url": "file:///output/chart.png", "alt": "chart.png"}]
    }
  },
  {
    "kind": "send-message",
    "message": {
      "type": "attachment",
      "url": "file:///output/report.pdf",
      "file_name": "report.pdf"
    }
  }
]
```

兼容旧附件记录中的 `file_path`、`fileName`、`attachmentPaths` 与 `attachmentNames`。
只解析本次请求之后的完整发出消息，不把工具记录或用户附件当作输出。

## QQ 文件交付

已核对的 LLOneBot 版本中，Satori `<file>` 的发送分支仍为 TODO，
因此对 `account.adapter == "llonebot"` 使用同一 Satori 连接的内部接口：

- 群：`internal/onebot11/upload_group_file`，参数 `group_id`、`name`、`file`。
- 私聊：`internal/onebot11/upload_private_file`，参数 `user_id`、`name`、`file`。
- `file` 使用 `base64://` 加原始字节的 Base64；适配器支持该格式。
- 接收者取当前事件的群 ID 或私聊用户 ID，响应须为 `status="ok",retcode=0`。
  这两个接口会上传并发送消息；超时后不再调用其他发送接口，避免重复交付。

图片和其他适配器的文件使用 Satori：先 `upload.create`，不支持时使用内联数据；
文件单独发送，不与正文或引用混排，并检查发送回执。
