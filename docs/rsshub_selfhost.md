# RSSHub 自建部署手册

> 面向运维：在服务器上部署一个私有 RSSHub 实例，供 otae Bot 的 bilibilibot 插件作为 B站数据的备用数据源。
>
> 相关代码：`plugins/bilibilibot/api/rsshub.py`、`plugins/bilibilibot/api/space.py`

---

## 1. 背景：为什么需要这个

bilibilibot 插件获取 B站 UP 主的视频/动态时，按以下顺序回退：

```
B站官方 API  →  RSSHub  →  动态流接口
```

**问题**：B站官方 API 对服务器 IP 有风控，会返回 `HTTP 412 Precondition Failed`。实测本机直连成功率约 2/3，代理出口更是 0/3。

**公共 RSSHub 实例不可用**：实测 11 个公共实例，当前基本全部失效。

| 实例 | 实测结果 |
|---|---|
| `rsshub.app` | HTTP 403 |
| `rsshub.rssforever.com` | HTTP 503 |
| `rsshub.moeyy.cn` | 连接失败 |
| `rss.materium.io` | 时好时坏（1/3 ~ 3/4） |
| `rss.starfreedomx.top` | 1/6（动态路由全挂） |
| 其余 6 个 | 超时 / 连接失败 |

**自建的价值**：独占实例、可配缓存、可配 cookie、不受他人限流影响。

**务必先读第 11 节「已知限制」**——自建能解决一部分问题，但**不能**解决 B站对服务器 IP 的风控。

---

## 2. 前置要求

| 项目 | 要求 |
|---|---|
| 服务器 | Linux x86_64（推荐）或 Windows Server |
| Docker | 20.10+ 与 docker compose v2（推荐方案） |
| 内存 | ≥ 1 GB 可用（RSSHub + Redis） |
| 磁盘 | ≥ 2 GB |
| 网络 | 服务器需能访问 `api.bilibili.com`、`www.bilibili.com` |
| 端口 | 默认 1200（可改） |

> **网络连通性先自测**（重要）：
> ```bash
> curl -s -o /dev/null -w "%{http_code}\n" "https://api.bilibili.com/x/web-interface/nav"
> ```
> 返回 `200` 即可。若返回 `412`，说明该服务器 IP 已被 B站风控，自建 RSSHub 效果会大打折扣（见第 11 节）。

---

## 3. 方案 A：Docker Compose（推荐）

### 3.1 目录准备

```bash
sudo mkdir -p /opt/rsshub && cd /opt/rsshub
```

### 3.2 编写 `docker-compose.yml`

```yaml
services:
  rsshub:
    image: diygod/rsshub:latest
    container_name: rsshub
    restart: unless-stopped
    ports:
      # 仅本机可访问；若机器人不在同一台机器，见 3.4 节
      - "127.0.0.1:1200:1200"
    environment:
      NODE_ENV: production
      PORT: "1200"
      # 缓存：Redis 可跨重启保留，显著降低对 B站的请求次数
      CACHE_TYPE: redis
      REDIS_URL: "redis://redis:6379/"
      # 缓存 5 分钟：兼顾"及时性"与"降低风控暴露"
      CACHE_EXPIRE: "300"
      # 单次上游请求超时（毫秒）。B站风控时较慢，给足时间
      REQUEST_TIMEOUT: "8000"
      REQUEST_RETRY: "2"
      # 只放行 bilibili 路由，减少被滥用风险
      WHITELIST: "/bilibili"
      # 时区，影响日志与部分时间类路由
      TZ: "Asia/Shanghai"
    depends_on:
      - redis
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:1200/ || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  redis:
    image: redis:7-alpine
    container_name: rsshub-redis
    restart: unless-stopped
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    volumes:
      - redis-data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  redis-data:
```

> **说明**：Redis 仅作缓存，故关闭持久化（`--save "" --appendonly no`），重启后缓存自动重建，无需备份。

### 3.3 启动

```bash
cd /opt/rsshub
docker compose up -d
docker compose ps
docker compose logs -f rsshub    # Ctrl+C 退出查看
```

首次启动约需 30–60 秒（镜像拉取 + 应用初始化）。

### 3.4 若机器人不在同一台服务器

