# 终末地寻访批量脚本使用说明

两个独立脚本，配合本地账号库 `data/endfield/endfield.db` 使用：

| 脚本 | 用途 | 是否需要凭据密钥 | 是否联网 |
|---|---|---|---|
| `scripts/export_endfield_gacha_history.py` | 批量导出**已授权用户**的完整寻访历史到 JSON | 需要 `ENDFIELD_CREDENTIAL_KEY` | 是（调官方接口） |
| `scripts/aggregate_endfield_gacha_stats.py` | 聚合已授权用户的**实测出率**并与官方概率公示对比 | 不需要 | 否（只读本地库） |

## 关于数据范围（重要）

这两个脚本只处理**主动在 bot 内绑定/导入过的用户**，凭据由用户自己授权、加密存放在本地库。

- 单个玩家的寻访/抽卡历史在协议层就不对第三方开放：抽卡接口要 u8 token，u8 token 只能由该账号本人的鹰角通行证换取。因此**无法**遍历“森空岛全站用户”去抓抽卡历史，这不是脚本限制，是接口本身没有入口。
- “全服实测出率”同理没有公开聚合源。社区里的寻访统计工具（`bhaoo/endfield-gacha`、`ef-gacha.mogujun.icu`、`end.shallow.ink`、`endfieldtools.dev`、`prydwen.gg`）**都是导入个人 token 记录的个人分析器**，不提供跨玩家聚合。
- 唯一真正公开、无需账号、全服级的“卡池 UP + 概率 + 保底”来源是**官方概率公示**（理论概率，非实测），已作为对比基线内置进聚合脚本。

因此 `aggregate` 脚本给出的是**社区实测**（你已授权用户范围内），不是全服实测；样本随绑定/导入用户增长而变强。

## 前置准备

1. Python 环境：仓库自带虚拟环境 `./.venv`。
2. `export` 脚本需要在 `.env` 配置解密密钥（与 bot 绑定功能同一把）：

   ```ini
   ENDFIELD_CREDENTIAL_KEY=<Base64 编码的 32 字节密钥>
   ```

   未配置时 `export` 会以退出码 `2` 提示；`aggregate` 不需要该密钥。

## 一、批量导出寻访历史

对库里每份鹰角通行证凭据，先用 SDK 的 `GET /account/binding/v1/binding_list` 展开该通行证下**全部**终末地 uid（不止手动绑定的角色），再逐个 uid 换 u8 token，把四条角色池 + 武器池的记录全量翻页导出。

```powershell
# 导出全部已授权用户
.\.venv\Scripts\python.exe scripts\export_endfield_gacha_history.py

# 只导出指定 QQ（可重复 --qq）
.\.venv\Scripts\python.exe scripts\export_endfield_gacha_history.py --qq 2461673400 --qq 2490675469
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db` | `data/endfield/endfield.db` | 账号数据库路径 |
| `--output` | `output/endfield_gacha_history` | JSON 导出目录 |
| `--qq` | 全部 | 只导出指定 QQ 用户，可重复 |
| `--concurrency` | `2` | 并发导出的角色数量（压请求频率） |
| `--delay` | `0.2` | 同一角色翻页之间的间隔秒数 |
| `--timeout` | `25.0` | 单次 HTTP 请求超时秒数 |

### 输出

`output/endfield_gacha_history/` 下：

- `<qq>_<roleId>.json`：单角色，含角色信息、武器卡池表、按 stream 分组的完整记录、`complete` 标志与逐条错误。
- `index.json`：本次汇总（凭据数、角色数、总记录数、每角色摘要、失败列表）。

不写回 `endfield.db`，可反复运行。**token 不会出现在任何输出文件里。**

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部成功 |
| `1` | 有角色失败，或没有可导出的用户 |
| `2` | 数据库不存在，或未配置 `ENDFIELD_CREDENTIAL_KEY` |

## 二、聚合实测出率并对比公示

只读本地库（`gacha_records` + 小黑盒导入 `xhh_gacha_*`），跨全部角色合并，按**角色池 / 武器池**分别统计，输出匿名聚合。不需要联网，也不解密任何凭据。

```powershell
.\.venv\Scripts\python.exe scripts\aggregate_endfield_gacha_stats.py
```

Windows 终端若显示中文乱码，先切 UTF-8 代码页（不影响写出的 JSON，文件始终是 UTF-8）：

```powershell
chcp 65001 > $null; .\.venv\Scripts\python.exe scripts\aggregate_endfield_gacha_stats.py
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db` | `data/endfield/endfield.db` | 账号数据库路径 |
| `--output` | `output/endfield_gacha_stats/aggregate.json` | JSON 输出路径 |

### 指标口径

- **六星综合出率** = 六星数 / 付费抽数，附 95% Wilson 置信区间；免费十连不计入付费出率，单列 `free_pulls`。
- **平均保底** = 相邻两个六星之间的付费抽数；每个池族第一个六星是左删失（本机记录可能不完整），单独计数 `left_censored_first_six`，不进平均。
- **保底/池族判定**（角色 special 共享、joint / beginner 隔离；武器按单条流）与 `plugins/endfield/gacha/service.py` 对齐，以其为准。
- **歪率 miss_up**：官方 `gacha_records` 不含 UP 归属，故仅取自**小黑盒导入**，输出中标 `"source": "xhh"`。

### 对比基线（官方概率公示，理论值）

| 池 | 6★ 基础 | 6★ 综合（含保底） | UP | 硬保底 | 大保底 |
|---|---|---|---|---|---|
| 角色 | 0.8% | 2.04%–2.27% | 0.4%（六星里 50% 为 UP） | 80 抽 | 120 抽必出 UP |
| 武器 | 4.0% | 5.36%–6.22% | 1.0% | 80 抽 | — |

> **注意**：`0.8% / 4.0%` 是**基础概率，不含保底**。真实付费抽因为有保底，实测出率应对齐**综合概率**（角色约 2%、武器约 5–6%），直接拿实测跟 0.8% 比是常见误读。脚本的 `comparison.notes` 里已明确标注这一点。

### 输出

`output/endfield_gacha_stats/aggregate.json`：匿名聚合，只有计数与比率，**不含 role_id / QQ / 昵称**。同时在终端打印一张速览表。

## 注意事项

- `output/` 目录当前**未**被 `.gitignore` 忽略。导出的寻访明细含玩家数据，聚合 JSON 虽为匿名，若不想入库，请先向 `.gitignore` 追加 `output/`。
- 两个脚本都通过合成包按需加载 `plugins/endfield` 下的模块，不会触发 bot 主入口；正常运行不影响线上 bot 进程与数据库（`aggregate` 只读，`export` 不写库）。