把端口绑定改为对外（并**务必**配合第 6 节的防火墙与访问控制）：

```yaml
    ports:
      - "1200:1200"
```

然后重新 `docker compose up -d`。

---

## 4. 方案 B：裸机部署（无 Docker）

仅当服务器无法使用 Docker 时采用。需要 Node.js 22+ 与 pnpm。

```bash
# 1. 安装 Node.js 22（以 Debian/Ubuntu 为例）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo corepack enable

# 2. 获取源码
sudo mkdir -p /opt/rsshub && cd /opt/rsshub
git clone https://github.com/DIYgod/RSSHub.git .

# 3. 构建
pnpm install
pnpm build

# 4. 配置
cp .env.example .env
```

编辑 `.env`：

```dotenv
NODE_ENV=production
PORT=1200
CACHE_TYPE=memory
CACHE_EXPIRE=300
REQUEST_TIMEOUT=8000
WHITELIST=/bilibili
TZ=Asia/Shanghai
```

启动：

```bash
pnpm start
```

### 4.1 systemd 常驻

`/etc/systemd/system/rsshub.service`：

```ini
[Unit]
Description=RSSHub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rsshub
WorkingDirectory=/opt/rsshub
EnvironmentFile=/opt/rsshub/.env
ExecStart=/usr/bin/pnpm start
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rsshub
sudo systemctl status rsshub
journalctl -u rsshub -f
```

---

## 5. 关键配置说明

| 变量 | 建议值 | 为什么 |
|---|---|---|
| `CACHE_TYPE` | `redis`（Docker）/ `memory`（裸机） | 缓存是自建的核心收益，必须开启 |
| `CACHE_EXPIRE` | `300` | 5 分钟内重复请求直接命中缓存，**大幅降低对 B站的请求量与 412 概率** |
| `REQUEST_TIMEOUT` | `8000` | 单位毫秒。B站风控时响应慢，过短会导致 RSSHub 自己超时 |
| `REQUEST_RETRY` | `2` | 上游失败重试次数 |
| `WHITELIST` | `/bilibili` | 只放行 B站路由，避免实例被当作通用代理滥用 |
| `PORT` | `1200` | 默认端口，非必要不改 |

> **⚠️ 环境变量名称请以官方文档为准**
>
> 上表变量名依据 RSSHub 通用约定编写，但**本手册编写时未能访问官方文档站点**（`docs.rsshub.app`）进行逐项核对。RSSHub 迭代较快，变量名可能随版本变化。
>
> **部署前请务必对照官方配置文档确认**：`https://docs.rsshub.app/deploy/config`
>
> 若某个变量名不存在，RSSHub 会忽略它（不会报错），此时功能会**静默降级**（例如缓存未生效）。**因此第 7 节的验收测试不能省**——它才是最终判据。

### ⚠️ 关于 `ACCESS_KEY`

RSSHub 支持 `ACCESS_KEY` 做访问鉴权，但**机器人插件不支持携带 key**。**请勿设置 `ACCESS_KEY`**，否则插件所有请求都会失败。

访问控制请改用**网络层**（第 6 节）。

### 关于 B站 Cookie（可选）

RSSHub 支持为 B站路由配置 cookie 以提高风控通过率，形如：

```dotenv
BILIBILI_COOKIE_<你的B站UID>=SESSDATA=xxx; bili_jct=xxx; buvid3=xxx
```

> **注意**：
> 1. 具体变量名与格式**请以 RSSHub 官方文档 `docs.rsshub.app/deploy/config` 为准**（版本间可能变化）。
> 2. Cookie 属于敏感凭证，等同账号登录态。**建议使用小号**，并确保该文件权限为 `600`。
> 3. 实测结论：配置 cookie **对 412 风控帮助有限**（风控主要针对 IP），不要期待它解决问题。

---

## 6. 安全加固

RSSHub 本质是一个「能代表服务器发起外部请求」的服务，暴露到公网可能被滥用。生产环境建议：

1. **优先仅监听本机**（3.2 节已默认如此）。机器人与 RSSHub 同机时这是最安全的做法。
2. 若必须跨机访问，用防火墙限制来源 IP：
   ```bash
   # 仅允许机器人服务器 IP 访问 1200 端口
   sudo ufw allow from <机器人服务器IP> to any port 1200 proto tcp
   sudo ufw deny 1200/tcp
   sudo ufw reload
   ```
   （CentOS 用 `firewall-cmd`，云服务器还需同步配置安全组）
3. 若需要公网访问，**必须**置于 HTTPS 反向代理之后，并加访问控制。
4. 保持 `WHITELIST=/bilibili`。

---

## 7. 验收测试（必做）

### 7.1 服务存活

```bash
curl -s -o /dev/null -w "root: %{http_code}\n" http://127.0.0.1:1200/
```
预期：`root: 200`

### 7.2 插件真正使用的两条路由

把 `<UID>` 换成实际的 B站 UID（例如库中已有的 `135116630`）：

```bash
# 视频路由
curl -s -o /dev/null -w "video: %{http_code}  %{time_total}s\n" \
  "http://127.0.0.1:1200/bilibili/user/video/135116630"

# 动态路由
curl -s -o /dev/null -w "dynamic: %{http_code}  %{time_total}s\n" \
  "http://127.0.0.1:1200/bilibili/user/dynamic/135116630"
```

**验收标准**：

| 指标 | 要求 |
|---|---|
| HTTP 状态码 | `200` |
| 响应体 | 包含 `<item`（RSS）或 `<entry`（Atom） |
| 响应时间 | **< 8 秒**（硬性要求，见下） |

检查响应体：

```bash
curl -s "http://127.0.0.1:1200/bilibili/user/video/135116630" | head -40
```

### 7.3 ⚠️ 8 秒硬性预算

插件对每个 RSSHub 实例的单次请求预算是 **8 秒**（`plugins/bilibilibot/api/rsshub.py` 中 `FETCH_BUDGET_SECONDS = 8.0`）。超过 8 秒会被判为失败。

**正常现象**：缓存过期后的第一次请求可能较慢甚至超时（RSSHub 正在回源 B站）。但该请求会**预热缓存**，后续请求会命中缓存、在 100 ms 内返回。

**因此验收时请连续请求 2–3 次**，以第 2、3 次的结果为准：

```bash
for i in 1 2 3; do
  curl -s -o /dev/null -w "第 $i 次: %{http_code}  %{time_total}s\n" \
    "http://127.0.0.1:1200/bilibili/user/video/135116630"
  sleep 1
done
```

---

## 8. 接入机器人

在机器人项目的 `.env` 中配置（文件：`<项目根>/.env`，第 52 行附近）：

**同机部署**：

```dotenv
BILI_RSSHUB_BASE_URLS=http://127.0.0.1:1200
```

**跨机部署**：

```dotenv
BILI_RSSHUB_BASE_URLS=http://<RSSHub服务器IP>:1200
```

**多个实例（用逗号分隔，靠前的优先参与竞争）**：

```dotenv
BILI_RSSHUB_BASE_URLS=http://127.0.0.1:1200,https://rss.materium.io
```

### 配置说明

- 插件会**同时**向所有实例（配置的 + 内置的）发起请求，**取第一个成功返回的结果**，其余立即取消。因此配置多个实例是**冗余容错**，不是轮询。
- 地址**结尾不要加斜杠**（代码会处理，但保持规范）。
- 内置实例无法通过环境变量移除，但自建实例因**有缓存、响应快**，通常会在竞争中胜出。

### 生效方式

配置在**模块导入期**读取，修改 `.env` 后**必须重启机器人**：

```powershell
.\scripts\stop.bat
.\scripts\start.bat
```

---

## 9. 运维日常

### 查看日志

```bash
docker compose logs -f --tail=200 rsshub
```

关注关键字：`error`、`timeout`、`412`、`ECONNREFUSED`。

### 升级

```bash
cd /opt/rsshub
docker compose pull
docker compose up -d
docker compose ps
```

升级后请重跑第 7 节验收测试。

### 健康检查（建议加入监控）

```bash
curl -fsS http://127.0.0.1:1200/ > /dev/null && echo OK || echo FAIL
```

建议配置告警：连续 3 次探测失败即通知。

### 资源占用参考

| 项目 | 正常范围 |
|---|---|
| RSSHub 内存 | 200–500 MB |
| Redis 内存 | 50–200 MB（取决于 `CACHE_EXPIRE`） |
| CPU | 空闲时接近 0 |

---

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `curl` 连接被拒 | 容器未启动 / 端口未监听 | `docker compose ps`、`docker compose logs rsshub` |
| 路由返回 **503** | 上游 B站请求失败（多为 412 风控）或实例过载 | 查看日志确认上游状态码；确认第 2 节网络自测结果 |
| 路由返回 **403** | 命中 `WHITELIST` 之外的路径，或触发了风控 | 确认请求路径以 `/bilibili` 开头 |
| 路由返回 **404** | 路由不存在或 UID 无效 | 用有效 UID 重试；确认 RSSHub 版本 |
| 响应 > 8 秒 | 缓存未命中 + B站响应慢 | 确认 `CACHE_TYPE` 已生效；连测 2–3 次看缓存是否命中 |
| 首次请求慢、后续快 | **正常**，缓存预热 | 无需处理 |
| 机器人侧仍报失败 | `.env` 未生效 / 网络不通 | 确认已重启机器人；在机器人服务器上执行第 7 节 `curl` 测试 |
| Redis 连接错误 | Redis 容器异常 | `docker compose logs redis`；`docker compose restart` |

---

## 11. 已知限制（务必阅读）

### 11.1 自建**不能**解决 IP 风控

RSSHub 是用**这台服务器自己的 IP** 去访问 B站的。B站的风控针对 **IP**，所以：

- 如果这台服务器的 IP 被 B站风控，自建 RSSHub 的 bilibili 路由**同样会失败**（表现为 503）
- 实测本机直连 B站接口成功率约 **2/3**，即仍有约 1/3 的请求会被 412 拦截

**自建能解决的是**：公共实例过载、公共实例维护者跑路、共享配额争抢、无法自定义缓存与 cookie。

**自建解决不了的是**：自己 IP 的风控状态。

### 11.2 不要用共享代理

实测结论：让流量走共享代理出口，B站风控**更严重**（本机直连 2/3，走代理 0/3）。共享代理 IP 通常已被大规模滥用并列入黑名单。

如需更换出口，应使用**干净、独享**的 IP。

### 11.3 系统本身有兜底

即使 RSSHub 完全不可用，插件仍有最后一道回退：**B站动态流接口**（`polymer/web-dynamic/v1/feed/space`）。该接口实测稳定返回 200，因此视频通知大多仍能送达，仅延迟增加（约 4–6 秒）。

**结论**：RSSHub 自建属于「提升成功率与响应速度」，而非「从不可用变为可用」。请据此设定预期。

---

## 12. 验收清单

交付前请逐项确认：

- [ ] 服务器可访问 `api.bilibili.com`（第 2 节自测返回 200）
- [ ] 容器运行中：`docker compose ps` 显示 `rsshub` 与 `redis` 均为 `Up`
- [ ] 根路径返回 200（第 7.1 节）
- [ ] `/bilibili/user/video/<UID>` 返回 200 且含 `<item`
- [ ] `/bilibili/user/dynamic/<UID>` 返回 200 且含 `<item`
- [ ] 连续 3 次请求，第 2、3 次响应时间 **< 8 秒**
- [ ] 未设置 `ACCESS_KEY`（否则插件无法访问）
- [ ] 端口访问已按第 6 节限制来源
- [ ] 健康检查已加入监控告警
- [ ] 已提供最终访问地址给机器人侧，并已在 `.env` 配置 `BILI_RSSHUB_BASE_URLS`
- [ ] 机器人重启后，日志中不再出现 `all RSSHub instances unavailable`

---

## 13. 附录：本次相关的代码位置

| 内容 | 位置 |
|---|---|
| RSSHub 实例列表与竞争逻辑 | `plugins/bilibilibot/api/rsshub.py` |
| 8 秒预算常量 `FETCH_BUDGET_SECONDS` | `plugins/bilibilibot/api/rsshub.py` |
| 视频回退链（官方 → RSSHub → 动态流） | `plugins/bilibilibot/api/space.py` |
| 环境变量读取点 | `plugins/bilibilibot/handlers.py` |
| 环境变量模板 | `.env.example` |
